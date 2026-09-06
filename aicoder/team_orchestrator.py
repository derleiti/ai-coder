"""Experimental RAM-backed multi-agent orchestration for AICoder.

The orchestrator deliberately reuses NativeLightRuntime for every tool-capable
worker so tool calling, approvals, recovery, telemetry and workspace protection
stay identical to normal AICoder runs.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Callable
import uuid

from . import audit
from .agent_runtime import AgentRunResult, NativeLightRuntime
from .failure_tracking import FailureTracker
from .executor import build_system_prompt, load_tools
from .model_transport import ModelTransport
from .performance import RuntimePerformance
from .team_runtime import (
    BRAINSTORM_EVOLUTION_SYSTEM_PROMPT, BRAINSTORM_OPERATOR_SYSTEM_PROMPT,
    BRAINSTORM_PERSPECTIVES, BRAINSTORM_SYNTHESIS_SYSTEM_PROMPT, BRAINSTORM_SYSTEM_PROMPT,
    CODER_SYSTEM_TEMPLATE, COORDINATOR_SYSTEM_PROMPT, MERGE_PLANNER_SYSTEM_PROMPT, MERGE_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT, RESEARCH_INSTRUCTIONS, RESEARCH_OUTPUT_CONTRACT,
    RESEARCH_PLANNER_SYSTEM_PROMPT, TEST_PLANNER_SYSTEM_PROMPT, TeamConfig,
)
from .team_handoff import (
    BRAINSTORM_SECTIONS, CODE_PLAN_SECTIONS, MERGE_PLAN_SECTIONS, RESEARCH_SECTIONS,
    HandoffEnvelope, make_handoff,
)
from .team_pipeline import (
    StageLedger, TeamStage, blind_candidate_id, configured_project_python, execute_verification_plan,
    objective_rank_key, project_verification_plan, test_change_evidence, verification_passed,
)
from .workspace import resolve_or_create_project_workspace
from .workspace_backend import (
    RamWorkspace, WorkspaceBackend, WorkspaceError, create_isolated_team_workspace,
    team_workspace_plan,
)

EventFn = Callable[[str, dict[str, Any]], None]
StopFn = Callable[[], bool]

_RESEARCH_TOOL_NAMES = frozenset({
    "search", "crawl", "web_fetch_local", "web_search_local", "doc_read", "doc_search",
    "file_read", "file_tree", "code_read", "code_tree", "code_search", "code_grep",
    "git", "skill_read",
})
_CODER_TOOL_NAMES = frozenset({
    "file_read", "file_edit", "file_tree", "directory_create", "code_read", "code_tree",
    "code_search", "code_grep", "git", "lint", "test", "binary_exec", "skill_read",
    "doc_read", "doc_search",
})


@dataclass
class AgentStageResult:
    role: str
    model: str
    status: str
    response: str
    elapsed_ms: int
    error: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class CandidateResult:
    slot: int
    model: str
    strategy: str
    workspace: WorkspaceBackend
    run: AgentRunResult
    score: int = 0
    evaluation: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    evaluation_ms: int = 0


@dataclass
class TeamRunResult:
    status: str
    response: str
    model: str
    stages: list[AgentStageResult]
    candidates: list[CandidateResult]
    performance: dict[str, Any]
    error: str = ""


def _plan_grounding_issues(plan: str, source_workspace: str) -> list[str]:
    """Detect implementation plans that treat nonexistent top-level project areas as real.

    The planner may propose new files, but inventing an unrelated top-level package (for
    example ``src/`` in a project whose package is ``aicoder/``) sends every candidate
    down the same invalid architecture. Keep this deterministic and conservative: only
    path-like references with a missing top-level component are rejected.
    """
    root = Path(source_workspace).expanduser().resolve(strict=True)
    try:
        existing_top = {entry.name for entry in root.iterdir()}
    except OSError:
        return []
    allowed_virtual = {".", ".."}
    issues: list[str] = []
    seen: set[str] = set()
    # Backticks and ordinary slash-containing path tokens cover planner sections and
    # verification commands without trying to parse arbitrary prose as filesystem data.
    candidates = re.findall(r"`([^`]+)`|(?<![\w.-])(/?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)", str(plan or ""))
    for pair in candidates:
        raw = next((item for item in pair if item), "") if isinstance(pair, tuple) else str(pair)
        token = raw.strip().strip("\'\"()[]{}:,;")
        if not token or token.startswith(("http://", "https://")):
            continue
        if not re.fullmatch(r"(?:\./)?/?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?", token):
            continue
        # Ignore command/module notation that is not a project path.
        if " " in token or token.startswith(("python/", "pytest/")):
            continue
        path = Path(token)
        if path.is_absolute():
            try:
                rel = path.resolve(strict=False).relative_to(root)
            except ValueError:
                # Absolute paths below /src, /tests etc. are a common hallucination. The
                # authoritative root itself is the one allowed absolute project prefix.
                if token.startswith(("/src/", "/tests/", "/aicoder/")):
                    key = f"absolute project path outside authoritative root: {token}"
                    if key not in seen:
                        issues.append(key); seen.add(key)
                continue
        else:
            rel = path
        parts = [part for part in rel.parts if part not in allowed_virtual]
        if not parts:
            continue
        top = parts[0]
        if top not in existing_top and top not in {root.name}:
            key = f"unknown top-level project area referenced by plan: {top}/ (from {token})"
            if key not in seen:
                issues.append(key); seen.add(key)
    return issues[:12]


def _repair_ungrounded_code_plan(
    model_client: ModelTransport, *, model: str, task: str, source_workspace: str,
    original_plan: AgentStageResult, event_fn: EventFn | None, stop_requested: StopFn | None,
) -> AgentStageResult:
    issues = _plan_grounding_issues(original_plan.response, source_workspace)
    if not issues:
        return original_plan
    _emit(event_fn, "team_plan_grounding", status="repairing", issues=issues)
    prompt = (
        "The implementation plan below failed deterministic repository-grounding checks. "
        "Repair the plan; do not broaden scope or implement anything. Preserve useful evidence-backed goals, "
        "but map them onto the ACTUAL repository layout. Do not invent existing modules, packages, dependencies, "
        "or integration points. New files may be proposed only inside existing project areas unless a new top-level "
        "area is explicitly justified by the user task/evidence. Return the same required planner sections.\n\n"
        f"USER TASK:\n{task}\n\nREPOSITORY CONTEXT:\n{_repository_context(source_workspace)}\n\n"
        "GROUNDING FAILURES:\n- " + "\n- ".join(issues)
        + "\n\nORIGINAL PLAN:\n" + original_plan.response
    )
    repaired = _call_advisor(
        model_client, model=model, system=PLANNER_SYSTEM_PROMPT, prompt=prompt,
        max_tokens=9000, event_fn=event_fn, role="plan_code_repair", stop_requested=stop_requested,
    )
    repaired.role = "plan_code"
    if repaired.status != "completed":
        return repaired
    remaining = _plan_grounding_issues(repaired.response, source_workspace)
    if remaining:
        repaired.status = "failed"
        repaired.error = "implementation plan remained ungrounded after repair: " + "; ".join(remaining)
        _emit(event_fn, "team_plan_grounding", status="failed", issues=remaining)
    else:
        _emit(event_fn, "team_plan_grounding", status="passed", repaired=True)
    return repaired


def _coder_worker_count(configured_coders: int) -> int:
    """Run every configured coding candidate concurrently; provider staggering happens inside workers."""
    return max(1, int(configured_coders))


def _redact_debug_value(value: Any, *, key: str = "") -> Any:
    """Redact likely secrets without truncating diagnostic payloads."""
    sensitive = {"password", "passwd", "token", "bearer", "secret", "api_key", "apikey",
                 "authorization", "private_key", "privatekey", "client_secret", "clientsecret",
                 "access_token", "accesstoken"}
    normalized = key.lower().replace("-", "_")
    if normalized in sensitive:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact_debug_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_debug_value(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_redact_debug_value(item, key=key) for item in value]
    if isinstance(value, str):
        try:
            return audit._redact_inline(value)
        except Exception:
            return value
    return value


_TEAM_DEBUG_LOG_PATH = Path("/tmp/aicoder-experimental.log.jsonl")


def reset_team_debug_log() -> Path:
    """Start one fresh process-session trace at a stable /tmp path."""
    try:
        _TEAM_DEBUG_LOG_PATH.write_text("", encoding="utf-8")
        os.chmod(_TEAM_DEBUG_LOG_PATH, 0o600)
    except OSError:
        pass
    return _TEAM_DEBUG_LOG_PATH


class _TeamDebugLog:
    """Best-effort complete JSONL trace shared by all team runs in this process session."""

    def __init__(self, run_id: str):
        self.run_id = str(run_id)
        self.path = _TEAM_DEBUG_LOG_PATH
        try:
            self.path.touch(mode=0o600, exist_ok=True)
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            row = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "run_id": self.run_id,
                "kind": str(kind),
                "payload": _redact_debug_value(payload),
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass


def _event_with_debug(fn: EventFn | None, debug_log: _TeamDebugLog) -> EventFn:
    def sink(kind: str, payload: dict[str, Any]) -> None:
        enriched = {"run_id": debug_log.run_id, **payload}
        debug_log.write(kind, enriched)
        if fn is not None:
            fn(kind, enriched)
    return sink


def _emit(fn: EventFn | None, kind: str, **payload: Any) -> None:
    if fn is None:
        return
    try:
        fn(kind, payload)
    except Exception:
        pass


def _worker_event_forwarder(fn: EventFn | None, role: str) -> EventFn:
    """Forward useful worker-runtime telemetry without exposing model/provider identity to peers."""
    allowed = {
        "model_start", "model_response", "thought", "tool_call", "tool_result",
        "error", "paused", "performance_warning", "performance_summary", "final",
        "verification_required", "completion_audit", "runtime_status", "final_response_repair",
        "loop_prevented", "completion_signal",
    }
    def forward(kind: str, payload: dict[str, Any]) -> None:
        if kind not in allowed:
            return
        forwarded = dict(payload)
        for key in ("role", "event", "kind"):
            forwarded.pop(key, None)
        _emit(fn, "team_worker_event", role=role, event=kind, **forwarded)
    return forward


def _advisor_retryable(reason: str, exc: Exception | None = None) -> bool:
    if exc is not None and bool(getattr(exc, "retryable", False)):
        return True
    category, _signature, retryable = FailureTracker.classify(reason)
    return category == "transient" and retryable


def _call_advisor(
    model_client: ModelTransport,
    *,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int = 6000,
    event_fn: EventFn | None = None,
    role: str = "advisor",
    stop_requested: StopFn | None = None,
) -> AgentStageResult:
    """Run a stateless advisor with bounded recovery for empty/transient provider failures."""
    started = time.monotonic()
    attempt = 0
    while True:
        if stop_requested and stop_requested():
            return AgentStageResult(role, model, "failed", "", int((time.monotonic()-started)*1000), "advisor stopped by user")
        attempt += 1
        try:
            result = model_client.chat(
                message=prompt, model=model, system_prompt=system, temperature=0.2,
                max_tokens=max_tokens, fallback_model=None, tools=None, tool_choice="none",
            )
            response = str(result.get("response") or "").strip() if isinstance(result, dict) else ""
            metrics = {"prompt_chars": len(prompt), "response_chars": len(response), "attempts": attempt}
            if response:
                return AgentStageResult(
                    role, str(result.get("model") or model), "completed", response,
                    int((time.monotonic()-started)*1000), evidence=metrics,
                )
            reason = "empty response"
            retryable = True
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            retryable = _advisor_retryable(str(exc), exc)
        if not retryable or attempt >= 4:
            return AgentStageResult(
                role, model, "failed", "", int((time.monotonic()-started)*1000), reason,
                evidence={"prompt_chars": len(prompt), "response_chars": 0, "attempts": attempt},
            )
        delay = min(8.0, float(2 ** (attempt - 1)))
        _emit(event_fn, "team_worker_event", role=role, event="runtime_status", category="recovery",
              status="backoff", phase="advisor_retry", message=f"advisor retry {attempt}/4 in {delay:.0f}s: {reason[:500]}")
        deadline=time.monotonic()+delay
        while time.monotonic() < deadline:
            if stop_requested and stop_requested():
                return AgentStageResult(role, model, "failed", "", int((time.monotonic()-started)*1000), "advisor stopped by user")
            time.sleep(min(0.25, max(0.0, deadline-time.monotonic())))


def _filtered_tools(catalogue: list[dict], names: frozenset[str]) -> list[dict]:
    return [dict(tool) for tool in catalogue if str(tool.get("name") or "") in names]


def _task_handoff(task: str) -> HandoffEnvelope:
    return make_handoff("task", task, max_chars=5000)


def _research_plan_handoff(text: str) -> HandoffEnvelope:
    return make_handoff("research-contract", text, max_chars=4500)


def _code_plan_handoff(text: str) -> HandoffEnvelope:
    return make_handoff("code-contract", text, max_chars=9000, section_labels=CODE_PLAN_SECTIONS)


def _merge_plan_handoff(text: str) -> HandoffEnvelope:
    return make_handoff("merge-contract", text, max_chars=6000, section_labels=MERGE_PLAN_SECTIONS)


def _compact_check_summary(checks: dict[str, Any]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for name, raw in sorted((checks or {}).items()):
        item = raw if isinstance(raw, dict) else {}
        rows[str(name)] = {
            "ok": bool(item.get("ok")),
            "exit_code": item.get("exit_code"),
            "elapsed_ms": item.get("elapsed_ms"),
            "required": bool(item.get("required", True)),
        }
    return rows


def _compact_diff(diff: str, max_chars: int = 6000) -> str:
    text = str(diff or "")
    if len(text) <= max_chars:
        return text
    headers = []
    for line in text.splitlines():
        if line.startswith(("diff --git ", "--- ", "+++ ", "@@ ")):
            headers.append(line)
    header_text = "\n".join(headers[:80])
    remaining = max(512, max_chars - len(header_text) - 120)
    sample = text[:remaining]
    return (header_text + "\n\nDIFF SAMPLE:\n" + sample).strip()[:max_chars]


def _compact_candidate_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in evidence:
        delta = item.get("delta") if isinstance(item.get("delta"), dict) else {}
        compact.append({
            "candidate_id": item.get("candidate_id"),
            "score": item.get("score"),
            "verification_passed": bool(item.get("verification_passed")),
            "checks": _compact_check_summary(item.get("checks") or {}),
            "delta": {
                "changed_count": int(delta.get("changed_count") or 0),
                "deleted_count": int(delta.get("deleted_count") or 0),
                "added_count": int(delta.get("added_count") or 0),
                "modified_count": int(delta.get("modified_count") or 0),
                "changed": list(delta.get("changed") or [])[:60],
                "deleted": list(delta.get("deleted") or [])[:60],
                "added_files": list(delta.get("added_files") or [])[:120],
                "modified_files": list(delta.get("modified_files") or [])[:120],
                "deleted_files": list(delta.get("deleted_files") or [])[:120],
            },
            "diff_excerpt": _compact_diff(str(item.get("diff") or "")),
            "snapshot": item.get("snapshot"),
            "change_manifest": item.get("change_manifest"),
        })
    return compact


def _research_approval(_tool_name: str, _args: dict) -> bool:
    return False

_research_approval._aicoder_autonomous_policy = True


def _run_researcher(
    *, client, model_client: ModelTransport, model: str, role: str, task: str,
    source_workspace: str, tools: list[dict], stop_requested: StopFn | None,
    research_plan: str = "", native_openrouter_tool_calling: bool = False, event_fn: EventFn | None = None,
    request_timeout: int = 300,
) -> AgentStageResult:
    task_handoff = _task_handoff(task)
    research_handoff = _research_plan_handoff(research_plan or "(none)")
    prompt = (
        f"USER TASK HANDOFF:\n{task_handoff.render()}\n\n"
        f"Repository root for read-only inspection: {source_workspace}\n\n"
        f"RESEARCH CONTRACT HANDOFF:\n{research_handoff.render()}\n\n"
        + RESEARCH_INSTRUCTIONS[role] + "\n\n" + RESEARCH_OUTPUT_CONTRACT
    )
    system = build_system_prompt(tools, source_workspace).rstrip() + (
        "\n\n## RESEARCH AGENT ROLE\n" + RESEARCH_INSTRUCTIONS[role] + "\n\n" + RESEARCH_OUTPUT_CONTRACT
    )
    started = time.monotonic()
    evidence_events: list[dict[str, Any]] = []

    forward = _worker_event_forwarder(event_fn, f"research:{role}")
    def research_event(kind: str, payload: dict[str, Any]) -> None:
        if kind in {"tool_call", "tool_result"}:
            row = {"kind": kind, **dict(payload)}
            evidence_events.append(row)
        forward(kind, payload)

    conversation: list[dict[str, Any]] = []
    current_prompt = prompt
    result: AgentRunResult | None = None
    for attempt in range(0, 5):
        runtime = NativeLightRuntime(
            client=client, model_client=model_client, initial_prompt=current_prompt,
            model=model, fallback_model=None, workspace_root=source_workspace,
            plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
            tools=tools, system_prompt=system, load_tools_on_start=True,
            quick_chat=False, persistent_plan=False, approval_fn=_research_approval,
            max_iterations=10, max_output_tokens=6000, stop_requested=stop_requested,
            base_timeout=max(10, min(300, int(request_timeout))), event_fn=research_event,
            native_openrouter_tool_calling=bool(native_openrouter_tool_calling),
            conversation=conversation,
        )
        result = runtime.run()
        if result.status != "paused" or (stop_requested and stop_requested()) or attempt >= 4:
            break
        reason = str(result.response or result.error or "research worker paused")
        if _is_incomplete_envelope_reason(reason):
            conversation = []
            current_prompt = _fresh_worker_recovery_prompt(result, reason, attempt + 1, label=f"research:{role}") + (
                "\n\nContinue read-only, gather only missing evidence, then return the required compact research report."
            )
            recovery_status = "fresh_chat"
        else:
            conversation = _candidate_conversation(result)
            current_prompt = (
                f"AUTONOMOUS RESEARCH RESUME {attempt + 1}/4\n\nPrevious pause reason:\n{reason[:1600]}\n\n"
                "Continue the same read-only research assignment from existing evidence. Do not restart or modify state. "
                "Resolve the blocker, gather only missing evidence, then return the required compact research report."
            )
            recovery_status = "resuming"
        _emit(event_fn, "team_worker_event", role=f"research:{role}", event="runtime_status",
              category="recovery", status=recovery_status, phase="research_resume",
              message=f"automatic research resume {attempt + 1}/4: {reason[:500]}")
    assert result is not None
    research_tool_names = {
        str(item.get("name") or "") for item in evidence_events
        if item.get("kind") == "tool_result" and not bool(item.get("is_error"))
    }
    external_tools = sorted(name for name in research_tool_names if name in {
        "search", "crawl", "web_fetch_local", "web_search_local", "doc_search", "doc_read",
    })
    evidence = {
        "successful_tools": sorted(research_tool_names),
        "external_tools": external_tools,
        "externally_verified": bool(external_tools),
        "tool_event_count": len(evidence_events),
    }
    return AgentStageResult(
        role=f"research:{role}", model=result.model or model, status=result.status,
        response=result.response, elapsed_ms=int((time.monotonic()-started)*1000), error=result.error,
        evidence=evidence,
    )


def _repository_context(source_workspace: str) -> str:
    root = Path(source_workspace)
    rows: list[str] = [f"workspace={root}"]
    try:
        proc = subprocess.run(["git", "-C", str(root), "status", "--short", "--branch"], capture_output=True, text=True, timeout=5)
        rows.append("git_status:\n" + (proc.stdout.strip() or proc.stderr.strip())[:6000])
    except Exception as exc:
        rows.append(f"git_status_unavailable={exc}")
    try:
        remote = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        remote_url = (remote.stdout.strip() or "") if remote.returncode == 0 else ""
        if remote_url:
            rows.append("git_remote_origin=" + remote_url)
    except Exception:
        pass
    try:
        entries = sorted(p.name for p in root.iterdir() if p.name not in {".git", ".venv", "node_modules"})[:80]
        rows.append("top_level=" + ", ".join(entries))
    except Exception:
        pass
    return "\n".join(rows)


def _brainstorm_rounds(state: dict[str, Any]) -> int:
    try:
        return max(1, min(5, int(state.get("team_brainstorm_rounds") or 2)))
    except (TypeError, ValueError):
        return 2


def _brainstorm_participants(config: TeamConfig, limit: int = 6) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(label: str, model: str | None, perspective: str) -> None:
        value = str(model or "").strip()
        if not value or value in seen or len(rows) >= max(1, int(limit)):
            return
        seen.add(value)
        rows.append((label, value, perspective))

    for slot in config.research:
        add(f"research:{slot.role}", slot.model, BRAINSTORM_PERSPECTIVES.get(slot.role, "novel engineering opportunities"))
    coder_perspectives = {
        "conservative/minimal-change": "feasibility, compatibility, low-risk changes and simplicity",
        "architecture-first": "architecture boundaries, extensibility and long-term coherence",
        "performance/efficiency": "latency, resource efficiency, throughput and developer productivity",
        "robustness/security": "security hardening, resilience, recovery, observability and abuse resistance",
    }
    for slot in config.coders:
        add(f"coder:{slot.strategy}", slot.model, coder_perspectives.get(slot.strategy, "implementation opportunities"))
    add("planner", config.planner_model, "requirements, product coherence and testable outcomes")
    add("coordinator", config.coordinator_model, "cross-team synthesis, dependency risks and missing acceptance criteria")
    add("merge", config.merge_model, "integration safety, composability and conflict reduction")
    add("test_planner", config.test_planner_model, "testability, failure injection and regression prevention")
    return rows


def _brainstorm_research_handoff(research: list[AgentStageResult]) -> str:
    rows: list[str] = []
    for item in research:
        handoff = make_handoff(
            f"{item.role}-report", item.response or item.error or "(no report)",
            max_chars=3000, section_labels=RESEARCH_SECTIONS,
        )
        evidence = item.evidence or {}
        rows.append(
            f"### {item.role} · status={item.status} · externally_verified={bool(evidence.get('externally_verified'))}\n"
            + handoff.render()
        )
    return "\n\n".join(rows) or "(no research reports)"


def _build_brainstorm_prompt(
    task: str, repo_context: str, research_handoff: str, perspective: str,
    *, round_index: int, brainstorm_state: str,
) -> str:
    return (
        f"ORIGINAL USER TASK:\n{_task_handoff(task).render()}\n\n"
        f"REPOSITORY CONTEXT:\n{make_handoff('repository-context', repo_context, max_chars=4500).render()}\n\n"
        f"RESEARCH EVIDENCE HANDOFFS:\n{research_handoff}\n\n"
        f"BRAINSTORM ROUND: {round_index}\n"
        f"YOUR PERSPECTIVE: {perspective}\n\n"
        f"CURRENT ANONYMIZED BRAINSTORM STATE:\n{brainstorm_state or '(none - create independent ideas)'}"
    )


def _anonymized_brainstorm_round(results: list[AgentStageResult]) -> str:
    rows: list[str] = []
    usable = sorted(results, key=lambda item: (item.role, item.response or item.error))
    for index, item in enumerate(usable, start=1):
        handoff = make_handoff(
            f"brainstorm-proposal-{index}", item.response or item.error or "(empty)",
            max_chars=3500, section_labels=BRAINSTORM_SECTIONS,
        )
        rows.append(f"### proposal-{index:02d} · status={item.status}\n{handoff.render()}")
    return "\n\n".join(rows) or "(no usable proposals)"


def _build_brainstorm_operator_prompt(
    task: str, round_index: int, results: list[AgentStageResult], previous_state: str,
) -> str:
    previous = make_handoff(
        "brainstorm-state", previous_state or "(none)", max_chars=7000, section_labels=BRAINSTORM_SECTIONS,
    )
    return (
        f"ORIGINAL USER TASK:\n{_task_handoff(task).render()}\n\n"
        f"ROUND: {round_index}\n\n"
        f"PREVIOUS STATE:\n{previous.render()}\n\n"
        f"ANONYMIZED ROUND PROPOSALS:\n{_anonymized_brainstorm_round(results)}"
    )


def _build_brainstorm_synthesis_prompt(task: str, state: str, results: list[AgentStageResult]) -> str:
    state_handoff = make_handoff(
        "brainstorm-state-final", state or "(none)", max_chars=8000, section_labels=BRAINSTORM_SECTIONS,
    )
    return (
        f"ORIGINAL USER TASK:\n{_task_handoff(task).render()}\n\n"
        f"FINAL EVOLVED STATE:\n{state_handoff.render()}\n\n"
        f"ANONYMIZED CONTRIBUTIONS:\n{_anonymized_brainstorm_round(results)}"
    )


def _brainstorm_handoff(text: str) -> HandoffEnvelope:
    return make_handoff("brainstorm-synthesis", text, max_chars=8000, section_labels=BRAINSTORM_SECTIONS)


def _build_planner_prompt(task: str, repo_context: str, research: list[AgentStageResult]) -> str:
    task_handoff = _task_handoff(task)
    reports = []
    for item in research:
        evidence = item.evidence or {}
        verified = "verified-tool-evidence" if evidence.get("externally_verified") else "unverified-or-local-only"
        tools = ",".join(evidence.get("successful_tools") or []) or "none"
        raw = item.response or item.error or "(no report)"
        handoff = make_handoff(
            f"{item.role}-report", raw, max_chars=3500, section_labels=RESEARCH_SECTIONS,
        )
        reports.append(
            f"### {item.role} · status={item.status} · evidence={verified} · tools={tools}\n"
            f"{handoff.render()}"
        )
    repo_handoff = make_handoff("repository-context", repo_context, max_chars=5000)
    return (
        f"ORIGINAL USER TASK:\n{task_handoff.render()}\n\n"
        f"REPOSITORY CONTEXT:\n{repo_handoff.render()}\n\n"
        "INDEPENDENT RESEARCH HANDOFFS:\n" + "\n\n".join(reports)
    )


def _candidate_prompt(task: str, plan: str, coordinator: str, strategy: str) -> str:
    task_handoff = _task_handoff(task)
    plan_handoff = _code_plan_handoff(plan)
    coordinator_handoff = make_handoff("coordination-notes", coordinator or "(none)", max_chars=2500)
    return (
        f"ORIGINAL USER TASK:\n{task_handoff.render()}\n\n"
        f"SHARED IMPLEMENTATION CONTRACT:\n{plan_handoff.render()}\n\n"
        f"COORDINATION NOTES:\n{coordinator_handoff.render()}\n\n"
        f"Your strategy emphasis is {strategy}. Implement the complete shared contract, not only the strategy-specific parts."
    )


def _candidate_approval(tool_name: str, args: dict) -> bool:
    """Autonomous candidate policy: safe RAM mutations yes; elevation/destruction/escape/security never."""
    from .executor import is_destructive
    from .privileges import assess_execution
    risk = assess_execution(tool_name, args, destructive=is_destructive(str(args.get("command") or "")))
    if args.get("_workspace_escape") or risk.elevation or risk.deletion or risk.destructive or risk.security_change:
        return False
    return bool(risk.mutation) or not risk.needs_approval


# Distinguish autonomous safety denial from explicit operator rejection.
_candidate_approval._aicoder_autonomous_policy = True

_TEAM_CANDIDATE_MAX_AUTO_RESUMES = 4
_TEAM_MERGE_MAX_AUTO_RESUMES = 4


def _candidate_pause_is_resumable(run: AgentRunResult, stop_requested: StopFn | None) -> bool:
    """Return whether a team candidate pause may be continued without human input."""
    if run.status != "paused":
        return False
    if stop_requested is not None and stop_requested():
        return False
    reason = str(run.response or run.error or "").strip().lower()
    non_resumable_markers = (
        "stopped by user",
        "user rejected",
        "approval rejected",
        "approval denied",
        "explicit confirmation",
        "security policy",
        "high-risk",
    )
    return not any(marker in reason for marker in non_resumable_markers)


def _candidate_resume_prompt(run: AgentRunResult, delta: dict[str, Any], attempt: int) -> str:
    """Build a targeted autonomous continuation turn for a paused RAM candidate."""
    reason = str(run.response or run.error or "paused without a specific reason").strip()
    changed = int(delta.get("changed_count") or 0)
    deleted = int(delta.get("deleted_count") or 0)
    has_delta = bool(changed or deleted)
    lower = reason.lower()
    if "without making a change" in lower or "no mutation" in lower:
        action = (
            "No repository mutation was completed. Implement the best-supported change from the shared contract now, "
            "then verify it."
        )
    elif "verification" in lower or "verify" in lower:
        action = (
            "The candidate already has work in progress. Run the appropriate post-change verification now, fix any "
            "failures, and only then finish."
        )
    elif "same tool" in lower or "repeating" in lower or "without progress" in lower:
        action = (
            "Do not repeat the previous tool operation unchanged. Use the existing result, inspect a different signal, "
            "or take the next concrete implementation/verification step."
        )
    elif "transient" in lower or "backend" in lower or "provider" in lower:
        action = (
            "Continue the same task after the transient provider/backend interruption. Do not restart the analysis."
        )
    elif "final response" in lower or "usable final" in lower:
        action = (
            "Continue from the preserved state and produce a valid completion only after the remaining work and checks "
            "are actually finished."
        )
    else:
        action = "Continue the unfinished candidate from the preserved state and complete the remaining work."

    delta_note = (
        f"The current RAM candidate already contains {changed} changed and {deleted} deleted paths relative to its "
        "start snapshot. Preserve useful work and verify/fix it; do not redo the task from scratch."
        if has_delta else
        "The current RAM candidate has no repository delta yet, so make the required implementation change before finishing."
    )
    return (
        f"AUTONOMOUS TEAM RESUME {attempt}/{_TEAM_CANDIDATE_MAX_AUTO_RESUMES}\n\n"
        f"Previous pause reason:\n{reason[:1800]}\n\n"
        f"{action}\n{delta_note}\n\n"
        "Stay inside this same isolated RAM workspace and continue with the existing conversation/tool evidence. "
        "Do not ask for human input or confirmation, and do not restart the analysis from the beginning. "
        "Finish only when the shared implementation contract is complete and the result is ready for deterministic "
        "candidate evaluation. Use `DONE:` for a genuine completion."
    )


def _is_incomplete_envelope_reason(reason: str) -> bool:
    text = str(reason or "").lower()
    return (
        "transient incomplete chat response" in text
        or "no recognized assistant response envelope" in text
        or "_transport_telemetry" in text
    )


def _fresh_worker_recovery_prompt(run: AgentRunResult, reason: str, attempt: int, *, label: str) -> str:
    """Create a bounded clean-chat handoff after a malformed provider response."""
    rows: list[str] = []
    for message in _candidate_conversation(run):
        role = str(message.get("role") or "unknown").upper()
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        else:
            try:
                text = json.dumps(content, ensure_ascii=False, default=str)
            except Exception:
                text = str(content or "")
        if text:
            rows.append(f"[{role}]\n{text}")
    evidence = "\n\n".join(rows)
    if len(evidence) > 12000:
        evidence = evidence[:4000] + "\n\n[... bounded recovery handoff ...]\n\n" + evidence[-8000:]
    return (
        f"FRESH {label.upper()} RECOVERY CHAT {attempt}\n\n"
        "The previous provider response contained no usable assistant envelope. This is a NEW provider chat, "
        "but the same isolated workspace remains authoritative. Do not restart completed work. Reuse the bounded "
        "evidence below and continue the unfinished assignment.\n\n"
        f"RECOVERY REASON:\n{reason[:2000]}\n\n"
        f"PRIOR CONTEXT/EVIDENCE:\n{evidence or '(none)'}"
    )


def _candidate_conversation(run: AgentRunResult) -> list[dict[str, Any]]:
    """Carry the model/tool history forward without duplicating the old system prompt."""
    return [
        dict(message) for message in (run.messages or [])
        if isinstance(message, dict) and str(message.get("role") or "") != "system"
    ]


_MERGE_INCOMPLETE_MARKERS = (
    "merge could not be executed", "merge konnte nicht ausgeführt werden",
    "integration was not performed", "integration not performed",
    "verification was not performed", "verification not performed",
    "required verification failed", "verification: incomplete",
    "recovery_required", "recovery required", "persistence: blocked", "persistenz: blockiert",
)

def _merge_completion_contradiction(response: str) -> bool:
    lowered = str(response or "").lower()
    return any(marker in lowered for marker in _MERGE_INCOMPLETE_MARKERS)


def _merge_resume_prompt(run: AgentRunResult, attempt: int) -> str:
    """Continue a paused merge in the same integration workspace without losing evidence."""
    reason = str(run.response or run.error or "merge paused without a specific reason").strip()
    lower = reason.lower()
    if "verification" in lower or "verify" in lower:
        action = "Run the missing post-merge verification, fix any failures, then complete the integration."
    elif "same tool" in lower or "repeating" in lower or "without progress" in lower:
        action = "Do not repeat the blocked tool call. Use existing evidence and take the next concrete integration or verification step."
    elif "transient" in lower or "backend" in lower or "provider" in lower:
        action = "Continue the same merge after the transient interruption without restarting the analysis."
    elif "final response" in lower or "usable final" in lower:
        action = "Produce a valid completion only after the selected integration work and verification are actually finished."
    else:
        action = "Continue the unfinished merge from the preserved integration workspace and complete the remaining work."
    return (
        f"AUTONOMOUS MERGE RESUME {attempt}/{_TEAM_MERGE_MAX_AUTO_RESUMES}\n\n"
        f"Previous pause reason:\n{reason[:1800]}\n\n"
        f"{action}\n\n"
        "Stay in this same integration workspace. Preserve existing merged changes and candidate evidence. "
        "Do not ask for human confirmation, do not restart from scratch, and do not write to the protected source workspace. "
        "Finish with a concise DONE: summary only when the integrated result is ready for deterministic final verification."
    )


def _run_candidate(
    *, client, model_client: ModelTransport, source_workspace: str, backend_mode: str,
    slot: int, model: str, strategy: str, task: str, plan: str, coordinator: str,
    tools: list[dict], stop_requested: StopFn | None, native_openrouter_tool_calling: bool = False,
    request_timeout: int = 300, event_fn: EventFn | None = None, liveness_timeout_s: int = 1200,
    stage_handoffs: dict[str, Any] | None = None,
) -> CandidateResult:
    backend = create_isolated_team_workspace(source_workspace, backend_mode)
    try:
        backend.prepare()
        if not isinstance(backend, RamWorkspace):
            raise WorkspaceError("parallel candidate runtime requires a transactional isolated workspace")
    except Exception:
        backend.abort()
        raise
    try:
        test_python = configured_project_python(backend.info.execution_root)
        test_runtime_note = (
            f"\n\nPROJECT TEST RUNTIME\n- Use the `test` tool for Python test execution. "
            f"It is configured to use {test_python}.\n"
            "- Do not install pytest/pip/system packages merely because `pytest` or system Python lacks dependencies. "
            "Do not use apt/pip/sudo to repair the test runner.\n"
            if test_python else ""
        )
        system = (
            build_system_prompt(tools, str(backend.info.execution_root)).rstrip()
            + "\n\n" + CODER_SYSTEM_TEMPLATE.format(slot=slot, strategy=strategy)
            + test_runtime_note
        )
        started = time.monotonic()
        liveness_deadline = started + max(60, int(liveness_timeout_s))
        worker_role = f"coder:{slot}"
        backend.write_candidate_artifact(
            ".aicoder-team/coder-handoff.json",
            json.dumps({"task": task, "implementation_contract": plan, "strategy": strategy}, ensure_ascii=False, indent=2),
        )
        if stage_handoffs:
            backend.write_candidate_artifact(
                ".aicoder-team/handoffs.json",
                json.dumps(stage_handoffs, ensure_ascii=False, indent=2),
            )
        prompt = _candidate_prompt(task, plan, coordinator, strategy)
        forward = _worker_event_forwarder(event_fn, worker_role)
        conversation: list[dict[str, Any]] = []
        run: AgentRunResult | None = None
        auto_resumes = 0

        while True:
            delta = backend.delta_summary()
            has_existing_delta = bool(
                int(delta.get("changed_count") or 0) or int(delta.get("deleted_count") or 0)
            )
            runtime = NativeLightRuntime(
                client=client, model_client=model_client,
                initial_prompt=prompt,
                model=model, fallback_model=None, workspace_root=str(backend.info.execution_root),
                plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
                tools=tools, system_prompt=system, load_tools_on_start=True,
                quick_chat=False, persistent_plan=False, approval_fn=_candidate_approval,
                max_iterations=None, max_output_tokens=12000,
                stop_requested=lambda: bool((stop_requested and stop_requested()) or time.monotonic() >= liveness_deadline),
                base_timeout=max(10, min(300, int(request_timeout))), event_fn=forward, conversation=conversation,
                require_mutation_or_explicit_no_change=not has_existing_delta,
                require_test_verification=True, allow_completion_signal=True,
                native_openrouter_tool_calling=bool(native_openrouter_tool_calling),
            )
            run = runtime.run()
            if time.monotonic() >= liveness_deadline and not (stop_requested and stop_requested()) and run.status != "completed":
                reason = f"candidate liveness timeout after {int(liveness_timeout_s)}s without terminal completion"
                run.status = "failed"; run.response = reason; run.error = reason
                _emit(event_fn, "team_worker_event", role=worker_role, event="runtime_status", category="liveness",
                      status="failed", phase="candidate_timeout", message=reason)
            if not _candidate_pause_is_resumable(run, stop_requested):
                break
            if auto_resumes >= _TEAM_CANDIDATE_MAX_AUTO_RESUMES:
                break
            auto_resumes += 1
            reason = str(run.response or run.error or "")
            delta = backend.delta_summary()
            if _is_incomplete_envelope_reason(reason):
                prompt = _fresh_worker_recovery_prompt(run, reason, auto_resumes, label=worker_role)
                conversation = []
                _emit(event_fn, "team_worker_event", role=worker_role, event="runtime_status",
                      category="recovery", status="fresh_chat", phase="candidate_resume",
                      message=f"starting fresh provider chat after incomplete response envelope ({auto_resumes}/{_TEAM_CANDIDATE_MAX_AUTO_RESUMES})")
            else:
                conversation = _candidate_conversation(run)
                prompt = _candidate_resume_prompt(run, delta, auto_resumes)

        assert run is not None
        if hasattr(run, "performance") and isinstance(run.performance, dict):
            run.performance.setdefault("team_auto_resumes", auto_resumes)
        return CandidateResult(
            slot=slot, model=model, strategy=strategy, workspace=backend, run=run,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception:
        backend.abort()
        raise


def _git_diff(root: Path) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(root), "diff", "--no-ext-diff", "--binary"], capture_output=True, text=True, timeout=20)
        untracked = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"], capture_output=True, text=True, timeout=10)
        text = proc.stdout
        for rel in untracked.stdout.splitlines()[:120]:
            path = root / rel
            if path.is_file() and path.stat().st_size <= 200_000:
                try:
                    content = path.read_text(encoding="utf-8")
                except Exception:
                    continue
                text += f"\n--- /dev/null\n+++ b/{rel}\n" + "\n".join("+" + line for line in content.splitlines()) + "\n"
        return text[:100_000]
    except Exception as exc:
        return f"diff unavailable: {exc}"


def _run_check(root: Path, command: list[str], timeout: int = 90) -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0, "exit_code": proc.returncode,
            "elapsed_ms": int((time.monotonic()-started)*1000),
            "output": (proc.stdout + "\n" + proc.stderr)[-6000:],
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "exit_code": -1, "elapsed_ms": int((time.monotonic()-started)*1000), "output": str(exc)}


def evaluate_candidate(candidate: CandidateResult) -> dict[str, Any]:
    root = Path(candidate.workspace.info.execution_root)
    delta = candidate.workspace.delta_summary() if isinstance(candidate.workspace, RamWorkspace) else {}
    plan = project_verification_plan(root)
    results = execute_verification_plan(root, plan)
    checks = {row.name: row.as_dict() for row in results}
    passed = sum(1 for row in results if row.ok and row.required)
    failed = sum(1 for row in results if (not row.ok) and row.required)
    score = (40 if candidate.run.status == "completed" else 0) + passed * 25 - failed * 60
    if delta.get("changed_count", 0) or delta.get("deleted_count", 0):
        score += 10
    if candidate.run.error:
        score -= 20
    diff = candidate.workspace.delta_diff() if isinstance(candidate.workspace, RamWorkspace) else _git_diff(root)
    coverage = test_change_evidence(delta)
    deterministic_ok = verification_passed(results)
    coverage_ok = bool(coverage.get("coverage_evidence_ok"))
    if not coverage_ok:
        score -= 120
        checks["test-change-evidence"] = {
            "name": "test-change-evidence", "ok": False, "required": True,
            "output": "behavior-changing source code requires a changed or newly created regression test",
            **coverage,
        }
    return {
        "score": score, "delta": delta, "checks": checks, "diff": diff,
        "test_evidence": coverage, "candidate_id": blind_candidate_id(diff),
        "verification_passed": deterministic_ok and coverage_ok,
    }


def _candidate_is_mergeable(candidate: CandidateResult) -> bool:
    return candidate.run.status == "completed" and bool(candidate.evaluation.get("verification_passed"))


def _evaluation_prompt(candidates: list[CandidateResult]) -> str:
    rows = []
    for c in sorted(candidates, key=lambda item: item.slot):
        rows.append(json.dumps({
            "slot": c.slot, "model": c.model, "strategy": c.strategy,
            "run_status": c.run.status, "score": c.score,
            "evaluation": {k: v for k, v in c.evaluation.items() if k != "diff"},
            "summary": c.run.response[:5000], "diff": c.evaluation.get("diff", "")[:25000],
        }, ensure_ascii=False))
    return "\n\n".join(rows)



def _stage_start(ledger: StageLedger, stage: TeamStage, event_fn: EventFn | None) -> None:
    ledger.start(stage)
    _emit(event_fn, "team_pipeline", stage=stage.value, status="started", ledger=ledger.as_dict())


def _stage_complete(ledger: StageLedger, stage: TeamStage, event_fn: EventFn | None) -> None:
    ledger.complete(stage)
    _emit(event_fn, "team_pipeline", stage=stage.value, status="completed", ledger=ledger.as_dict())


def _cleanup_team_workspaces(
    candidates: list[CandidateResult],
    integration: WorkspaceBackend | None,
    event_fn: EventFn | None = None,
) -> None:
    """Release every isolated workspace owned by one team job.

    abort() is idempotent, so this is safe after successful finalize() as well as
    on any terminal failure or unexpected exception.
    """
    released = 0
    seen: set[int] = set()
    workspaces: list[WorkspaceBackend] = [candidate.workspace for candidate in candidates]
    if integration is not None:
        workspaces.append(integration)
    for workspace in workspaces:
        marker = id(workspace)
        if marker in seen:
            continue
        seen.add(marker)
        try:
            workspace.abort()
            released += 1
        except Exception:
            pass
    _emit(event_fn, "team_ram_cleanup", released=released)


def _link_or_copy(src: str, dst: str) -> str:
    try:
        os.link(src, dst)
        return dst
    except OSError:
        return shutil.copy2(src, dst)


def _attach_blind_candidate_snapshots(integration: RamWorkspace, candidates: list[CandidateResult]) -> list[dict[str, Any]]:
    base = integration.info.execution_root / ".aicoder-team" / "candidates"
    base.mkdir(parents=True, exist_ok=True)
    evidence: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: str(item.evaluation.get("candidate_id") or "")):
        cid = str(candidate.evaluation.get("candidate_id") or blind_candidate_id(candidate.evaluation.get("diff", "")))
        target = base / cid
        shutil.copytree(
            candidate.workspace.info.execution_root, target, symlinks=True,
            ignore=shutil.ignore_patterns(".git", ".aicoder-team", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"),
            copy_function=_link_or_copy, dirs_exist_ok=True,
        )
        delta = candidate.evaluation.get("delta") or {}
        snapshot_rel = f".aicoder-team/candidates/{cid}"
        manifest_rel = f".aicoder-team/candidates/{cid}.changes.json"
        integration.write_candidate_artifact(
            manifest_rel,
            json.dumps({
                "candidate_id": cid,
                "snapshot": snapshot_rel,
                "added_files": list(delta.get("added_files") or []),
                "modified_files": list(delta.get("modified_files") or []),
                "deleted_files": list(delta.get("deleted_files") or []),
            }, ensure_ascii=False, indent=2),
        )
        evidence.append({
            "candidate_id": cid,
            "score": int(candidate.evaluation.get("score") or 0),
            "verification_passed": bool(candidate.evaluation.get("verification_passed")),
            "checks": candidate.evaluation.get("checks") or {},
            "delta": delta,
            "diff": str(candidate.evaluation.get("diff") or "")[:50000],
            "snapshot": snapshot_rel,
            "change_manifest": manifest_rel,
        })
    integration.write_candidate_artifact(
        ".aicoder-team/candidates.json", json.dumps(evidence, ensure_ascii=False, indent=2)
    )
    return evidence


def _blind_merge_prompt(task: str, code_plan: str, evidence: list[dict[str, Any]]) -> str:
    task_handoff = _task_handoff(task)
    code_handoff = _code_plan_handoff(code_plan)
    compact = _compact_candidate_evidence(evidence)
    evidence_text = json.dumps(compact, ensure_ascii=False, indent=2)
    evidence_handoff = make_handoff("candidate-evidence", evidence_text, max_chars=30000)
    return (
        f"USER TASK:\n{task_handoff.render()}\n\n"
        f"SHARED CODE CONTRACT:\n{code_handoff.render()}\n\n"
        f"ANONYMIZED CANDIDATE EVIDENCE:\n{evidence_handoff.render()}"
    )


def run_team(
    *, task: str, state: dict[str, Any], config: TeamConfig, client,
    model_client: ModelTransport, source_workspace: str,
    event_fn: EventFn | None = None, stop_requested: StopFn | None = None,
) -> TeamRunResult:
    """Run the team pipeline and always publish one post-cleanup terminal event."""
    run_id = f"team-{uuid.uuid4().hex[:16]}"
    run_started = time.monotonic()
    events = _event_with_debug(event_fn, _TeamDebugLog(run_id))
    try:
        result = _run_team_pipeline(
            task=task, state=state, config=config, client=client,
            model_client=model_client, source_workspace=source_workspace,
            event_fn=events, stop_requested=stop_requested,
        )
        if result.status != "completed" and stop_requested is not None and stop_requested():
            result.status = "cancelled"
            result.error = result.error or "team run cancelled by user"
    except KeyboardInterrupt:
        result = TeamRunResult(
            "cancelled", "", "", [], [], {}, "team run cancelled by user"
        )
    except Exception as exc:
        result = TeamRunResult(
            "failed", "", "", [], [], {}, f"{type(exc).__name__}: {exc}"
        )
    elapsed_ms = int((time.monotonic() - run_started) * 1000)
    ledger = result.performance.get("ledger", {}) if isinstance(result.performance, dict) else {}
    _emit(
        events, "team_terminal", status=result.status,
        progress=100 if result.status == "completed" else None,
        elapsed_ms=elapsed_ms, error=result.error, ledger=ledger,
    )
    return result


def _run_team_pipeline(
    *, task: str, state: dict[str, Any], config: TeamConfig, client,
    model_client: ModelTransport, source_workspace: str,
    event_fn: EventFn | None = None, stop_requested: StopFn | None = None,
) -> TeamRunResult:
    errors = config.validate()
    if errors:
        return TeamRunResult("failed", "", "", [], [], {}, "; ".join(errors))
    try:
        resolved_workspace, auto_selected, workspace_reason = resolve_or_create_project_workspace(
            source_workspace, task, state.get("projects_root")
        )
        source_workspace = str(resolved_workspace)
        if auto_selected:
            from .session_state import set_workspace

            set_workspace(source_workspace)
            state["workspace_root"] = source_workspace
            _emit(
                event_fn, "team_project_workspace", path=source_workspace,
                auto_selected=True, reason=workspace_reason,
            )
    except (OSError, ValueError) as exc:
        return TeamRunResult("failed", "", "", [], [], {}, f"project workspace setup failed: {exc}")

    try:
        request_timeout = max(10, min(300, int(state.get("request_timeout") or 300)))
    except (TypeError, ValueError):
        request_timeout = 300
    started = time.monotonic()
    ledger = StageLedger()
    stages: list[AgentStageResult] = []
    candidates: list[CandidateResult] = []
    handoff_metrics: list[dict[str, int | str]] = []
    handoff_archive: dict[str, dict[str, Any]] = {}
    all_tools = load_tools(client)
    research_tools = _filtered_tools(all_tools, _RESEARCH_TOOL_NAMES)
    coder_tools = _filtered_tools(all_tools, _CODER_TOOL_NAMES)
    _emit(event_fn, "team_start", agents=config.active_count, research=len(config.research), coders=len(config.coders))

    # 1) plan_research
    _stage_start(ledger, TeamStage.PLAN_RESEARCH, event_fn)
    research_planner_model = config.coordinator_model or config.planner_model or ""
    research_plan = _call_advisor(
        model_client, model=research_planner_model, system=RESEARCH_PLANNER_SYSTEM_PROMPT,
        prompt=(
            f"USER TASK:\n{_task_handoff(task).render()}\n\n"
            f"REPOSITORY CONTEXT:\n{make_handoff('repository-context', _repository_context(source_workspace), max_chars=5000).render()}"
        ),
        max_tokens=3000, event_fn=event_fn, role="plan_research", stop_requested=stop_requested,
    )
    research_plan.role = "plan_research"; stages.append(research_plan)
    if research_plan.status != "completed":
        return TeamRunResult("failed", "", research_plan.model, stages, [], {"ledger": ledger.as_dict()}, research_plan.error)
    research_contract_handoff = _research_plan_handoff(research_plan.response)
    handoff_metrics.append(research_contract_handoff.metrics())
    handoff_archive[research_contract_handoff.handoff_id] = {
        "kind": research_contract_handoff.kind, "raw": research_contract_handoff.raw,
        "compact": research_contract_handoff.compact,
    }
    _stage_complete(ledger, TeamStage.PLAN_RESEARCH, event_fn)

    # 2) research
    _stage_start(ledger, TeamStage.RESEARCH, event_fn)
    research_results: list[AgentStageResult] = []
    if config.research:
        with ThreadPoolExecutor(max_workers=len(config.research), thread_name_prefix="aicoder-research") as pool:
            futures = {
                pool.submit(
                    _run_researcher, client=client, model_client=model_client, model=slot.model,
                    role=slot.role, task=task, source_workspace=source_workspace,
                    tools=research_tools, stop_requested=stop_requested,
                    research_plan=research_contract_handoff.compact,
                    native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)), event_fn=event_fn,
                    request_timeout=request_timeout,
                ): slot for slot in config.research
            }
            for future in as_completed(futures):
                slot = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = AgentStageResult(f"research:{slot.role}", slot.model, "failed", "", 0, f"{type(exc).__name__}: {exc}")
                research_results.append(result); stages.append(result)
                _emit(
                    event_fn, "team_stage", role=result.role, status=result.status, model=result.model,
                    elapsed_ms=result.elapsed_ms, error=result.error, evidence=result.evidence,
                )
    for item in research_results:
        report_handoff = make_handoff(
            f"{item.role}-report", item.response or item.error or "(no report)",
            max_chars=3500, section_labels=RESEARCH_SECTIONS,
        )
        handoff_metrics.append(report_handoff.metrics())
        handoff_archive[report_handoff.handoff_id] = {
            "kind": report_handoff.kind, "role": item.role, "status": item.status,
            "raw": report_handoff.raw, "compact": report_handoff.compact,
        }
    _stage_complete(ledger, TeamStage.RESEARCH, event_fn)

    # 3) brainstorm -- divergent multi-model reasoning after research, before implementation planning.
    _stage_start(ledger, TeamStage.BRAINSTORM, event_fn)
    brainstorm_results: list[AgentStageResult] = []
    brainstorm_state = ""
    brainstorm_participants = _brainstorm_participants(config)
    configured_rounds = _brainstorm_rounds(state)
    repo_context = _repository_context(source_workspace)
    research_handoff_text = _brainstorm_research_handoff(research_results)
    synthesis_model = config.coordinator_model or config.planner_model or ""
    _emit(
        event_fn, "team_brainstorm_config", rounds=configured_rounds,
        participants=len(brainstorm_participants),
    )
    for round_index in range(1, configured_rounds + 1):
        if not brainstorm_participants:
            break
        _emit(event_fn, "team_brainstorm_round", round=round_index, status="started", total_rounds=configured_rounds)
        system_prompt = BRAINSTORM_SYSTEM_PROMPT if round_index == 1 else BRAINSTORM_EVOLUTION_SYSTEM_PROMPT
        round_results: list[AgentStageResult] = []
        with ThreadPoolExecutor(max_workers=len(brainstorm_participants), thread_name_prefix=f"aicoder-brainstorm-r{round_index}") as pool:
            futures = {
                pool.submit(
                    _call_advisor, model_client, model=model, system=system_prompt,
                    prompt=_build_brainstorm_prompt(
                        task, repo_context, research_handoff_text, perspective,
                        round_index=round_index, brainstorm_state=brainstorm_state,
                    ),
                    max_tokens=4000, event_fn=event_fn, role=f"brainstorm:r{round_index}:{label}", stop_requested=stop_requested,
                ): (label, model)
                for label, model, perspective in brainstorm_participants
            }
            for future in as_completed(futures):
                label, model = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = AgentStageResult(
                        f"brainstorm:r{round_index}:{label}", model, "failed", "", 0,
                        f"{type(exc).__name__}: {exc}",
                    )
                result.role = f"brainstorm:r{round_index}:{label}"
                round_results.append(result)
                brainstorm_results.append(result)
                stages.append(result)
                _emit(
                    event_fn, "team_stage", role=result.role, status=result.status, model=result.model,
                    elapsed_ms=result.elapsed_ms, error=result.error, evidence=result.evidence,
                )
        usable = [item for item in round_results if item.status == "completed" and item.response.strip()]
        if not usable:
            _emit(event_fn, "team_brainstorm_round", round=round_index, status="empty", total_rounds=configured_rounds)
            break
        operator = _call_advisor(
            model_client, model=synthesis_model, system=BRAINSTORM_OPERATOR_SYSTEM_PROMPT,
            prompt=_build_brainstorm_operator_prompt(task, round_index, usable, brainstorm_state),
            max_tokens=5000, event_fn=event_fn, role=f"brainstorm_state:r{round_index}", stop_requested=stop_requested,
        )
        operator.role = f"brainstorm_state:r{round_index}"
        stages.append(operator)
        if operator.status != "completed" or not operator.response.strip():
            _emit(event_fn, "team_brainstorm_round", round=round_index, status="operator_failed", total_rounds=configured_rounds)
            break
        brainstorm_state = operator.response
        _emit(
            event_fn, "team_brainstorm_round", round=round_index, status="completed",
            proposals=len(usable), total_rounds=configured_rounds,
        )

    if brainstorm_results:
        brainstorm_synthesis = _call_advisor(
            model_client, model=synthesis_model, system=BRAINSTORM_SYNTHESIS_SYSTEM_PROMPT,
            prompt=_build_brainstorm_synthesis_prompt(task, brainstorm_state, brainstorm_results),
            max_tokens=6000, event_fn=event_fn, role="brainstorm_synthesis", stop_requested=stop_requested,
        )
        brainstorm_synthesis.role = "brainstorm_synthesis"
        stages.append(brainstorm_synthesis)
        if brainstorm_synthesis.status != "completed":
            _emit(event_fn, "team_worker_event", role="brainstorm_synthesis", event="runtime_status",
                  category="brainstorm", status="warning", phase="synthesis",
                  message=f"brainstorm synthesis unavailable; planner continues from research evidence: {brainstorm_synthesis.error[:500]}")
            brainstorm_contract_handoff = _brainstorm_handoff(
                "Brainstorm synthesis unavailable. Treat creative ideas as unavailable and plan strictly from the research evidence and user task."
            )
        else:
            brainstorm_contract_handoff = _brainstorm_handoff(brainstorm_synthesis.response)
    else:
        brainstorm_synthesis = AgentStageResult(
            "brainstorm_synthesis", synthesis_model or "deterministic", "completed",
            "No distinct brainstorm participants were available; proceed using research evidence only.", 0,
        )
        stages.append(brainstorm_synthesis)
        brainstorm_contract_handoff = _brainstorm_handoff(brainstorm_synthesis.response)
    handoff_metrics.append(brainstorm_contract_handoff.metrics())
    handoff_archive[brainstorm_contract_handoff.handoff_id] = {
        "kind": brainstorm_contract_handoff.kind, "raw": brainstorm_contract_handoff.raw,
        "compact": brainstorm_contract_handoff.compact,
    }
    _stage_complete(ledger, TeamStage.BRAINSTORM, event_fn)

    # 4) plan_code
    _stage_start(ledger, TeamStage.PLAN_CODE, event_fn)
    code_plan = _call_advisor(
        model_client, model=config.planner_model or "", system=PLANNER_SYSTEM_PROMPT,
        prompt=_build_planner_prompt(task, repo_context, research_results)
        + "\n\nRESEARCH CONTRACT:\n" + research_contract_handoff.render()
        + "\n\nBRAINSTORM SYNTHESIS (creative decision support, not evidence):\n" + brainstorm_contract_handoff.render(),
        max_tokens=6500, event_fn=event_fn, role="plan_code", stop_requested=stop_requested,
    )
    code_plan.role = "plan_code"
    stages.append(code_plan)
    if code_plan.status == "completed":
        code_contract_handoff = _code_plan_handoff(code_plan.response)
        handoff_metrics.append(code_contract_handoff.metrics())
        handoff_archive[code_contract_handoff.handoff_id] = {
            "kind": code_contract_handoff.kind, "raw": code_contract_handoff.raw,
            "compact": code_contract_handoff.compact,
        }
    else:
        code_contract_handoff = _code_plan_handoff(code_plan.response or code_plan.error)
    if code_plan.status != "completed":
        return TeamRunResult("failed", "", code_plan.model, stages, [], {"ledger": ledger.as_dict()}, code_plan.error)
    _stage_complete(ledger, TeamStage.PLAN_CODE, event_fn)

    coordination_notes = ""
    if config.coordinator_model:
        coordinator_result = _call_advisor(
            model_client, model=config.coordinator_model, system=COORDINATOR_SYSTEM_PROMPT,
            prompt=(
                f"USER TASK:\n{_task_handoff(task).render()}\n\n"
                f"SHARED IMPLEMENTATION CONTRACT:\n{code_contract_handoff.render()}\n\n"
                "Review only for ambiguity, missing acceptance criteria, unsafe assumptions, and candidate execution hazards. "
                "Return concise coordination notes; do not redesign or implement."
            ),
            max_tokens=2500, event_fn=event_fn, role="coordinator", stop_requested=stop_requested,
        )
        coordinator_result.role = "coordinator"
        stages.append(coordinator_result)
        _emit(event_fn, "team_stage", role="coordinator", status=coordinator_result.status,
              model=coordinator_result.model, elapsed_ms=coordinator_result.elapsed_ms, error=coordinator_result.error,
              evidence=coordinator_result.evidence)
        if coordinator_result.status == "completed":
            coordination_notes = coordinator_result.response
        else:
            _emit(event_fn, "team_worker_event", role="coordinator", event="runtime_status", category="coordination",
                  status="warning", phase="pre_code", message="coordinator unavailable; candidates continue with shared contract")

    candidate_handoffs = {
        "research_contract": research_contract_handoff.compact,
        "brainstorm_synthesis": brainstorm_contract_handoff.compact,
        "code_contract": code_contract_handoff.compact,
        "coordination_notes": coordination_notes,
    }

    # 5) code — isolated parallel candidates with one fair global backing mode.
    workspace_plan = team_workspace_plan(
        source_workspace, len(config.coders), str(state.get("workspace_mode") or "auto")
    )
    _emit(event_fn, "team_workspace_plan", **workspace_plan.as_dict())
    integration: WorkspaceBackend | None = None
    futures: dict[Any, Any] = {}
    try:
        _stage_start(ledger, TeamStage.CODE, event_fn)
        with ThreadPoolExecutor(max_workers=len(config.coders), thread_name_prefix="aicoder-coder") as pool:
            futures = {
                pool.submit(
                    _run_candidate, client=client, model_client=model_client, source_workspace=source_workspace,
                    backend_mode=workspace_plan.backend_mode, slot=slot.slot, model=slot.model,
                    strategy=slot.strategy, task=task, plan=code_contract_handoff.compact, coordinator=coordination_notes,
                    tools=coder_tools, stop_requested=stop_requested,
                    native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
                    request_timeout=request_timeout, event_fn=event_fn,
                    liveness_timeout_s=int(state.get("team_candidate_liveness_timeout_seconds") or 1200),
                    stage_handoffs=candidate_handoffs,
                ): slot for slot in config.coders
            }
            for future in as_completed(futures):
                slot = futures[future]
                candidate: CandidateResult | None = None
                try:
                    candidate = future.result()
                    evaluation_started = time.monotonic()
                    candidate.evaluation = evaluate_candidate(candidate)
                    candidate.evaluation_ms = int((time.monotonic() - evaluation_started) * 1000)
                    candidate.score = int(candidate.evaluation.get("score") or 0)
                    candidates.append(candidate)
                    failed_checks = [name for name, row in (candidate.evaluation.get("checks") or {}).items() if isinstance(row, dict) and row.get("required", True) and not row.get("ok")]
                    delta = candidate.evaluation.get("delta") or {}
                    _emit(event_fn, "team_candidate", candidate_id=candidate.evaluation.get("candidate_id"),
                          slot=slot.slot, model=candidate.run.model or slot.model, strategy=slot.strategy,
                          status=candidate.run.status, score=candidate.score, error=candidate.run.error,
                          iterations=candidate.run.iterations, verification_passed=bool(candidate.evaluation.get("verification_passed")),
                          failed_checks=failed_checks, changed_count=int(delta.get("changed_count") or 0),
                          deleted_count=int(delta.get("deleted_count") or 0),
                          elapsed_ms=candidate.elapsed_ms, evaluation_ms=candidate.evaluation_ms)
                except Exception as exc:
                    if candidate is not None:
                        candidate.workspace.abort()
                    _emit(event_fn, "team_candidate", candidate_id="failed", status="failed", score=-999,
                          error=f"{type(exc).__name__}: {exc}")
        viable = [candidate for candidate in candidates if _candidate_is_mergeable(candidate)]
        if not viable:
            return TeamRunResult("failed", "", "", stages, candidates, {"ledger": ledger.as_dict()}, "no verified coding candidate completed")
        winner = max(viable, key=lambda item: objective_rank_key(item.evaluation))
        _stage_complete(ledger, TeamStage.CODE, event_fn)

        # Build fresh integration workspace and attach anonymized full snapshots.
        integration = create_isolated_team_workspace(source_workspace, workspace_plan.backend_mode)
        integration.prepare()
        if not isinstance(integration, RamWorkspace):
            return TeamRunResult("failed", "", "", stages, candidates, {"ledger": ledger.as_dict()}, "integration requires transactional isolation")
        _emit(
            event_fn, "team_integration_workspace", mode=integration.info.mode,
            fallback_reason=integration.info.fallback_reason,
        )
        integration.seed_from(winner.workspace.info.execution_root)
        blind_evidence = _attach_blind_candidate_snapshots(integration, viable)
        integration.write_candidate_artifact(
            ".aicoder-team/handoffs.json",
            json.dumps(handoff_archive, ensure_ascii=False, indent=2),
        )
        winner_id = str(winner.evaluation.get("candidate_id"))

        # 5) merge_plan — blind to model/provider/slot identity.
        _stage_start(ledger, TeamStage.MERGE_PLAN, event_fn)
        merge_planner_model = config.coordinator_model or config.planner_model or ""
        merge_plan = _call_advisor(
            model_client, model=merge_planner_model, system=MERGE_PLANNER_SYSTEM_PROMPT,
            prompt=_blind_merge_prompt(task, code_contract_handoff.compact, blind_evidence)
            + f"\n\nDETERMINISTIC BASE CANDIDATE: {winner_id}",
            max_tokens=4000, event_fn=event_fn, role="merge_plan", stop_requested=stop_requested,
        )
        merge_plan.role = "merge_plan"; stages.append(merge_plan)
        if merge_plan.status == "completed":
            merge_contract_handoff = _merge_plan_handoff(merge_plan.response)
            handoff_metrics.append(merge_contract_handoff.metrics())
            handoff_archive[merge_contract_handoff.handoff_id] = {
                "kind": merge_contract_handoff.kind, "raw": merge_contract_handoff.raw,
                "compact": merge_contract_handoff.compact,
            }
        else:
            merge_contract_handoff = _merge_plan_handoff(merge_plan.response or merge_plan.error)
        if merge_plan.status != "completed":
            return TeamRunResult("failed", "", merge_plan.model, stages, candidates, {"ledger": ledger.as_dict()}, merge_plan.error)
        integration.write_candidate_artifact(".aicoder-team/merge-plan.txt", merge_plan.response)
        integration.write_candidate_artifact(
            ".aicoder-team/handoffs.json",
            json.dumps(handoff_archive, ensure_ascii=False, indent=2),
        )
        _stage_complete(ledger, TeamStage.MERGE_PLAN, event_fn)

        # 6) merge — optional LLM. Empty merge slot means deterministic winner only.
        _stage_start(ledger, TeamStage.MERGE, event_fn)
        merge_model = config.merge_model
        if merge_model:
            merge_prompt = (
                f"USER TASK:\n{_task_handoff(task).render()}\n\n"
                f"CODE CONTRACT:\n{code_contract_handoff.render()}\n\n"
                f"BLIND MERGE CONTRACT:\n{merge_contract_handoff.render()}\n\n"
                "Candidate snapshots are under .aicoder-team/candidates/. Integrate only evidence-backed improvements."
            )
            merge_system = build_system_prompt(coder_tools, str(integration.info.execution_root)).rstrip()+"\n\n"+MERGE_SYSTEM_PROMPT
            merge_conversation: list[dict[str, Any]] = []
            merge_auto_resumes = 0
            merge_run: AgentRunResult | None = None
            merge_started = time.monotonic()
            while True:
                merge_runtime = NativeLightRuntime(
                    client=client, model_client=model_client,
                    initial_prompt=merge_prompt,
                    model=merge_model, fallback_model=None, workspace_root=str(integration.info.execution_root),
                    plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
                    tools=coder_tools, system_prompt=merge_system,
                    load_tools_on_start=True, quick_chat=False, persistent_plan=False,
                    approval_fn=_candidate_approval, max_iterations=14, max_output_tokens=10000, stop_requested=stop_requested,
                    base_timeout=request_timeout, conversation=merge_conversation, allow_completion_signal=True,
                    event_fn=_worker_event_forwarder(event_fn, "merge"),
                    native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
                )
                merge_run = merge_runtime.run()
                if merge_run.status == "completed" and _merge_completion_contradiction(merge_run.response):
                    reason = "merge self-reported incomplete integration or verification; continue the same integration workspace"
                    merge_run.status = "paused"; merge_run.response = reason; merge_run.error = reason
                if not _candidate_pause_is_resumable(merge_run, stop_requested):
                    break
                if merge_auto_resumes >= _TEAM_MERGE_MAX_AUTO_RESUMES:
                    break
                merge_auto_resumes += 1
                merge_pause_reason = str(merge_run.response or merge_run.error or "merge paused")
                _emit(
                    event_fn, "team_merge_resume", attempt=merge_auto_resumes,
                    reason=merge_pause_reason[:2000],
                )
                if _is_incomplete_envelope_reason(merge_pause_reason):
                    merge_conversation = []
                    merge_prompt = _fresh_worker_recovery_prompt(merge_run, merge_pause_reason, merge_auto_resumes, label="merge")
                    _emit(event_fn, "team_worker_event", role="merge", event="runtime_status",
                          category="recovery", status="fresh_chat", phase="merge_resume",
                          message=f"starting fresh provider chat after incomplete response envelope ({merge_auto_resumes}/{_TEAM_MERGE_MAX_AUTO_RESUMES})")
                else:
                    merge_conversation = _candidate_conversation(merge_run)
                    merge_prompt = _merge_resume_prompt(merge_run, merge_auto_resumes)

            assert merge_run is not None
            merge_elapsed = int((time.monotonic() - merge_started) * 1000)
            merge_reason = str(merge_run.error or merge_run.response or "merge failed").strip()
            _emit(
                event_fn, "team_merge_result", status=merge_run.status, model=merge_run.model or merge_model,
                elapsed_ms=merge_elapsed, auto_resumes=merge_auto_resumes,
                reason=(merge_reason[:4000] if merge_run.status != "completed" else ""),
            )
            stages.append(AgentStageResult(
                "merge", merge_run.model or merge_model, merge_run.status, merge_run.response,
                merge_elapsed, (merge_reason if merge_run.status != "completed" else merge_run.error),
                evidence={"auto_resumes": merge_auto_resumes},
            ))
            if merge_run.status != "completed":
                return TeamRunResult(
                    "failed", "", merge_run.model, stages, candidates, {"ledger": ledger.as_dict()},
                    merge_reason or "merge failed",
                )
            final_response = merge_run.response
            result_model = merge_run.model or merge_model
        else:
            stages.append(AgentStageResult("merge", "deterministic", "completed", f"Selected {winner_id} without LLM merge", 0))
            final_response = f"Selected verified base candidate {winner_id}."
            result_model = winner.run.model
        _stage_complete(ledger, TeamStage.MERGE, event_fn)

        # 7) plan_tests — model may explain/extend intent, deterministic commands remain authoritative.
        _stage_start(ledger, TeamStage.PLAN_TESTS, event_fn)
        deterministic_plan = project_verification_plan(integration.info.execution_root)
        test_plan_text = json.dumps([
            {"name": item.name, "argv": list(item.argv), "timeout": item.timeout, "required": item.required}
            for item in deterministic_plan
        ], ensure_ascii=False, indent=2)
        if config.test_planner_model:
            test_plan = _call_advisor(
                model_client, model=config.test_planner_model, system=TEST_PLANNER_SYSTEM_PROMPT,
                prompt=(
                    f"USER TASK:\n{_task_handoff(task).render()}\n\n"
                    f"CODE CONTRACT:\n{code_contract_handoff.render()}\n\n"
                    f"MERGE CONTRACT:\n{merge_contract_handoff.render()}\n\n"
                    f"DETERMINISTIC REPOSITORY CHECKS (authoritative):\n{make_handoff('deterministic-checks', test_plan_text, max_chars=6000).render()}"
                ),
                max_tokens=3000, event_fn=event_fn, role="plan_tests", stop_requested=stop_requested,
            )
            test_plan.role = "plan_tests"; stages.append(test_plan)
            if test_plan.status != "completed":
                _emit(event_fn, "team_worker_event", role="plan_tests", event="runtime_status",
                      category="test_planner", status="warning", phase="plan_tests",
                      message=f"test planner unavailable; deterministic verification remains authoritative: {test_plan.error[:500]}")
            else:
                integration.write_candidate_artifact(".aicoder-team/test-plan.txt", test_plan.response)
        else:
            stages.append(AgentStageResult("plan_tests", "deterministic", "completed", test_plan_text, 0))
        _stage_complete(ledger, TeamStage.PLAN_TESTS, event_fn)

        # 8) tests_function_ok — only executable evidence can open the disk-write gate.
        _stage_start(ledger, TeamStage.TESTS_FUNCTION_OK, event_fn)
        verification_results = execute_verification_plan(integration.info.execution_root, deterministic_plan)
        verification_payload = [item.as_dict() for item in verification_results]
        integration.write_candidate_artifact(".aicoder-team/final-verification.json", json.dumps(verification_payload, ensure_ascii=False, indent=2))
        if not verification_passed(verification_results):
            return TeamRunResult(
                "failed", "", result_model, stages, candidates,
                {"ledger": ledger.as_dict(), "verification": verification_payload},
                "tests_function_ok gate failed; persistent workspace was not modified",
            )
        _stage_complete(ledger, TeamStage.TESTS_FUNCTION_OK, event_fn)

        # 9) atomic_disk_write — the only persistent mutation stage.
        _stage_start(ledger, TeamStage.ATOMIC_DISK_WRITE, event_fn)
        final_delta = integration.delta_summary()
        change_manifest = {
            "created": list(final_delta.get("added_files") or []),
            "modified": list(final_delta.get("modified_files") or []),
            "deleted": list(final_delta.get("deleted_files") or []),
        }
        _emit(event_fn, "team_change_manifest", **change_manifest)
        integration.finalize(verified=True)
        _stage_complete(ledger, TeamStage.ATOMIC_DISK_WRITE, event_fn)

        wall_ms = int((time.monotonic() - started) * 1000)
        accumulated_agent_ms = sum(stage.elapsed_ms for stage in stages) + sum(
            candidate.elapsed_ms + candidate.evaluation_ms for candidate in candidates
        )
        perf = {
            "wall_ms": wall_ms,
            "accumulated_agent_ms": accumulated_agent_ms,
            "parallelism": round(accumulated_agent_ms / wall_ms, 2) if wall_ms else 0.0,
            "research_agents": len(research_results), "coding_candidates": len(candidates),
            "winner_candidate_id": winner_id, "winner_score": winner.score,
            "workspace_plan": workspace_plan.as_dict(),
            "integration_workspace_mode": integration.info.mode,
            "handoffs": handoff_metrics,
            "handoff_original_chars": sum(int(item.get("original_chars") or 0) for item in handoff_metrics),
            "handoff_compact_chars": sum(int(item.get("compact_chars") or 0) for item in handoff_metrics),
            "handoff_saved_chars": sum(int(item.get("saved_chars") or 0) for item in handoff_metrics),
            "advisor_prompt_chars": sum(
                int((stage.evidence or {}).get("prompt_chars") or 0) for stage in stages
            ),
            "advisor_response_chars": sum(
                int((stage.evidence or {}).get("response_chars") or 0) for stage in stages
            ),
            "ledger": ledger.as_dict(), "verification": verification_payload,
            "change_manifest": change_manifest,
            "stage_timings": [
                {"role": stage.role, "model": stage.model, "status": stage.status, "elapsed_ms": stage.elapsed_ms}
                for stage in stages
            ],
        }
        _emit(event_fn, "team_complete", **perf)
        return TeamRunResult("completed", final_response, result_model or "", stages, candidates, perf)
    finally:
        # On Ctrl+C/SIGTERM the executor may have completed workers whose futures
        # were never consumed by the main thread. Recover those results solely so
        # their isolated workspaces can be released as part of this job cleanup.
        for future in list(futures):
            if not future.done() or future.cancelled():
                continue
            try:
                finished_candidate = future.result()
            except BaseException:
                continue
            if isinstance(finished_candidate, CandidateResult) and all(
                existing is not finished_candidate for existing in candidates
            ):
                candidates.append(finished_candidate)
        _cleanup_team_workspaces(candidates, integration, event_fn)
