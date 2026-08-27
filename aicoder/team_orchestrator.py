"""Experimental RAM-backed multi-agent orchestration for AICoder.

The orchestrator deliberately reuses NativeLightRuntime for every tool-capable
worker so tool calling, approvals, recovery, telemetry and workspace protection
stay identical to normal AICoder runs.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Callable

from .agent_runtime import (
    AgentRunResult, NativeLightRuntime, MAX_AUTO_RESUMES,
    auto_resumable_pause, auto_resume_limit, auto_resume_prompt, continuation_messages,
)
from .executor import build_system_prompt, load_tools
from .config import CONFIG_DIR
from . import audit
from .model_transport import ModelTransport
from .performance import model_usage_metrics
from .team_runtime import (
    BRAINSTORM_EVOLUTION_SYSTEM_PROMPT, BRAINSTORM_OPERATOR_SYSTEM_PROMPT, BRAINSTORM_PERSPECTIVES, BRAINSTORM_SYSTEM_PROMPT, BRAINSTORM_SYNTHESIS_SYSTEM_PROMPT,
    CODER_SYSTEM_TEMPLATE, MERGE_PLANNER_SYSTEM_PROMPT, MERGE_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT, RESEARCH_INSTRUCTIONS, RESEARCH_OUTPUT_CONTRACT,
    RESEARCH_PLANNER_SYSTEM_PROMPT, TEST_PLANNER_SYSTEM_PROMPT, TeamConfig,
)
from .team_pipeline import (
    StageLedger, TeamStage, blind_candidate_id, content_fingerprint, execute_verification_plan,
    objective_rank_key, project_verification_plan, test_change_evidence, verification_passed,
)
from .workspace_backend import (
    RamWorkspace, WorkspaceBackend, WorkspaceError, create_isolated_team_workspace,
    team_workspace_plan,
)

EventFn = Callable[[str, dict[str, Any]], None]
StopFn = Callable[[], bool]

_TEAM_CHECKPOINT_DIR = CONFIG_DIR / "team-checkpoints"

def _team_checkpoint_path(source_workspace: str) -> Path:
    import hashlib
    key = hashlib.sha256(str(Path(source_workspace).resolve()).encode("utf-8")).hexdigest()[:20]
    return _TEAM_CHECKPOINT_DIR / f"{key}.json"

def load_team_checkpoint(source_workspace: str) -> dict[str, Any] | None:
    path = _team_checkpoint_path(source_workspace)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("source_workspace") or "") != str(Path(source_workspace).resolve()):
        return None
    return data

def load_latest_team_checkpoint(workspace_root: str) -> dict[str, Any] | None:
    root = Path(workspace_root).resolve()
    try:
        paths = sorted(_TEAM_CHECKPOINT_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            source = Path(str(data.get("source_workspace") or "")).resolve()
            source.relative_to(root)
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None

def _save_team_checkpoint(source_workspace: str, payload: dict[str, Any]) -> None:
    _TEAM_CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = _team_checkpoint_path(source_workspace)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = {**payload, "source_workspace": str(Path(source_workspace).resolve())}
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def clear_team_checkpoint(source_workspace: str) -> None:
    try:
        _team_checkpoint_path(source_workspace).unlink(missing_ok=True)
    except OSError:
        pass

_RESEARCH_TOOL_NAMES = frozenset({
    "search", "crawl", "web_fetch_local", "web_search_local", "doc_read", "doc_search",
    "file_read", "file_tree", "code_read", "code_tree", "code_search", "code_grep",
    "git", "skill_read", "test", "lint", "binary_exec",
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
    telemetry: dict[str, Any] = field(default_factory=dict)


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


def _emit(fn: EventFn | None, kind: str, **payload: Any) -> None:
    if fn is None:
        return
    try:
        fn(kind, payload)
    except Exception:
        pass


def _emit_stage_result(fn: EventFn | None, result: AgentStageResult) -> None:
    _emit(
        fn, "team_stage", role=result.role, status=result.status, model=result.model,
        elapsed_ms=result.elapsed_ms, error=result.error, evidence=result.evidence,
        telemetry=result.telemetry, detail=(result.response or result.error)[:1600],
    )
    if result.role in {"plan_research", "brainstorm_synthesis", "plan_code", "merge_plan", "plan_tests"} and result.response.strip():
        _emit(
            fn, "team_model_output", role=result.role, model=result.model,
            text=result.response[:6000], final=True,
        )


def _worker_event_forwarder(fn: EventFn | None, role: str) -> EventFn:
    allowed = {
        "model_start", "model_response", "thought", "tool_call", "tool_result",
        "error", "paused", "performance_warning", "performance_summary", "final",
        "verification_required", "completion_audit", "runtime_status",
    }
    def forward(kind: str, payload: dict[str, Any]) -> None:
        if kind in allowed:
            forwarded = dict(payload)
            reserved = {}
            for key in ("kind", "event", "role"):
                if key in forwarded:
                    reserved[key] = forwarded.pop(key)
            _emit(
                fn, "team_worker_event", role=role, event=kind,
                worker_payload=reserved or None, **forwarded,
            )
    return forward


def _is_incomplete_envelope_reason(reason: str) -> bool:
    text = str(reason or "").lower()
    return (
        "transient incomplete chat response" in text
        or "no recognized assistant response envelope" in text
        or "keys=['_transport_telemetry']" in text
    )


def _fresh_recovery_handoff(
    result: AgentRunResult, reason: str, attempt: int, limit: int, *, max_chars: int = 12000,
) -> str:
    """Build a bounded handoff for a fresh model chat after an incomplete provider envelope.

    The new runtime keeps the same isolated workspace but intentionally receives no old
    conversation object.  Relevant prior user/assistant/tool evidence is transferred as
    plain text so malformed provider state cannot poison the replacement chat.
    """
    rows: list[str] = []
    for message in continuation_messages(result):
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        else:
            try:
                text = json.dumps(content, ensure_ascii=False, default=str)
            except Exception:
                text = str(content or "")
        if not text and message.get("tool_calls"):
            try:
                text = json.dumps(message.get("tool_calls"), ensure_ascii=False, default=str)
            except Exception:
                text = str(message.get("tool_calls") or "")
        if text:
            rows.append(f"[{role.upper()}]\n{text}")
    evidence = "\n\n".join(rows)
    if len(evidence) > max_chars:
        head = max_chars // 3
        tail = max_chars - head
        evidence = evidence[:head] + "\n\n[... bounded handoff omitted middle context ...]\n\n" + evidence[-tail:]
    return (
        f"FRESH RECOVERY CHAT {attempt}/{limit}\n\n"
        "The previous provider chat ended with an incomplete response envelope. This is a NEW chat. "
        "Continue the same worker task using the existing isolated workspace as the authoritative current state. "
        "Do not restart completed work and do not assume the handoff text is more current than files in the workspace. "
        "Inspect files/tests as needed, preserve existing good changes, then continue toward the original completion contract.\n\n"
        f"[RECOVERY_REASON]\n{reason[:2000]}\n\n"
        f"[PRIOR_CONTEXT_AND_EVIDENCE]\n{evidence or '(no transferable conversation evidence)'}"
    )


def _run_worker_with_auto_resume(
    run_once: Callable[[str | None, list[dict[str, Any]] | None], AgentRunResult],
    *, role: str, event_fn: EventFn | None, stop_requested: StopFn | None,
) -> AgentRunResult:
    result = run_once(None, None)
    attempts = {"continuation": 0, "recovery": 0}
    while (
        result.status == "paused"
        and auto_resumable_pause(result.response or result.error)
        and not (stop_requested and stop_requested())
    ):
        reason = result.response or result.error
        slice_mode = "continuation" if "safety pause after an unusually long run" in reason.lower() else "recovery"
        limit = auto_resume_limit(reason)
        if attempts[slice_mode] >= limit:
            break
        attempts[slice_mode] += 1
        attempt = attempts[slice_mode]
        if slice_mode == "recovery" and "transient model/backend failure" in reason.lower():
            envelope_failure = _is_incomplete_envelope_reason(reason)
            if envelope_failure:
                envelope_delays = (30.0, 60.0, 120.0, 300.0, 300.0)
                delay_s = envelope_delays[min(attempt - 1, len(envelope_delays) - 1)]
                status = "cooldown"
                label = "incomplete provider envelope recovery"
            else:
                delay_s = min(8.0, float(2 ** (attempt - 1)))
                delay_s += (sum(ord(ch) for ch in role) % 5) * 0.17
                status = "backoff"
                label = "transient provider backoff"
            _emit(
                event_fn, "team_worker_event", role=role, event="runtime_status",
                category="recovery", status=status, phase="auto_resume",
                message=f"{label} {delay_s:.2f}s before retry {attempt}/{limit}",
            )
            time.sleep(delay_s)
        _emit(
            event_fn, "team_worker_event", role=role, event="runtime_status",
            category="recovery", status="resuming", phase="auto_resume",
            message=f"automatic {slice_mode} {attempt}/{limit}: {reason[:1000]}",
        )
        if slice_mode == "recovery" and _is_incomplete_envelope_reason(reason):
            handoff = _fresh_recovery_handoff(result, reason, attempt, limit)
            _emit(
                event_fn, "team_worker_event", role=role, event="runtime_status",
                category="recovery", status="fresh_chat", phase="auto_resume",
                message=f"starting fresh recovery chat {attempt}/{limit} with bounded context handoff",
            )
            result = run_once(handoff, None)
        else:
            result = run_once(auto_resume_prompt(reason, attempt, limit), continuation_messages(result))
    if result.status == "paused" and auto_resumable_pause(result.response or result.error):
        reason = result.response or result.error
        slice_mode = "continuation" if "safety pause after an unusually long run" in reason.lower() else "recovery"
        limit = auto_resume_limit(reason)
        _emit(
            event_fn, "team_worker_event", role=role, event="runtime_status",
            category="recovery", status="failed", phase="auto_resume",
            message=(f"automatic {slice_mode} budget exhausted after {attempts[slice_mode]}/{limit} attempt(s); "
                     f"continuations={attempts['continuation']}, recoveries={attempts['recovery']}"),
        )
    return result


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
    started = time.monotonic()
    envelope_delays = (30.0, 60.0, 120.0, 300.0, 300.0)
    for attempt in range(len(envelope_delays) + 1):
        try:
            result = model_client.chat(
                message=prompt, model=model, system_prompt=system, temperature=0.2,
                max_tokens=max_tokens, fallback_model=None, tools=None, tool_choice="none",
            )
            response = str(result.get("response") or "").strip() if isinstance(result, dict) else ""
            elapsed_ms = int((time.monotonic() - started) * 1000)
            usage = model_usage_metrics(result, elapsed_ms)
            telemetry = {
                **usage,
                "transport_telemetry": dict(result.get("_transport_telemetry") or {}) if isinstance(result, dict) else {},
            }
            if not response:
                return AgentStageResult("advisor", model, "failed", "", elapsed_ms, "empty response", telemetry=telemetry)
            return AgentStageResult("advisor", str(result.get("model") or model), "completed", response, elapsed_ms, telemetry=telemetry)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if _is_incomplete_envelope_reason(reason) and attempt < len(envelope_delays):
                delay_s = envelope_delays[attempt]
                _emit(
                    event_fn, "team_worker_event", role=role, event="runtime_status",
                    category="recovery", status="cooldown", phase="advisor_retry",
                    message=f"incomplete provider envelope recovery {delay_s:.0f}s before advisor retry {attempt + 1}/{len(envelope_delays)}",
                )
                if stop_requested is None:
                    time.sleep(delay_s)
                else:
                    deadline = time.monotonic() + delay_s
                    while time.monotonic() < deadline:
                        if stop_requested():
                            return AgentStageResult(
                                role, model, "failed", "", int((time.monotonic()-started)*1000),
                                "advisor recovery stopped by user",
                            )
                        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
                continue
            return AgentStageResult("advisor", model, "failed", "", int((time.monotonic()-started)*1000), reason)
    return AgentStageResult("advisor", model, "failed", "", int((time.monotonic()-started)*1000), "advisor recovery exhausted")

def _filtered_tools(catalogue: list[dict], names: frozenset[str]) -> list[dict]:
    return [dict(tool) for tool in catalogue if str(tool.get("name") or "") in names]


def _run_researcher(
    *, client, model_client: ModelTransport, model: str, role: str, task: str,
    source_workspace: str, tools: list[dict], stop_requested: StopFn | None,
    research_plan: str = "", request_timeout: int = 300, event_fn: EventFn | None = None,
) -> AgentStageResult:
    prompt = (
        f"User task:\n{task}\n\nRepository root for read-only inspection: {source_workspace}\n\n"
        f"RESEARCH CONTRACT:\n{research_plan or '(none)'}\n\n"
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

    def run_once(continuation_prompt: str | None, conversation: list[dict[str, Any]] | None) -> AgentRunResult:
        runtime = NativeLightRuntime(
            client=client, model_client=model_client, initial_prompt=continuation_prompt or prompt,
            model=model, fallback_model=None, workspace_root=source_workspace,
            plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
            tools=tools, system_prompt=system, load_tools_on_start=True,
            quick_chat=False, persistent_plan=False, approval_fn=lambda *_: False,
            max_iterations=10, max_output_tokens=6000, stop_requested=stop_requested,
            base_timeout=request_timeout, event_fn=research_event, conversation=conversation,
        )
        return runtime.run()

    result = _run_worker_with_auto_resume(
        run_once, role=f"research:{role}", event_fn=event_fn, stop_requested=stop_requested,
    )
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
        entries = sorted(p.name for p in root.iterdir() if p.name not in {".git", ".venv", "node_modules"})[:80]
        rows.append("top_level=" + ", ".join(entries))
    except Exception:
        pass
    return "\n".join(rows)


def _brainstorm_participants(config: TeamConfig, limit: int = 6) -> list[tuple[str, str, str]]:
    """Return distinct configured models with complementary creative perspectives."""
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
        "conservative/minimal-change": "feasibility, simplicity, compatibility and low-risk high-value improvements",
        "architecture-first": "architecture, extensibility, clean boundaries and future capabilities",
        "performance/efficiency": "performance, resource efficiency, latency, automation and developer productivity",
        "robustness/security": "security hardening, resilience, recovery, observability and abuse resistance",
    }
    for slot in config.coders:
        add(f"coder:{slot.strategy}", slot.model, coder_perspectives.get(slot.strategy, "implementation and engineering opportunities"))
    add("planner", config.planner_model, "requirements, product coherence, implementation leverage and testable outcomes")
    add("coordinator", config.coordinator_model, "cross-team synthesis, dependency risks, orchestration and missing acceptance criteria")
    add("merge", config.merge_model, "integration safety, conflict reduction, composability and incremental delivery")
    add("test_planner", config.test_planner_model, "testability, verification depth, failure injection and regression prevention")
    return rows


def _brainstorm_rounds(state: dict[str, Any]) -> int:
    try:
        return max(1, min(5, int(state.get("team_brainstorm_rounds") or 2)))
    except (TypeError, ValueError):
        return 2


def _build_brainstorm_prompt(
    task: str, repo_context: str, research: list[AgentStageResult], perspective: str,
    *, round_index: int = 1, brainstorm_state: str = "",
) -> str:
    reports = []
    for item in research:
        reports.append(f"### {item.role} · status={item.status}\n{item.response or item.error}")
    state_block = brainstorm_state.strip() or "(none — create independent ideas without anchoring on peers)"
    return (
        f"[ORIGINAL_USER_TASK]\n{task}\n\n[AUTHORITATIVE_REPOSITORY_CONTEXT]\n{repo_context}\n\n"
        f"[BRAINSTORM_ROUND]\n{round_index}\n\n[YOUR_CREATIVE_PERSPECTIVE]\n{perspective}\n\n"
        "[RESEARCH_EVIDENCE]\n" + "\n\n".join(reports)
        + f"\n\n[CURRENT_BRAINSTORM_STATE]\n{state_block}"
    )


def _anonymized_brainstorm_round(round_results: list[AgentStageResult]) -> str:
    rows = []
    ordered = sorted(round_results, key=lambda item: (item.role, item.response or item.error))
    for index, item in enumerate(ordered, start=1):
        rows.append(
            f"### proposal-{index:02d} · status={item.status}\n{item.response or item.error}"
        )
    return "\n\n".join(rows)


def _build_brainstorm_operator_prompt(task: str, round_index: int, round_results: list[AgentStageResult], previous_state: str = "") -> str:
    return (
        f"[ORIGINAL_USER_TASK]\n{task}\n\n[ROUND_NUMBER]\n{round_index}\n\n"
        f"[PREVIOUS_BRAINSTORM_STATE]\n{previous_state.strip() or '(none)'}\n\n"
        "[ANONYMIZED_ROUND_PROPOSALS]\n" + _anonymized_brainstorm_round(round_results)
    )


def _build_brainstorm_synthesis_prompt(task: str, brainstorm: list[AgentStageResult], brainstorm_state: str = "") -> str:
    return (
        f"[ORIGINAL_USER_TASK]\n{task}\n\n[FINAL_BRAINSTORM_STATE]\n{brainstorm_state.strip() or '(none)'}\n\n"
        "[ANONYMIZED_BRAINSTORM_CONTRIBUTIONS]\n" + _anonymized_brainstorm_round(brainstorm)
    )


def _build_planner_prompt(task: str, repo_context: str, research: list[AgentStageResult]) -> str:
    reports = []
    for item in research:
        evidence = item.evidence or {}
        verified = "verified-tool-evidence" if evidence.get("externally_verified") else "unverified-or-local-only"
        tools = ",".join(evidence.get("successful_tools") or []) or "none"
        reports.append(
            f"### {item.role} · status={item.status} · evidence={verified} · tools={tools}\n"
            f"{item.response or item.error}"
        )
    return (
        f"[ORIGINAL_USER_TASK]\n{task}\n\n[AUTHORITATIVE_REPOSITORY_CONTEXT]\n{repo_context}\n\n"
        "[INDEPENDENT_RESEARCH_REPORTS]\n" + "\n\n".join(reports)
    )


def _candidate_prompt(task: str, plan: str, coordinator: str, strategy: str) -> str:
    return (
        f"[ORIGINAL_USER_TASK]\n{task}\n\n[SHARED_IMPLEMENTATION_CONTRACT]\n{plan}\n\n"
        f"[COORDINATION_NOTES]\n{coordinator or '(none)'}\n\n"
        f"[CANDIDATE_STRATEGY]\n{strategy}\n\n"
        "[EXECUTION_RULE]\nUse the current Native-Light runtime workspace as the only project tree. "
        "Do not follow source/engine paths embedded in the original user text. Implement the complete shared contract. "
        "Do not spend the whole run repeatedly inspecting git status, rerunning the same baseline tests, or rereading unchanged files. "
        "After establishing enough evidence, move to implementation. If no mutation is justified, finish explicitly with DONE and the evidence. "
        "For coding work, prioritize an actual workspace mutation followed by verification before the iteration budget is exhausted."
    )


_MERGE_INCOMPLETE_MARKERS = (
    "merge konnte nicht ausgeführt werden",
    "merge could not be executed",
    "verification: nicht möglich",
    "verification: not possible",
    "verification: impossible",
    "keine workspace-tools verfügbar",
    "no workspace tools available",
    "recovery_required",
    "recovery required",
    "integration failed",
    "integration was not performed",
    "integration not performed",
    "verification was not performed",
    "verification not performed",
    "required verification failed",
    "verification: incomplete",
    "persistence: blocked",
    "persistenz: blockiert",
)


def _merge_completion_contradiction(response: str) -> bool:
    """Reject a DONE merge that explicitly self-reports an incomplete merge."""
    text = str(response or "")
    if "[MERGE_RESULT]" in text:
        text = text.rsplit("[MERGE_RESULT]", 1)[-1]
    lowered = text.lower()
    return any(marker in lowered for marker in _MERGE_INCOMPLETE_MARKERS)


def _candidate_approval(tool_name: str, args: dict) -> bool:
    """Autonomous candidate policy: safe RAM mutations yes; elevation/destruction/escape/security never."""
    from .executor import is_destructive
    from .privileges import assess_execution
    risk = assess_execution(tool_name, args, destructive=is_destructive(str(args.get("command") or "")))
    if args.get("_workspace_escape") or risk.elevation or risk.deletion or risk.destructive or risk.security_change:
        return False
    return bool(risk.mutation) or not risk.needs_approval


def _run_candidate(
    *, client, model_client: ModelTransport, source_workspace: str, backend_mode: str,
    slot: int, model: str, strategy: str, task: str, plan: str, coordinator: str,
    tools: list[dict], stop_requested: StopFn | None, request_timeout: int = 300,
    event_fn: EventFn | None = None,
) -> CandidateResult:
    backend = create_isolated_team_workspace(source_workspace, backend_mode)
    try:
        backend.prepare()
        if not isinstance(backend, RamWorkspace):
            raise WorkspaceError("parallel candidate runtime requires a transactional isolated workspace")
        system = build_system_prompt(tools, str(backend.info.execution_root)).rstrip() + "\n\n" + CODER_SYSTEM_TEMPLATE.format(slot=slot, strategy=strategy)
        started = time.monotonic()
        worker_role = f"coder:{slot}"
        # Avoid a four-request burst against one upstream/provider at the exact
        # same instant.  Keep the stagger short so healthy parallelism remains.
        startup_delay_s = min(15.0, max(0.0, (slot - 1) * 5.0))
        if startup_delay_s:
            _emit(
                event_fn, "team_worker_event", role=worker_role, event="runtime_status",
                category="provider_pacing", status="waiting", phase="startup",
                message=f"candidate request stagger {startup_delay_s:.2f}s",
            )
            time.sleep(startup_delay_s)
        forward = _worker_event_forwarder(event_fn, worker_role)
        original_prompt = _candidate_prompt(task, plan, coordinator, strategy)

        def run_once(continuation_prompt: str | None, conversation: list[dict[str, Any]] | None) -> AgentRunResult:
            runtime = NativeLightRuntime(
                client=client, model_client=model_client,
                initial_prompt=continuation_prompt or original_prompt,
                model=model, fallback_model=None, workspace_root=str(backend.info.execution_root),
                plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
                tools=tools, system_prompt=system, load_tools_on_start=True,
                quick_chat=False, persistent_plan=False, approval_fn=_candidate_approval,
                max_iterations=18, max_output_tokens=12000, stop_requested=stop_requested,
                base_timeout=request_timeout, event_fn=forward, conversation=conversation,
            )
            return runtime.run()

        run = _run_worker_with_auto_resume(
            run_once, role=worker_role, event_fn=event_fn, stop_requested=stop_requested,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        audit.log_tool(tool_name="team_candidate_result", arguments={"slot": slot, "strategy": strategy}, result=f"status={run.status}; iterations={run.iterations}; error={str(run.error or chr(45))[:1200]}; response_present={bool(str(run.response or chr(32)).strip())}", duration_s=elapsed_ms / 1000.0, is_error=run.status != "completed", model=model, iteration=run.iterations)
        return CandidateResult(
            slot=slot, model=model, strategy=strategy, workspace=backend, run=run,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        audit.log_tool(tool_name="team_candidate_result", arguments={"slot": slot, "strategy": strategy}, result=f"status=exception; type={type(exc).__name__}; error={str(exc)[:1200]}", duration_s=0.0, is_error=True, model=model, iteration=0)
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
    test_evidence = test_change_evidence(delta)
    if test_evidence["behavior_change"] and not test_evidence["tests_changed"]:
        score -= 35
    if candidate.run.error:
        score -= 20
    diff = candidate.workspace.delta_diff() if isinstance(candidate.workspace, RamWorkspace) else _git_diff(root)
    return {
        "score": score, "delta": delta, "checks": checks, "diff": diff,
        "candidate_id": blind_candidate_id(),
        "content_fingerprint": content_fingerprint(diff),
        "verification_passed": verification_passed(results),
        "test_change_evidence": test_evidence,
    }


def _evaluation_prompt(candidates: list[CandidateResult]) -> str:
    rows = []
    for c in sorted(candidates, key=lambda item: item.slot):
        rows.append(json.dumps({
            "slot": c.slot, "model": c.model, "strategy": c.strategy,
            "run_status": c.run.status, "score": c.score,
            "evaluation": {k: v for k, v in c.evaluation.items() if k != "diff"},
            "summary": c.run.response[:1600], "diff": c.evaluation.get("diff", "")[:20000],
        }, ensure_ascii=False))
    return "\n\n".join(rows)



def _stage_start(ledger: StageLedger, stage: TeamStage, event_fn: EventFn | None) -> None:
    ledger.start(stage)
    _emit(event_fn, "team_pipeline", stage=stage.value, status="started", ledger=ledger.as_dict())


def _stage_complete(ledger: StageLedger, stage: TeamStage, event_fn: EventFn | None) -> None:
    ledger.complete(stage)
    _emit(event_fn, "team_pipeline", stage=stage.value, status="completed", ledger=ledger.as_dict())


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
    seen_fingerprints: dict[str, str] = {}
    ordered = sorted(candidates, key=lambda item: (str(item.evaluation.get("candidate_id") or ""), item.slot))
    for ordinal, candidate in enumerate(ordered, start=1):
        diff = str(candidate.evaluation.get("diff") or "")
        fingerprint = str(candidate.evaluation.get("content_fingerprint") or content_fingerprint(diff))
        snapshot_id = str(candidate.evaluation.get("candidate_id") or blind_candidate_id())
        duplicate_of = seen_fingerprints.get(fingerprint)
        if duplicate_of is None:
            seen_fingerprints[fingerprint] = snapshot_id
        target = base / snapshot_id
        shutil.copytree(
            candidate.workspace.info.execution_root, target, symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git", ".aicoder-team", ".venv", "venv", "env", ".env",
                "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                "build", "dist", "*.egg-info",
            ),
            copy_function=_link_or_copy, dirs_exist_ok=False,
        )
        evidence.append({
            "candidate_id": snapshot_id,
            "content_fingerprint": fingerprint,
            "duplicate_of": duplicate_of,
            "score": int(candidate.evaluation.get("score") or 0),
            "run_status": candidate.run.status,
            "verification_passed": bool(candidate.evaluation.get("verification_passed")),
            "checks": candidate.evaluation.get("checks") or {},
            "delta": candidate.evaluation.get("delta") or {},
            "diff": diff[:20000],
            "diff_truncated": len(diff) > 20000,
            "snapshot": f".aicoder-team/candidates/{snapshot_id}",
        })
    integration.write_candidate_artifact(
        ".aicoder-team/candidates.json", json.dumps(evidence, ensure_ascii=False, indent=2)
    )
    return evidence


def _blind_merge_prompt(task: str, code_plan: str, evidence: list[dict[str, Any]]) -> str:
    return (
        f"[USER_TASK]\n{task}\n\n[SHARED_IMPLEMENTATION_CONTRACT]\n{code_plan}\n\n"
        "[ANONYMIZED_CANDIDATE_EVIDENCE]\n" + json.dumps(evidence, ensure_ascii=False, indent=2)
    )


def run_team(
    *, task: str, state: dict[str, Any], config: TeamConfig, client,
    model_client: ModelTransport, source_workspace: str,
    event_fn: EventFn | None = None, stop_requested: StopFn | None = None,
    resume_checkpoint: dict[str, Any] | None = None,
) -> TeamRunResult:
    errors = config.validate()
    runtime_mode = str(state.get("runtime_mode") or "native-light").strip().lower()
    if runtime_mode != "native-light":
        errors.append(f"team runtime requires native-light; configured runtime is {runtime_mode}")
    if errors:
        return TeamRunResult("failed", "", "", [], [], {"runtime_mode": runtime_mode}, "; ".join(errors))
    try:
        request_timeout = max(10, min(300, int(state.get("request_timeout") or 300)))
    except (TypeError, ValueError):
        request_timeout = 300

    started = time.monotonic()
    resume_code = bool(resume_checkpoint and resume_checkpoint.get("stage") == TeamStage.CODE.value and resume_checkpoint.get("code_plan"))
    ledger = StageLedger(completed=[TeamStage.PLAN_RESEARCH.value, TeamStage.RESEARCH.value, TeamStage.BRAINSTORM.value, TeamStage.PLAN_CODE.value]) if resume_code else StageLedger()
    stages: list[AgentStageResult] = []
    candidates: list[CandidateResult] = []
    all_tools = load_tools(client)
    research_tools = _filtered_tools(all_tools, _RESEARCH_TOOL_NAMES)
    coder_tools = _filtered_tools(all_tools, _CODER_TOOL_NAMES)
    _emit(event_fn, "team_start", agents=config.active_count, research=len(config.research), coders=len(config.coders), runtime_mode=runtime_mode, request_timeout=request_timeout)

    if resume_code:
        saved_task = str(resume_checkpoint.get("task") or "").strip()
        if saved_task:
            task = saved_task
        code_plan = AgentStageResult(
            role="plan_code", model=str(resume_checkpoint.get("model") or config.planner_model or ""),
            status="completed", response=str(resume_checkpoint.get("code_plan") or ""), elapsed_ms=0,
        )
        research_results: list[AgentStageResult] = []
        _emit(event_fn, "team_resume", stage=TeamStage.CODE.value, source_workspace=source_workspace)
    else:
        # 1) plan_research
        _stage_start(ledger, TeamStage.PLAN_RESEARCH, event_fn)
        research_planner_model = config.coordinator_model or config.planner_model or ""
        research_plan = _call_advisor(
            model_client, model=research_planner_model, system=RESEARCH_PLANNER_SYSTEM_PROMPT,
            prompt=f"USER TASK:\n{task}\n\nREPOSITORY CONTEXT:\n{_repository_context(source_workspace)}",
            max_tokens=5000, event_fn=event_fn, role="plan_research", stop_requested=stop_requested,
        )
        research_plan.role = "plan_research"; stages.append(research_plan); _emit_stage_result(event_fn, research_plan)
        if research_plan.status != "completed":
            return TeamRunResult("failed", "", research_plan.model, stages, [], {"ledger": ledger.as_dict()}, research_plan.error)
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
                        research_plan=research_plan.response, request_timeout=request_timeout, event_fn=event_fn,
                    ): slot for slot in config.research
                }
                for future in as_completed(futures):
                    slot = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = AgentStageResult(f"research:{slot.role}", slot.model, "failed", "", 0, f"{type(exc).__name__}: {exc}")
                    research_results.append(result); stages.append(result)
                    _emit_stage_result(event_fn, result)
        _stage_complete(ledger, TeamStage.RESEARCH, event_fn)

        # 3) brainstorm — complete deterministic rounds with anonymous idea evolution.
        _stage_start(ledger, TeamStage.BRAINSTORM, event_fn)
        brainstorm_results: list[AgentStageResult] = []
        repo_context = _repository_context(source_workspace)
        brainstorm_participants = _brainstorm_participants(config)
        configured_rounds = _brainstorm_rounds(state)
        brainstorm_state = ""
        _emit(
            event_fn, "team_brainstorm_config", rounds=configured_rounds,
            participants=len(brainstorm_participants),
        )
        synthesis_model = config.coordinator_model or config.planner_model or ""
        for round_index in range(1, configured_rounds + 1):
            if not brainstorm_participants:
                break
            _emit(event_fn, "team_brainstorm_round", round=round_index, status="started", total_rounds=configured_rounds)
            system_prompt = BRAINSTORM_SYSTEM_PROMPT if round_index == 1 else BRAINSTORM_EVOLUTION_SYSTEM_PROMPT
            with ThreadPoolExecutor(max_workers=len(brainstorm_participants), thread_name_prefix=f"aicoder-brainstorm-r{round_index}") as pool:
                futures = {
                    pool.submit(
                        _call_advisor, model_client, model=model, system=system_prompt,
                        prompt=_build_brainstorm_prompt(
                            task, repo_context, research_results, perspective,
                            round_index=round_index, brainstorm_state=brainstorm_state,
                        ),
                        max_tokens=4500,
                    ): (label, model) for label, model, perspective in brainstorm_participants
                }
                round_results: list[AgentStageResult] = []
                for future in as_completed(futures):
                    label, model = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = AgentStageResult(f"brainstorm:r{round_index}:{label}", model, "failed", "", 0, f"{type(exc).__name__}: {exc}")
                    result.role = f"brainstorm:r{round_index}:{label}"
                    round_results.append(result); brainstorm_results.append(result); stages.append(result); _emit_stage_result(event_fn, result)
            usable = [item for item in round_results if item.status == "completed" and item.response.strip()]
            if not usable:
                break
            operator_result = _call_advisor(
                model_client, model=synthesis_model, system=BRAINSTORM_OPERATOR_SYSTEM_PROMPT,
                prompt=_build_brainstorm_operator_prompt(task, round_index, usable, brainstorm_state), max_tokens=5000,
                event_fn=event_fn, role=f"brainstorm_state:r{round_index}", stop_requested=stop_requested,
            )
            operator_result.role = f"brainstorm_state:r{round_index}"
            stages.append(operator_result); _emit_stage_result(event_fn, operator_result)
            if operator_result.status != "completed" or not operator_result.response.strip():
                break
            brainstorm_state = operator_result.response
            _emit(
                event_fn, "team_brainstorm_round", round=round_index, status="completed",
                proposals=len(usable), total_rounds=configured_rounds,
            )

        brainstorm_synthesis = _call_advisor(
            model_client, model=synthesis_model, system=BRAINSTORM_SYNTHESIS_SYSTEM_PROMPT,
            prompt=_build_brainstorm_synthesis_prompt(task, brainstorm_results, brainstorm_state), max_tokens=6000,
            event_fn=event_fn, role="brainstorm_synthesis", stop_requested=stop_requested,
        )
        brainstorm_synthesis.role = "brainstorm_synthesis"; stages.append(brainstorm_synthesis); _emit_stage_result(event_fn, brainstorm_synthesis)
        if brainstorm_synthesis.status != "completed":
            return TeamRunResult("failed", "", brainstorm_synthesis.model, stages, [], {"ledger": ledger.as_dict()}, brainstorm_synthesis.error)
        _stage_complete(ledger, TeamStage.BRAINSTORM, event_fn)

        # 4) plan_code — evidence plus bounded creative synthesis becomes the shared contract.
        _stage_start(ledger, TeamStage.PLAN_CODE, event_fn)
        code_plan = _call_advisor(
            model_client, model=config.planner_model or "", system=PLANNER_SYSTEM_PROMPT,
            prompt=_build_planner_prompt(task, _repository_context(source_workspace), research_results)
            + "\n\nRESEARCH CONTRACT:\n" + research_plan.response
            + "\n\nBOUNDED BRAINSTORM SYNTHESIS:\n" + brainstorm_synthesis.response,
            max_tokens=9000, event_fn=event_fn, role="plan_code", stop_requested=stop_requested,
        )
        code_plan.role = "plan_code"; stages.append(code_plan); _emit_stage_result(event_fn, code_plan)
        if code_plan.status != "completed":
            return TeamRunResult("failed", "", code_plan.model, stages, [], {"ledger": ledger.as_dict()}, code_plan.error)
        _stage_complete(ledger, TeamStage.PLAN_CODE, event_fn)
        _save_team_checkpoint(source_workspace, {"stage": TeamStage.CODE.value, "task": task, "code_plan": code_plan.response, "model": code_plan.model, "created_at": time.time()})

    # 5) code — isolated parallel candidates with one fair global backing mode.
    workspace_plan = team_workspace_plan(
        source_workspace, len(config.coders), str(state.get("workspace_mode") or "auto")
    )
    _emit(event_fn, "team_workspace_plan", **workspace_plan.as_dict())
    _stage_start(ledger, TeamStage.CODE, event_fn)
    with ThreadPoolExecutor(max_workers=min(2, len(config.coders)), thread_name_prefix="aicoder-coder") as pool:
        futures = {
            pool.submit(
                _run_candidate, client=client, model_client=model_client, source_workspace=source_workspace,
                backend_mode=workspace_plan.backend_mode, slot=slot.slot, model=slot.model,
                strategy=slot.strategy, task=task, plan=code_plan.response, coordinator="",
                tools=coder_tools, stop_requested=stop_requested, request_timeout=request_timeout, event_fn=event_fn,
            ): slot for slot in config.coders
        }
        for future in as_completed(futures):
            slot = futures[future]
            try:
                candidate = future.result()
                evaluation_started = time.monotonic()
                candidate.evaluation = evaluate_candidate(candidate)
                candidate.evaluation_ms = int((time.monotonic() - evaluation_started) * 1000)
                candidate.score = int(candidate.evaluation.get("score") or 0)
                candidates.append(candidate)
                _emit(event_fn, "team_candidate", candidate_id=candidate.evaluation.get("candidate_id"),
                      status=candidate.run.status, score=candidate.score,
                      elapsed_ms=candidate.elapsed_ms, evaluation_ms=candidate.evaluation_ms)
            except Exception as exc:
                _emit(event_fn, "team_candidate", candidate_id="failed", status="failed", score=-999,
                      error=f"{type(exc).__name__}: {exc}")
    viable = [candidate for candidate in candidates if candidate.run.status == "completed"]
    if not viable:
        for c in candidates: c.workspace.abort()
        return TeamRunResult("failed", "", "", stages, candidates, {"ledger": ledger.as_dict()}, "no coding candidate completed")
    winner = max(viable, key=lambda item: objective_rank_key(item.evaluation))
    _stage_complete(ledger, TeamStage.CODE, event_fn)

    # Build fresh integration workspace and attach anonymized full snapshots.
    integration = create_isolated_team_workspace(source_workspace, workspace_plan.backend_mode)
    integration.prepare()
    if not isinstance(integration, RamWorkspace):
        for c in candidates: c.workspace.abort()
        integration.abort()
        return TeamRunResult("failed", "", "", stages, candidates, {"ledger": ledger.as_dict()}, "integration requires transactional isolation")
    _emit(
        event_fn, "team_integration_workspace", mode=integration.info.mode,
        fallback_reason=integration.info.fallback_reason,
    )
    integration.seed_from(winner.workspace.info.execution_root)
    blind_evidence = _attach_blind_candidate_snapshots(integration, candidates)
    winner_id = str(winner.evaluation.get("candidate_id"))

    # 5) merge_plan — blind to model/provider/slot identity.
    _stage_start(ledger, TeamStage.MERGE_PLAN, event_fn)
    merge_planner_model = config.coordinator_model or config.planner_model or ""
    merge_plan = _call_advisor(
        model_client, model=merge_planner_model, system=MERGE_PLANNER_SYSTEM_PROMPT,
        prompt=_blind_merge_prompt(task, code_plan.response, blind_evidence)
        + f"\n\nDETERMINISTIC BASE CANDIDATE: {winner_id}",
        max_tokens=6000, event_fn=event_fn, role="merge_plan", stop_requested=stop_requested,
    )
    merge_plan.role = "merge_plan"; stages.append(merge_plan); _emit_stage_result(event_fn, merge_plan)
    if merge_plan.status != "completed":
        integration.abort(); [c.workspace.abort() for c in candidates]
        return TeamRunResult("failed", "", merge_plan.model, stages, candidates, {"ledger": ledger.as_dict()}, merge_plan.error)
    integration.write_candidate_artifact(".aicoder-team/merge-plan.txt", merge_plan.response)
    _stage_complete(ledger, TeamStage.MERGE_PLAN, event_fn)

    # 6) merge — optional LLM. Empty merge slot means deterministic winner only.
    _stage_start(ledger, TeamStage.MERGE, event_fn)
    merge_model = config.merge_model
    if merge_model:
        merge_prompt = (
            f"USER TASK:\n{task}\n\nCODE CONTRACT:\n{code_plan.response}\n\n"
            f"BLIND MERGE CONTRACT:\n{merge_plan.response}\n\n"
            "Candidate snapshots are under .aicoder-team/candidates/. Integrate only evidence-backed improvements."
        )
        merge_system = build_system_prompt(coder_tools, str(integration.info.execution_root)).rstrip()+"\n\n"+MERGE_SYSTEM_PROMPT
        merge_forward = _worker_event_forwarder(event_fn, "merge")

        def run_merge_once(continuation_prompt: str | None, conversation: list[dict[str, Any]] | None) -> AgentRunResult:
            merge_runtime = NativeLightRuntime(
                client=client, model_client=model_client,
                initial_prompt=continuation_prompt or merge_prompt,
                model=merge_model, fallback_model=None, workspace_root=str(integration.info.execution_root),
                plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
                tools=coder_tools, system_prompt=merge_system,
                load_tools_on_start=True, quick_chat=False, persistent_plan=False,
                approval_fn=_candidate_approval, max_iterations=14, max_output_tokens=10000, stop_requested=stop_requested,
                base_timeout=request_timeout, event_fn=merge_forward, conversation=conversation,
            )
            result = merge_runtime.run()
            if result.status == "completed" and _merge_completion_contradiction(result.response):
                reason = (
                    "Merge self-reported incomplete: the final response explicitly states that "
                    "integration or verification could not be completed. Preserve prior tool evidence "
                    "and continue the existing merge instead of accepting DONE."
                )
                result.status = "paused"
                result.response = reason
                result.error = reason
            return result

        merge_started = time.monotonic()
        merge_run = _run_worker_with_auto_resume(
            run_merge_once, role="merge", event_fn=event_fn, stop_requested=stop_requested,
        )
        merge_elapsed = int((time.monotonic() - merge_started) * 1000)
        stages.append(AgentStageResult("merge", merge_run.model or merge_model, merge_run.status, merge_run.response, merge_elapsed, merge_run.error)); _emit_stage_result(event_fn, stages[-1])
        if merge_run.status != "completed":
            integration.abort(); [c.workspace.abort() for c in candidates]
            return TeamRunResult("failed", "", merge_run.model, stages, candidates, {"ledger": ledger.as_dict()}, merge_run.error or "merge failed")
        final_response = merge_run.response
        result_model = merge_run.model or merge_model
    else:
        stages.append(AgentStageResult("merge", "deterministic", "completed", f"Selected {winner_id} without LLM merge", 0)); _emit_stage_result(event_fn, stages[-1])
        final_response = f"Selected verified base candidate {winner_id}."
        result_model = winner.run.model
    _stage_complete(ledger, TeamStage.MERGE, event_fn)

    # Tests are implementation artifacts: behavior/source changes must carry test-file evidence.
    merged_delta = integration.delta_summary()
    merged_test_evidence = test_change_evidence(merged_delta)

    # 7) plan_tests — model reviews coverage intent; deterministic commands remain authoritative.
    _stage_start(ledger, TeamStage.PLAN_TESTS, event_fn)
    deterministic_plan = project_verification_plan(integration.info.execution_root)
    test_plan_text = json.dumps([
        {"name": item.name, "argv": list(item.argv), "timeout": item.timeout, "required": item.required}
        for item in deterministic_plan
    ], ensure_ascii=False, indent=2)
    if config.test_planner_model:
        test_plan = _call_advisor(
            model_client, model=config.test_planner_model, system=TEST_PLANNER_SYSTEM_PROMPT,
            prompt=(f"[USER_TASK]\n{task}\n\n[SHARED_IMPLEMENTATION_CONTRACT]\n{code_plan.response}\n\n"
                    f"[BLIND_MERGE_CONTRACT]\n{merge_plan.response}\n\n"
                    f"[MERGED_CHANGE_TEST_EVIDENCE_AUTHORITATIVE]\n{json.dumps(merged_test_evidence, ensure_ascii=False, indent=2)}\n\n"
                    f"[DETERMINISTIC_REPOSITORY_CHECKS_AUTHORITATIVE]\n{test_plan_text}"),
            max_tokens=5000, event_fn=event_fn, role="plan_tests", stop_requested=stop_requested,
        )
        test_plan.role = "plan_tests"; stages.append(test_plan); _emit_stage_result(event_fn, test_plan)
        if test_plan.status != "completed":
            integration.abort(); [c.workspace.abort() for c in candidates]
            return TeamRunResult("failed", "", test_plan.model, stages, candidates, {"ledger": ledger.as_dict()}, test_plan.error)
        integration.write_candidate_artifact(".aicoder-team/test-plan.txt", test_plan.response)
    else:
        stages.append(AgentStageResult("plan_tests", "deterministic", "completed", test_plan_text, 0))
    _stage_complete(ledger, TeamStage.PLAN_TESTS, event_fn)
    if not merged_test_evidence["coverage_evidence_ok"]:
        integration.abort(); [c.workspace.abort() for c in candidates]
        return TeamRunResult(
            "failed", "", result_model, stages, candidates,
            {"ledger": ledger.as_dict(), "test_change_evidence": merged_test_evidence},
            "test coverage evidence gate failed: source/behavior changes were merged without adding or updating tests",
        )

    # 8) tests_function_ok — only executable evidence can open the disk-write gate.
    _stage_start(ledger, TeamStage.TESTS_FUNCTION_OK, event_fn)
    verification_results = execute_verification_plan(integration.info.execution_root, deterministic_plan)
    verification_payload = [item.as_dict() for item in verification_results]
    for item in verification_results:
        _emit(event_fn, "team_verification", name=item.name, ok=item.ok, required=item.required, elapsed_ms=item.elapsed_ms, exit_code=item.exit_code, output=item.output[-1200:])
    integration.write_candidate_artifact(".aicoder-team/final-verification.json", json.dumps(verification_payload, ensure_ascii=False, indent=2))
    if not verification_passed(verification_results):
        failed_checks = [item.name for item in verification_results if item.required and not item.ok]
        _emit(event_fn, "team_error", stage="tests_function_ok", error="verification failed: " + ", ".join(failed_checks))
        integration.abort(); [c.workspace.abort() for c in candidates]
        return TeamRunResult(
            "failed", "", result_model, stages, candidates,
            {"ledger": ledger.as_dict(), "verification": verification_payload},
            f"tests_function_ok gate failed: {', '.join(failed_checks)}; persistent workspace was not modified",
        )
    _stage_complete(ledger, TeamStage.TESTS_FUNCTION_OK, event_fn)

    # 9) atomic_disk_write — the only persistent mutation stage.
    _stage_start(ledger, TeamStage.ATOMIC_DISK_WRITE, event_fn)
    try:
        integration.finalize(verified=True)
    except Exception as exc:
        _emit(event_fn, "team_error", stage="atomic_disk_write", error=f"{type(exc).__name__}: {exc}")
        integration.abort()
        [c.workspace.abort() for c in candidates]
        return TeamRunResult("failed", "", result_model, stages, candidates, {"ledger": ledger.as_dict(), "verification": verification_payload}, f"atomic_disk_write failed: {type(exc).__name__}: {exc}")
    for c in candidates:
        c.workspace.abort()
    _stage_complete(ledger, TeamStage.ATOMIC_DISK_WRITE, event_fn)
    clear_team_checkpoint(source_workspace)

    wall_ms = int((time.monotonic() - started) * 1000)
    accumulated_agent_ms = sum(stage.elapsed_ms for stage in stages) + sum(
        candidate.elapsed_ms + candidate.evaluation_ms for candidate in candidates
    )
    perf = {
        "wall_ms": wall_ms,
        "runtime_mode": runtime_mode, "request_timeout": request_timeout,
        "accumulated_agent_ms": accumulated_agent_ms,
        "parallelism": round(accumulated_agent_ms / wall_ms, 2) if wall_ms else 0.0,
        "research_agents": len(research_results), "coding_candidates": len(candidates),
        "winner_candidate_id": winner_id, "winner_score": winner.score,
        "workspace_plan": workspace_plan.as_dict(),
        "integration_workspace_mode": integration.info.mode,
        "ledger": ledger.as_dict(), "verification": verification_payload,
        "stage_timings": [
            {"role": stage.role, "model": stage.model, "status": stage.status, "elapsed_ms": stage.elapsed_ms}
            for stage in stages
        ],
    }
    _emit(event_fn, "team_complete", **perf)
    return TeamRunResult("completed", final_response, result_model or "", stages, candidates, perf)
