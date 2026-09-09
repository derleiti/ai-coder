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
from .executor import MAX_ITERATIONS, atomic_write_text, build_system_prompt, load_tools, trim_messages
from .model_transport import ModelTransport
from .performance import RuntimePerformance
from .task_contract import TaskContract, compile_task_contract
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
    objective_rank_key, project_verification_plan, task_acceptance_verification_plan, merge_verification_plans, test_change_evidence, verification_passed,
)
from .workspace import resolve_or_create_project_workspace
from .workspace_backend import (
    RamWorkspace, WorkspaceBackend, WorkspaceError, create_isolated_team_workspace,
    team_workspace_plan,
)

EventFn = Callable[[str, dict[str, Any]], None]
StopFn = Callable[[], bool]

# Team observational stages should not consume an entire frontier-model context
# just because one is advertised. StageOff is the durable cross-stage memory; a
# bounded local conversation keeps free/provider endpoints responsive while still
# retaining the newest evidence.
_TEAM_OBSERVATIONAL_CONTEXT_CHARS = 48_000
_TEAM_RECOVERY_CONTEXT_CHARS = 48_000
_OBSERVATIONAL_STAGE_DISCIPLINE = (
    "\n\n## OBSERVATIONAL STAGE DISCIPLINE\n"
    "- StageOff/final structured output is the authoritative handoff. Do not create SESSION_MEMORY.md, "
    "handoff scratch files, or other workspace notes merely to remember this stage.\n"
    "- This stage is observational only: NEVER call mutation tools such as file_edit, file_write, atomic_write, "
    "directory_create, package installers, git mutation commands, or shell commands that modify files. Inspect only.\n"
    "- Gather only evidence needed for the requested contract. Do not repeat searches/fetches or re-read "
    "large content already present in the current conversation.\n"
    "- Keep the final stage contract concise and information-dense; prefer explicit decisions and source facts "
    "over reproducing raw tool output.\n"
    "- FINAL CONTRACT BUDGET: target <= 2200 output tokens and <= 9000 characters. Every required section must "
    "still be present; use short bullets/tables instead of essays. Do not spend output budget restating the full user task."
)

_USER_CONSTRAINT_DISCIPLINE = (
    "\n\n## USER CONSTRAINT DISCIPLINE\n"
    "- Explicit user requirements and prohibitions are authoritative constraints, not research hypotheses. Never recommend, probe, install, or depend on something the user explicitly forbids.\n"
    "- Do not invent settings, skills, tool names, files, or capabilities. If a named capability is not already known from the supplied catalogue/context, list/inspect available options first or proceed without it.\n"
    "- A failed lookup of a nonexistent setting/skill is evidence to stop guessing nearby names, not a reason to keep probing variants."
)


_BOOTSTRAP_SECTIONS = (
    "SESSION MEMORY", "RESEARCH PLAN", "R1 PRIMARY SOURCES", "R2 BEST PRACTICES",
    "R3 SECURITY RELIABILITY", "R4 ALTERNATIVE ARCHITECTURES", "EVIDENCE GAPS",
    "NEXT STAGE INSTRUCTIONS",
)
_STAGEOFF_COORDINATOR_SECTIONS = (
    "STAGE SUMMARY", "NEW FACTS", "REQUIRED CHANGES", "COMPLETED ITEMS",
    "OPEN ITEMS", "RISKS", "NEXT STAGE INSTRUCTIONS",
)
_TEST_PLAN_SECTIONS = (
    "VERIFICATION OBJECTIVE", "REQUIRED CHECKS", "ACCEPTANCE ASSERTIONS",
    "MISSING TOOLS", "RISKS", "NEXT STAGE INSTRUCTIONS",
)


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
        retry_after = None
        request_id = f"advisor-{role}-{uuid.uuid4().hex[:10]}"
        _emit(
            event_fn, "team_worker_event", role=role, event="model_start", category="advisor",
            phase="advisor_call", model=model, request_id=request_id, attempt=attempt,
            timeout=getattr(model_client, "timeout", None), prompt_chars=len(prompt), max_tokens=max_tokens,
        )
        try:
            result = model_client.chat(
                message=prompt, model=model, system_prompt=system, temperature=0.2,
                max_tokens=max_tokens, fallback_model=None, tools=None, tool_choice="none",
                request_id=request_id,
            )
            response = str(result.get("response") or "").strip() if isinstance(result, dict) else ""
            _emit(
                event_fn, "team_worker_event", role=role, event="model_response", category="advisor",
                phase="advisor_call", model=str(result.get("model") or model) if isinstance(result, dict) else model,
                request_id=request_id, attempt=attempt, response_chars=len(response),
                latency_ms=(result.get("latency_ms") if isinstance(result, dict) else None),
                transport=(result.get("_transport_telemetry") if isinstance(result, dict) else None),
            )
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
            retry_after = getattr(exc, "retry_after", None)
        if not retryable:
            return AgentStageResult(
                role, model, "failed", "", int((time.monotonic()-started)*1000), reason,
                evidence={"prompt_chars": len(prompt), "response_chars": 0, "attempts": attempt},
            )
        delay = (
            float(min(300, retry_after))
            if isinstance(retry_after, int) and retry_after > 0
            else min(8.0, float(2 ** (attempt - 1)))
        )
        _emit(event_fn, "team_worker_event", role=role, event="runtime_status", category="recovery",
              status="backoff", phase="advisor_retry", message=f"advisor provider retry {attempt} (unlimited) in {delay:.0f}s: {reason[:500]}")
        deadline=time.monotonic()+delay
        while time.monotonic() < deadline:
            if stop_requested and stop_requested():
                return AgentStageResult(role, model, "failed", "", int((time.monotonic()-started)*1000), "advisor stopped by user")
            time.sleep(min(0.25, max(0.0, deadline-time.monotonic())))


def _extract_contract_sections(text: str, labels: tuple[str, ...]) -> dict[str, str]:
    """Extract required stage-contract sections without trusting model prose shape."""
    value = str(text or "").strip()
    found: dict[str, str] = {}
    if not value or not labels:
        return found
    escaped = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
    # Accept both strict LABEL: contracts and natural Markdown headings emitted by
    # models, including descriptive suffixes such as
    # `## R1 PRIMARY SOURCES — Authoritative References Needed` and
    # `# EVIDENCE GAPS (Must Be Resolved)`.  A heading must still start with an
    # exact required label, so ordinary prose mentioning a label is not treated
    # as a section boundary.
    pattern = re.compile(
        rf"(?im)^[ \t]*(?:#{{1,6}}[ \t]*)?(?:\*\*)?({escaped})"
        rf"(?:[ \t]*:[ \t]*|[ \t]*(?:[-–—(][^\n]*)?[ \t]*(?:\*\*)?[ \t]*(?:\n|$))"
    )
    matches = list(pattern.finditer(value))

    def heading_level(match: re.Match[str]) -> int:
        full = match.group(0).lstrip()
        marker = re.match(r"(#{1,6})[ \t]*", full)
        return len(marker.group(1)) if marker else 0

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        body = value[start:end].strip()
        if not body and index + 1 < len(matches):
            # Markdown parent sections often contain only required child headings,
            # e.g. `# RESEARCH PLAN` immediately followed by `## R1 ...`. Treat
            # that parent as substantively populated by its nested contract
            # sections, while an empty peer heading remains invalid.
            current_level = heading_level(match)
            next_level = heading_level(matches[index + 1])
            if current_level > 0 and next_level > current_level:
                body = "[nested required sections follow]"
        found[match.group(1).upper()] = body
    return found


def _deterministic_contract_fallback(text: str, required_sections: tuple[str, ...]) -> str:
    """Normalize a semantically useful but structurally invalid stage result without inventing facts."""
    sections = _extract_contract_sections(str(text or ""), required_sections)
    rows: list[str] = []
    for label in required_sections:
        body = str(sections.get(label.upper()) or "").strip()
        if not body:
            if label.upper() == "NEXT STAGE INSTRUCTIONS":
                body = "Continue from the authoritative user task and prior StageOff; preserve unresolved requirements and do not infer missing facts."
            else:
                body = "Not supplied by the model after bounded repair; treat this item as unresolved and preserve the authoritative user task and prior StageOff."
        rows.append(f"## {label}\n{body}")
    return "\n\n".join(rows)


def _observational_state_issues(text: str, *, role: str, workspace_root: str) -> list[str]:
    """Reject impossible completion/mutation claims in observational bootstrap stages."""
    if str(role or "") != "coordinator:plan_research":
        return []
    root = Path(workspace_root)
    try:
        meaningful = [
            path for path in root.iterdir()
            if path.name not in {".git", ".aicoder-team", ".venv", "node_modules", "__pycache__"}
        ]
    except OSError:
        return []
    if meaningful:
        return []
    value = str(text or "")
    contradiction = re.search(
        r"(?i)\b(?:task has been completed|files? (?:have|has) been created|"
        r"implementation (?:has been|is) completed|created in the workspace|project is complete)\b",
        value,
    )
    if contradiction:
        return [
            "observational state contradiction: bootstrap workspace is empty/read-only but output claims implementation or files already exist"
        ]
    return []


def _contract_issues(text: str, required_sections: tuple[str, ...]) -> list[str]:
    """Return deterministic reasons why a model stage output is not a usable contract."""
    value = str(text or "").strip()
    if not value:
        return ["empty output"]
    sections = _extract_contract_sections(value, required_sections)
    issues = [f"missing section: {label}" for label in required_sections if not sections.get(label.upper())]
    if required_sections and len(sections) == 0:
        # A common failure mode with text-tool protocol is returning tool JSON as the final answer.
        json_lines = []
        for line in value.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and ("tool" in parsed or ("name" in parsed and "arguments" in parsed)):
                json_lines.append(parsed)
        if json_lines or "TOOL_CALL " in value:
            issues.append("final output contains tool-call syntax instead of the required stage contract")
    return issues


def _call_stage_agent(
    *, client, model_client: ModelTransport, model: str, system: str, prompt: str,
    tools: list[dict], workspace_root: str, event_fn: EventFn | None, role: str,
    stop_requested: StopFn | None, approval_fn: Callable[[str, dict], bool] | None,
    required_sections: tuple[str, ...] = (), max_tokens: int = 6000,
    max_iterations: int = 40, request_timeout: int = 300,
    native_openrouter_tool_calling: bool = False,
) -> AgentStageResult:
    """Run an observational team stage inside a disposable isolated snapshot.

    Planning/coordinator stages keep the complete authenticated tool catalogue, but any
    accidental mutation is confined to the snapshot and discarded. This preserves the
    low-friction tool policy without allowing a planning model to alter the source tree.
    Paths are translated back to the canonical source root in the returned handoff.
    """
    backend = create_isolated_team_workspace(workspace_root, "ram")
    execution_root = str(backend.prepare())
    source_root = str(Path(workspace_root).expanduser().resolve(strict=False))

    def _to_snapshot(value: str) -> str:
        return str(value or "").replace(source_root, execution_root)

    def _to_source(value: str) -> str:
        return str(value or "").replace(execution_root, source_root)

    _emit(
        event_fn, "team_worker_event", role=role, event="runtime_status",
        category="workspace", status="isolated", phase="observational_stage",
        message="observational stage running in disposable isolated workspace; mutations cannot reach source",
        source_workspace=source_root, execution_workspace=execution_root,
    )
    try:
        result = _call_stage_agent_core(
            client=client, model_client=model_client, model=model, system=_to_snapshot(system),
            prompt=_to_snapshot(prompt), tools=tools, workspace_root=execution_root,
            event_fn=event_fn, role=role, stop_requested=stop_requested, approval_fn=approval_fn,
            required_sections=required_sections, max_tokens=max_tokens, max_iterations=max_iterations,
            request_timeout=request_timeout,
            native_openrouter_tool_calling=native_openrouter_tool_calling,
        )
        result.response = _to_source(result.response)
        result.error = _to_source(result.error)
        result.evidence = dict(result.evidence or {})
        result.evidence["isolated_observational_workspace"] = True
        return result
    finally:
        backend.abort()


def _call_stage_agent_core(
    *, client, model_client: ModelTransport, model: str, system: str, prompt: str,
    tools: list[dict], workspace_root: str, event_fn: EventFn | None, role: str,
    stop_requested: StopFn | None, approval_fn: Callable[[str, dict], bool] | None,
    required_sections: tuple[str, ...] = (), max_tokens: int = 6000,
    max_iterations: int = 40, request_timeout: int = 300,
    native_openrouter_tool_calling: bool = False,
) -> AgentStageResult:
    """Run a fresh tool-capable stage with bounded provider recovery and contract repair.

    All authenticated tools are visible. Stage policy decides which actions are permitted;
    capability hiding is not used as a substitute for runtime safety.
    """
    started = time.monotonic()
    base_prompt = str(prompt)
    current_prompt = base_prompt
    conversation: list[dict[str, Any]] = []
    repair_attempts = 0
    provider_resumes = 0
    last_error = ""
    stage_system = (
        build_system_prompt(tools, workspace_root).rstrip()
        + "\n\n## CURRENT TEAM STAGE\n" + system.strip()
        + _USER_CONSTRAINT_DISCIPLINE
        + _OBSERVATIONAL_STAGE_DISCIPLINE
    )

    while True:
        active_prompt = current_prompt
        runtime = NativeLightRuntime(
            client=client, model_client=model_client, initial_prompt=current_prompt,
            model=model, fallback_model=None, workspace_root=workspace_root,
            plan_workspace_root=workspace_root, protected_workspace_root=None,
            tools=[dict(tool) for tool in tools], system_prompt=stage_system,
            conversation=conversation, load_tools_on_start=True, quick_chat=False,
            persistent_plan=False, approval_fn=approval_fn,
            max_iterations=max(1, min(MAX_ITERATIONS, int(max_iterations))),
            max_output_tokens=max_tokens, max_tool_calls_per_turn=4,
            max_context_chars=_TEAM_OBSERVATIONAL_CONTEXT_CHARS,
            stop_requested=stop_requested,
            base_timeout=max(10, min(300, int(request_timeout))),
            event_fn=_worker_event_forwarder(event_fn, role),
            progressive_tool_disclosure=False,
            native_openrouter_tool_calling=bool(native_openrouter_tool_calling),
            allow_mixed_tool_protocol_final=True,
            allow_tool_free_final=True,
            enforce_post_mutation_verification=False,
        )
        run = runtime.run()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        response = str(run.response or "").strip()

        if run.status == "completed":
            contract_issues = _contract_issues(response, required_sections)
            state_issues = _observational_state_issues(response, role=role, workspace_root=workspace_root)
            issues = contract_issues + [issue for issue in state_issues if issue not in contract_issues]
            if not issues:
                return AgentStageResult(
                    role, run.model or model, "completed", response, elapsed_ms,
                    evidence={
                        "iterations": run.iterations, "tool_count": len(tools),
                        "contract_repairs": repair_attempts, "provider_resumes": provider_resumes,
                    },
                )
            if repair_attempts >= 2:
                normalized_source = "" if state_issues else response
                normalized = _deterministic_contract_fallback(normalized_source, required_sections)
                _emit(
                    event_fn, "team_worker_event", role=role, event="runtime_status",
                    category="contract", status="normalized", phase="stage_contract",
                    message="model contract remained structurally invalid after bounded repair; deterministic non-inventing envelope applied",
                )
                return AgentStageResult(
                    role, run.model or model, "completed", normalized, elapsed_ms,
                    evidence={"contract_issues": issues, "contract_normalized": True, "tool_count": len(tools)},
                )
            repair_attempts += 1
            _emit(
                event_fn, "team_worker_event", role=role, event="runtime_status",
                category="contract", status="repairing", phase="stage_contract",
                message=f"contract repair {repair_attempts}/2: {'; '.join(issues)[:1200]}",
            )
            conversation = []
            required_template = "\n\n".join(f"## {label}\n- ..." for label in required_sections)
            current_prompt = (
                "CONTRACT REPAIR. Return the stage handoff ONLY. Do not explain the repair, do not ask questions, "
                "and do not emit tool-call syntax in the final response. Every heading below is mandatory, must appear "
                "exactly once, and must contain substantive content. Reuse preserved evidence instead of wandering into "
                "unrelated tools. If evidence is unavailable, state that explicitly under the appropriate heading.\n\n"
                + "MANDATORY FINAL FORMAT (copy these headings exactly):\n" + required_template
                + "\n\nISSUES TO FIX:\n- " + "\n- ".join(issues)
                + "\n\nAUTHORITATIVE ORIGINAL STAGE TASK:\n" + base_prompt
                + "\n\nINVALID PREVIOUS RESPONSE (salvage valid facts only):\n" + response[:30000]
            )
            continue

        last_error = str(run.error or run.response or "stage runtime failed")
        transient = (
            run.status == "paused"
            and (getattr(run, "failure_category", "") == "transient" or _advisor_retryable(last_error))
        )
        if transient and not (stop_requested and stop_requested()):
            provider_resumes += 1
            if not _wait_before_resume(
                run, provider_resumes, event_fn=event_fn, role=role,
                phase="stage_provider_resume", stop_requested=stop_requested,
            ):
                break
            conversation = (
                _candidate_conversation(run, max_chars=_TEAM_RECOVERY_CONTEXT_CHARS)
                if not _is_incomplete_envelope_reason(last_error) else []
            )
            current_prompt = (
                f"AUTONOMOUS STAGE PROVIDER RESUME {provider_resumes} (unlimited). Continue the SAME stage from preserved evidence.\n"
                f"Previous transient failure: {last_error[:1600]}\n\n"
                "Do not restart completed tool work. Finish the required stage contract.\n\n"
                "AUTHORITATIVE ACTIVE CONTINUATION/REPAIR TASK (highest priority):\n"
                + active_prompt[:30000]
                + "\n\nAUTHORITATIVE ORIGINAL STAGE TASK (always retained across provider recovery):\n"
                + base_prompt[:30000]
            )
            continue
        break

    return AgentStageResult(
        role, model, "failed", "", int((time.monotonic() - started) * 1000),
        last_error or "stage runtime failed",
        evidence={"tool_count": len(tools), "provider_resumes": provider_resumes},
    )


def _task_handoff(task: str) -> HandoffEnvelope:
    # The original user task is an immutable run contract. Keep substantially more
    # room than ordinary compact handoffs so detailed acceptance criteria survive
    # bootstrap/research even when a coordinator summarizes them aggressively.
    return make_handoff("task", task, max_chars=16000)


def _research_plan_handoff(text: str) -> HandoffEnvelope:
    return make_handoff("research-contract", text, max_chars=4500)


def _code_plan_handoff(text: str) -> HandoffEnvelope:
    return make_handoff("code-contract", text, max_chars=9000, section_labels=CODE_PLAN_SECTIONS)


def _merge_plan_handoff(text: str) -> HandoffEnvelope:
    return make_handoff("merge-contract", text, max_chars=6000, section_labels=MERGE_PLAN_SECTIONS)




def _emit_stage_handoff(event_fn: EventFn | None, handoff: HandoffEnvelope, *, next_stage: TeamStage | str) -> None:
    target = next_stage.value if isinstance(next_stage, TeamStage) else str(next_stage)
    _emit(
        event_fn, "team_stage_handoff", source_stage=handoff.source_stage, next_stage=target,
        handoff_id=handoff.handoff_id, parent_handoff_id=handoff.parent_handoff_id,
        original_chars=handoff.original_chars, compact_chars=handoff.compact_chars,
        fresh_model_process=True,
    )

def _deterministic_greenfield_research_stageoff_review() -> str:
    return (
        "## STAGE SUMMARY\nResearch completed deterministically from the immutable user task and greenfield workspace state; no external research was required.\n\n"
        "## NEW FACTS\nNo meaningful implementation files exist yet beyond AICoder/Git metadata. No code, test, security, compatibility, or completion claim is established.\n\n"
        "## REQUIRED CHANGES\nAll implementation requirements and acceptance checks from the original user task remain authoritative and open for planning/coding.\n\n"
        "## COMPLETED ITEMS\nGreenfield research preflight only. No implementation item is marked complete.\n\n"
        "## OPEN ITEMS\nArchitecture decisions, implementation, tests, merge, deterministic acceptance checks, documentation, and final verification remain open.\n\n"
        "## RISKS\nDo not infer missing implementation, add unrelated setup work, weaken user constraints, or claim external evidence that was not gathered.\n\n"
        "## NEXT STAGE INSTRUCTIONS\nProceed to brainstorm/planning using the original user task as immutable contract. Generate implementation options without inventing existing code or unrelated infrastructure requirements."
    )


def _stage_payload_is_deterministic_greenfield_research(stage_name: str, stage_payload: Any) -> bool:
    if stage_name != TeamStage.RESEARCH.value or not isinstance(stage_payload, dict):
        return False
    reports = stage_payload.get("reports")
    if not isinstance(reports, list) or not reports:
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("evidence"), dict)
        and bool(item["evidence"].get("deterministic_greenfield"))
        for item in reports
    )


def _coordinate_stageoff(
    *, current: dict[str, Any], stage: TeamStage | str, stage_payload: Any,
    client, model_client: ModelTransport, coordinator_model: str | None, tools: list[dict],
    workspace_root: str, event_fn: EventFn | None, stop_requested: StopFn | None,
    request_timeout: int = 300, native_openrouter_tool_calling: bool = False,
    stageoff_path: str | Path | None = None,
) -> tuple[dict[str, Any], AgentStageResult | None, HandoffEnvelope]:
    """Append one stage to the cumulative run StageOff and let a fresh coordinator curate it."""
    stage_name = stage.value if isinstance(stage, TeamStage) else str(stage)
    previous = json.loads(json.dumps(current, ensure_ascii=False, default=str))
    stage_text = stage_payload if isinstance(stage_payload, str) else json.dumps(stage_payload, ensure_ascii=False, indent=2, default=str)
    coordinator: AgentStageResult | None = None
    review = ""
    deterministic_greenfield_research = _stage_payload_is_deterministic_greenfield_research(stage_name, stage_payload)
    if deterministic_greenfield_research:
        review = _deterministic_greenfield_research_stageoff_review()
        coordinator = AgentStageResult(
            role=f"coordinator:{stage_name}", model="deterministic", status="completed",
            response=review, elapsed_ms=0,
            evidence={"deterministic_greenfield": True, "model_skipped": True},
        )
        _emit(
            event_fn, "team_worker_event", role=f"coordinator:{stage_name}", event="runtime_status",
            category="research", status="deterministic", phase="stageoff_coordination",
            message="deterministic greenfield research reports detected; model StageOff coordinator skipped to avoid speculative setup/hallucination",
        )
    elif coordinator_model:
        coordinator = _call_stage_agent(
            client=client, model_client=model_client, model=coordinator_model,
            system=COORDINATOR_SYSTEM_PROMPT, tools=tools, workspace_root=workspace_root,
            prompt=(
                "You are the StageOff coordinator for a staged coding run. This is a FRESH model process. "
                "Reconcile the new stage output with cumulative state. You may reorganize working-memory wording, "
                "but never silently drop still-valid requirements, facts, failures, evidence gaps or acceptance criteria. "
                "The `user_task` field in CURRENT CUMULATIVE STAGEOFF is immutable and outranks every model summary; "
                "do not weaken, reinterpret, or mark any of its requirements complete without deterministic evidence. "
                "Use tools observationally when they help verify state.\n\n"
                "CURRENT CUMULATIVE STAGEOFF:\n" + json.dumps(previous, ensure_ascii=False, indent=2, default=str)
                + "\n\nNEW STAGE OUTPUT:\n" + stage_text
            ),
            required_sections=_STAGEOFF_COORDINATOR_SECTIONS, max_tokens=2200, max_iterations=30,
            event_fn=event_fn, role=f"coordinator:{stage_name}", stop_requested=stop_requested,
            approval_fn=_approval_with_task_backend_policy(_planning_approval, str(previous.get("user_task") or "")), request_timeout=request_timeout,
            native_openrouter_tool_calling=native_openrouter_tool_calling,
        )
        review = coordinator.response if coordinator.status == "completed" else (coordinator.error or coordinator.response)
    entry = {
        "stage": stage_name,
        "sequence": len(previous.get("stages") or []) + 1,
        "output": stage_payload,
        "coordinator_review": review,
        "coordinator_status": (coordinator.status if coordinator else "disabled"),
    }
    updated = previous
    updated.setdefault("stages", []).append(entry)
    updated["current_stage"] = stage_name
    updated["latest_coordinator_review"] = review
    if coordinator is not None and coordinator.status == "completed":
        sections = _extract_contract_sections(review, _STAGEOFF_COORDINATOR_SECTIONS)
        updated["working_memory"] = {
            "stage_summary": sections.get("STAGE SUMMARY", ""),
            "new_facts": sections.get("NEW FACTS", ""),
            "required_changes": sections.get("REQUIRED CHANGES", ""),
            "completed_items": sections.get("COMPLETED ITEMS", ""),
            "open_items": sections.get("OPEN ITEMS", ""),
            "risks": sections.get("RISKS", ""),
            "next_stage_instructions": sections.get("NEXT STAGE INSTRUCTIONS", ""),
        }
    handoff = make_handoff(
        "stageoff", json.dumps(updated, ensure_ascii=False, indent=2, default=str),
        max_chars=120000, source_stage=stage_name,
        parent_handoff_id=str(previous.get("handoff_id") or ""),
    )
    updated["handoff_id"] = handoff.handoff_id
    persisted_stageoff = str(stageoff_path or "")
    if stageoff_path is not None:
        try:
            target = Path(stageoff_path).expanduser().resolve(strict=False)
            atomic_write_text(target, json.dumps(updated, ensure_ascii=False, indent=2, default=str) + "\n")
            os.chmod(target, 0o600)
            persisted_stageoff = str(target)
        except OSError as exc:
            _emit(event_fn, "team_worker_event", role=f"coordinator:{stage_name}", event="error",
                  category="stageoff", message=f"stageoff persistence failed: {type(exc).__name__}: {exc}")
    _emit(
        event_fn, "team_stageoff", stage=stage_name, handoff_id=handoff.handoff_id,
        parent_handoff_id=handoff.parent_handoff_id, entries=len(updated.get("stages") or []),
        coordinator_status=(coordinator.status if coordinator else "disabled"), fresh_coordinator_process=bool(coordinator_model),
        stageoff_path=persisted_stageoff, stageoff=updated,
    )
    return updated, coordinator, handoff


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


def _observational_diagnostic_allowed(tool_name: str, args: dict) -> bool:
    """Allow a narrow set of read-only diagnostics in observational team stages.

    This does not relax the global privilege classifier. It only avoids treating
    common environment introspection as a research mutation.
    """
    import ast
    import re
    import shlex

    canonical = str(tool_name or "").strip().lower().rsplit(".", 1)[-1].rsplit(":", 1)[-1].rsplit("/", 1)[-1]

    def python_script_ok(script: str) -> bool:
        try:
            tree = ast.parse(str(script or ""), mode="exec")
        except SyntaxError:
            return False
        if not tree.body:
            return False
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Name) and call.func.id == "print":
                    if any(isinstance(child, ast.Call) for arg in call.args for child in ast.walk(arg)):
                        return False
                    continue
            return False
        return True

    def argv_ok(program: str, argv: list[str]) -> bool:
        program = str(program or "").strip().lower().rsplit("/", 1)[-1]
        if program in {"ls", "head", "tail", "which", "pwd", "cat", "grep", "find", "stat", "wc", "uname", "id"}:
            return True
        if program in {"python", "python3"}:
            if argv[:1] in [["--version"], ["-V"]]:
                return True
            if argv == ["-m", "pytest", "--version"]:
                return True
            if len(argv) == 2 and argv[0] == "-c":
                return python_script_ok(argv[1])
            return False
        if program in {"pip", "pip3"}:
            return bool(argv) and argv[0] in {"list", "show", "freeze", "--version", "-V"}
        return False

    if canonical in {"crawl", "crawl_url"}:
        # Web crawling is observational in team planning/research. It may populate
        # provider-side caches internally, but it does not mutate the project/source state.
        return True
    if canonical == "binary_exec":
        return argv_ok(str(args.get("program") or ""), [str(x) for x in (args.get("arguments") or [])])
    if canonical == "shell":
        command = str(args.get("command") or "").strip()
        if not command:
            return False
        command = re.sub(r"(?:^|\s)2>(?:/dev/null|&1)(?=\s|$)", " ", command)
        if re.search(r"(?<!2)[><]", command):
            return False
        for segment in re.split(r"(?:&&|\|\||;|\|)", command):
            text = segment.strip()
            if not text:
                continue
            try:
                parts = shlex.split(text)
            except ValueError:
                return False
            if not parts:
                continue
            if parts[0].lower().rsplit("/", 1)[-1] == "echo":
                continue
            if not argv_ok(parts[0], parts[1:]):
                return False
        return True
    return False


def _as_task_contract(task: str | TaskContract) -> TaskContract:
    return task if isinstance(task, TaskContract) else compile_task_contract(str(task or ""))


def _task_requires_external_research(task: str | TaskContract) -> bool:
    return _as_task_contract(task).external_research_required


def _task_forbids_triforce_backend(task: str | TaskContract) -> bool:
    return _as_task_contract(task).forbid_triforce_backend


def _approval_with_task_backend_policy(approval_fn: Callable[[str, dict], bool], task: str | TaskContract) -> Callable[[str, dict], bool]:
    """Wrap a stage approval policy with authoritative TriForce backend isolation."""
    def approval(tool_name: str, args: dict) -> bool:
        return approval_fn(tool_name, args)

    for attr in (
        "_aicoder_autonomous_policy",
        "_aicoder_policy_denial_is_error",
        "_aicoder_enforce_all_tools",
    ):
        if hasattr(approval_fn, attr):
            setattr(approval, attr, getattr(approval_fn, attr))
    contract = _as_task_contract(task)
    approval._aicoder_forbid_triforce_backend = contract.forbid_triforce_backend
    approval._aicoder_task_contract = contract
    return approval

def _research_approval_for_task(task: str | TaskContract) -> Callable[[str, dict], bool]:
    contract = _as_task_contract(task)
    # Research is allowed to use read-only external sources by default. Whether
    # external research is *required* is a separate planning signal. Only an
    # explicit research-stage web prohibition disables those tools.
    external_allowed = not contract.forbid_research_web

    def approval(tool_name: str, args: dict) -> bool:
        canonical = str(tool_name or "").strip().lower().rsplit(".", 1)[-1].rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        payload = dict(args or {})
        if not external_allowed and canonical in {"search", "crawl", "crawl_url", "web_fetch", "web_fetch_local", "browser", "browser_search"}:
            return False
        # Common diagnostic tools with required selectors must never be invoked as
        # empty speculative probes during autonomous observational stages.
        required_selectors = {"config": ("key",)}
        required = required_selectors.get(canonical, ())
        if required and any(not str(payload.get(key) or "").strip() for key in required):
            return False
        return _research_approval(tool_name, payload)

    approval._aicoder_autonomous_policy = True
    approval._aicoder_policy_denial_is_error = False
    approval._aicoder_external_research_allowed = external_allowed
    approval._aicoder_enforce_all_tools = True
    approval._aicoder_forbid_triforce_backend = contract.forbid_triforce_backend
    approval._aicoder_task_contract = contract
    approval._aicoder_allow_research_web = external_allowed
    return approval


def _research_approval(tool_name: str, args: dict) -> bool:
    """Read-only autonomous policy for team research.

    Research sees the full authenticated catalog for capability awareness, but the
    runtime enforces the observational contract: no state mutation, elevation,
    deletion, destructive command, security change, or workspace escape.
    """
    from .executor import is_destructive
    from .privileges import assess_execution
    risk = assess_execution(tool_name, args, destructive=is_destructive(str(args.get("command") or "")))
    canonical = str(tool_name or "").strip().lower().rsplit(".", 1)[-1].rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    verification_only = canonical in {"test", "lint"}
    diagnostic_only = _observational_diagnostic_allowed(tool_name, args)
    return not bool(
        args.get("_workspace_escape") or (risk.mutation and not verification_only and not diagnostic_only)
        or risk.elevation or risk.deletion or risk.destructive or risk.security_change
    )

_research_approval._aicoder_autonomous_policy = True
_research_approval._aicoder_policy_denial_is_error = False


def _planning_approval(tool_name: str, args: dict) -> bool:
    """Low-friction autonomous policy for coordinator/planning stages.

    Unknown shell/binary commands are not blocked just because they are not on a
    read-only whitelist. Hard boundaries remain: no workspace escape, elevation,
    deletion, destructive command pattern, or security-boundary change.
    """
    from .executor import is_destructive
    from .privileges import assess_execution
    risk = assess_execution(tool_name, args, destructive=is_destructive(str(args.get("command") or "")))
    canonical = str(tool_name or "").strip().lower().rsplit(".", 1)[-1].rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    verification_only = canonical in {"test", "lint"}
    diagnostic_only = _observational_diagnostic_allowed(tool_name, args)
    # Planning/coordinator stages must not fan out extra model subagents. The staged
    # pipeline already launches dedicated research/coding workers immediately after
    # planning; duplicate subagent delegation only increases provider pressure and
    # repeats the same R1-R4 work.
    duplicate_stage_fanout = canonical == "subagent_run"
    return not bool(
        duplicate_stage_fanout or args.get("_workspace_escape")
        or (risk.mutation and not verification_only and not diagnostic_only)
        or risk.elevation or risk.deletion or risk.destructive or risk.security_change
    )


_planning_approval._aicoder_autonomous_policy = True
_planning_approval._aicoder_policy_denial_is_error = False


def _workspace_has_meaningful_project_files(root: str | Path) -> bool:
    base = Path(root)
    ignored = {".git", ".aicoder-team", ".venv", "node_modules", "__pycache__"}
    try:
        for path in base.rglob("*"):
            rel = path.relative_to(base)
            if any(part in ignored for part in rel.parts):
                continue
            if path.is_file() or path.is_symlink():
                return True
        return False
    except OSError:
        # If inspection fails, do not assume greenfield; fall back to model research.
        return True


def _deterministic_greenfield_bootstrap_plan(task: str) -> str:
    """Bootstrap StageOff without paraphrasing away greenfield user requirements."""
    task_text = str(task or "").strip()
    return (
        "## SESSION MEMORY\n"
        "AUTHORITATIVE USER TASK (verbatim; preserve every requirement and prohibition):\n"
        + task_text
        + "\n\nGreenfield state: no meaningful implementation files exist yet. No implementation, test, documentation, security, compatibility, or completion claim is established.\n\n"
        "## RESEARCH PLAN\n"
        "This is a self-contained greenfield task. Perform deterministic local/task preflight only; external research is not required. Preserve the user task unchanged and do not turn missing files into a blocker.\n\n"
        "## R1 PRIMARY SOURCES\n"
        "Use the explicit user task as the primary requirements source and confirm only the greenfield workspace state. Do not invent external sources.\n\n"
        "## R2 BEST PRACTICES\n"
        "Do not present generic architecture advice as research evidence before code exists. Defer design alternatives to brainstorm/planning.\n\n"
        "## R3 SECURITY RELIABILITY\n"
        "No implementation exists to audit yet. Record that security/reliability verification must occur after code exists; do not infer findings.\n\n"
        "## R4 ALTERNATIVE ARCHITECTURES\n"
        "No existing architecture exists to compare. Defer alternative designs to brainstorm and preserve all explicit user constraints.\n\n"
        "## EVIDENCE GAPS\n"
        "Implementation evidence, tests, runtime behavior, documentation, merge state, and acceptance results do not exist yet and must remain open until deterministically verified.\n\n"
        "## NEXT STAGE INSTRUCTIONS\n"
        "Run the deterministic greenfield research preflight, then proceed to brainstorm/planning from the immutable user task. Do not add unrelated infrastructure/setup requirements or mark implementation work complete."
    )


def _greenfield_self_contained_research_report(role: str, task: str) -> str:
    role_note = {
        "primary_sources": "No external/current API fact is required by the task; the explicit user task is the primary requirements source.",
        "best_practices": "There is no implementation yet to assess against repository-specific practice; architecture advice belongs to brainstorm/planning, not evidence.",
        "security_reliability": "There is no implementation yet to audit; reliability/security verification must be performed after code exists.",
        "alternative_architectures": "There is no existing architecture to compare; alternative designs belong to the brainstorm stage rather than being presented as research facts.",
    }.get(str(role or ""), "No implementation evidence exists yet; preserve the user task as authoritative.")
    return (
        "## FINDINGS\n"
        + role_note
        + " The project is greenfield: no meaningful implementation files exist beyond AICoder/Git metadata.\n\n"
        "## SOURCES\nOriginal user task and local workspace inspection only; external research is not required for this self-contained task.\n\n"
        "## APPLICABILITY\nPreserve every explicit user requirement and acceptance check unchanged. Treat missing implementation files as expected greenfield state, not a blocker or completed work.\n\n"
        "## RISKS\nNo implementation evidence exists yet, so do not claim code quality, test coverage, security, compatibility, or completion.\n\n"
        "## RECOMMENDATIONS\nProceed to brainstorm and implementation planning using the immutable user task; inspect and verify the actual code once coding begins."
    )


def _run_researcher(
    *, client, model_client: ModelTransport, model: str, role: str,
    source_workspace: str, tools: list[dict], stop_requested: StopFn | None,
    stage_input: HandoffEnvelope | None = None, task: str = "", research_plan: str = "",
    native_openrouter_tool_calling: bool = False, event_fn: EventFn | None = None,
    request_timeout: int = 300,
) -> AgentStageResult:
    """Run a researcher against a disposable snapshot and discard any accidental writes."""
    if (
        stage_input is not None
        and str(task or "").strip()
        and not _task_requires_external_research(task)
        and not _workspace_has_meaningful_project_files(source_workspace)
    ):
        report = _greenfield_self_contained_research_report(role, task)
        _emit(
            event_fn, "team_worker_event", role=f"research:{role}", event="runtime_status",
            category="research", status="deterministic", phase="greenfield_preflight",
            message="self-contained greenfield task detected; using deterministic task/workspace evidence instead of speculative external/model research",
        )
        return AgentStageResult(
            role=f"research:{role}", model=model or "deterministic", status="completed",
            response=report, elapsed_ms=0,
            evidence={
                "deterministic_greenfield": True,
                "externally_verified": False,
                "external_tools": [],
                "successful_tools": ["workspace_preflight"],
            },
        )
    backend = create_isolated_team_workspace(source_workspace, "ram")
    execution_root = str(backend.prepare())
    source_root = str(Path(source_workspace).expanduser().resolve(strict=False))

    def _mapped_handoff(value: HandoffEnvelope | None) -> HandoffEnvelope | None:
        if value is None:
            return None
        return HandoffEnvelope(
            kind=value.kind, handoff_id=value.handoff_id,
            raw=str(value.raw).replace(source_root, execution_root),
            compact=str(value.compact).replace(source_root, execution_root),
            source_stage=value.source_stage, parent_handoff_id=value.parent_handoff_id,
        )

    _emit(
        event_fn, "team_worker_event", role=f"research:{role}", event="runtime_status",
        category="workspace", status="isolated", phase="research",
        message="research worker running in disposable isolated workspace; mutations cannot reach source",
        source_workspace=source_root, execution_workspace=execution_root,
    )
    try:
        result = _run_researcher_core(
            client=client, model_client=model_client, model=model, role=role,
            source_workspace=execution_root, tools=tools, stop_requested=stop_requested,
            stage_input=_mapped_handoff(stage_input),
            task=str(task or "").replace(source_root, execution_root),
            research_plan=str(research_plan or "").replace(source_root, execution_root),
            native_openrouter_tool_calling=native_openrouter_tool_calling, event_fn=event_fn,
            request_timeout=request_timeout,
        )
        result.response = str(result.response or "").replace(execution_root, source_root)
        result.error = str(result.error or "").replace(execution_root, source_root)
        result.evidence = dict(result.evidence or {})
        result.evidence["isolated_observational_workspace"] = True
        return result
    finally:
        backend.abort()


def _explicit_forbidden_terms(task: str) -> list[str]:
    terms: list[str] = []
    text = str(task or "")
    patterns = (
        r"(?im)(?:^|[.;]\s*|[-*]\s*)do not\s+(?:require|use|install|add|include|depend on)\s+([^\n.;]+)",
        r"(?im)(?:^|[.;]\s*|[-*]\s*)no\s+([^\n.;]+)",
        r"(?im)\bwithout\s+([^\n.;]+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            phrase = match.group(1)
            for item in re.split(r"\s*(?:,|\bor\b|\band\b)\s*", phrase, flags=re.IGNORECASE):
                value = re.sub(r"^(?:any|the|a|an)\s+", "", item.strip(), flags=re.IGNORECASE)
                value = value.strip(" `*'\"")
                if 2 <= len(value) <= 80:
                    terms.append(value.lower())
    return list(dict.fromkeys(terms))


def _research_constraint_issues(response: str, task: str) -> list[str]:
    sections = _extract_contract_sections(str(response or ""), RESEARCH_SECTIONS)
    recommendations = str(sections.get("RECOMMENDATIONS", ""))
    if not recommendations:
        return []
    positive = re.compile(r"(?i)\b(?:recommend|consider|use|install|add|include|adopt|depend|require|choose)\b")
    negative = re.compile(r"(?i)\b(?:avoid|do not|don't|must not|never|without|no)\b")
    issues: list[str] = []
    for line in recommendations.splitlines():
        low = line.lower()
        if not positive.search(line) or negative.search(line):
            continue
        for term in _explicit_forbidden_terms(task):
            candidates = {term, term[:-1] if term.endswith("s") else term}
            if any(candidate and candidate in low for candidate in candidates):
                issues.append(f"recommendation violates explicit user prohibition: {term}")
    return list(dict.fromkeys(issues))


def _research_grounding_issues(response: str, *, external_allowed: bool, evidence_events: list[dict[str, Any]]) -> list[str]:
    if external_allowed:
        return []
    sections = _extract_contract_sections(str(response or ""), RESEARCH_SECTIONS)
    sources = str(sections.get("SOURCES", ""))
    if not sources:
        return []
    successful_external = {
        str(item.get("name") or "") for item in evidence_events
        if item.get("kind") == "tool_result"
        and str(item.get("name") or "") in {"search", "crawl", "crawl_url", "web_fetch_local"}
        and not bool(item.get("is_error"))
        and "stage_policy_denied" not in str(item.get("result") or "")
    }
    if successful_external:
        return []
    if re.search(r"(?i)(?:https?://|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|dev)\b)", sources):
        return ["source grounding violation: external source/URL claimed although external research was disabled and no successful external tool result exists"]
    return []


def _sanitize_self_contained_research(response: str) -> str:
    sections = _extract_contract_sections(str(response or ""), RESEARCH_SECTIONS)
    if not sections:
        return str(response or "")
    url_re = re.compile(r"(?i)(?:https?://|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|dev)\b)")
    for label in RESEARCH_SECTIONS:
        body = str(sections.get(label, ""))
        if label == "SOURCES":
            sections[label] = "Original user task and successful local repository/tool evidence only; external research was disabled for this self-contained task."
            continue
        kept = [line for line in body.splitlines() if not url_re.search(line)]
        sections[label] = "\n".join(kept).strip()
    return "\n\n".join(f"## {label}\n{sections.get(label, '').strip()}" for label in RESEARCH_SECTIONS)


def _deterministic_research_fallback(*, external_allowed: bool) -> str:
    source_line = (
        "No verified external source is asserted; use only successful tool evidence and the original user task."
        if external_allowed else
        "Original user task and local repository/tool evidence only; external research was disabled for this self-contained task."
    )
    return (
        "## FINDINGS\nNo additional research fact is asserted after bounded contract repair; the original user task and repository state remain authoritative.\n\n"
        f"## SOURCES\n{source_line}\n\n"
        "## APPLICABILITY\nProceed from the explicit user requirements and inspect the repository directly during planning/coding.\n\n"
        "## RISKS\nResearch evidence is incomplete; do not infer missing facts or weaken user constraints.\n\n"
        "## RECOMMENDATIONS\nPreserve every explicit user requirement and continue with repository-grounded implementation planning."
    )


def _sanitize_research_constraints(response: str, task: str) -> str:
    forbidden = _explicit_forbidden_terms(task)
    if not forbidden:
        return str(response or "")
    sections = _extract_contract_sections(str(response or ""), RESEARCH_SECTIONS)
    recommendations = str(sections.get("RECOMMENDATIONS", ""))
    positive = re.compile(r"(?i)\b(?:recommend|consider|use|install|add|include|adopt|depend|require|choose)\b")
    negative = re.compile(r"(?i)\b(?:avoid|do not|don't|must not|never|without|no)\b")
    kept: list[str] = []
    for line in recommendations.splitlines():
        low = line.lower()
        violates = False
        if positive.search(line) and not negative.search(line):
            for term in forbidden:
                candidates = {term, term[:-1] if term.endswith("s") else term}
                if any(candidate and candidate in low for candidate in candidates):
                    violates = True
                    break
        if not violates:
            kept.append(line)
    sections["RECOMMENDATIONS"] = "\n".join(kept).strip() or "Respect the explicit user constraints; no additional recommendation is justified."
    return "\n\n".join(f"## {label}\n{sections.get(label, '').strip()}" for label in RESEARCH_SECTIONS)


def _run_researcher_core(
    *, client, model_client: ModelTransport, model: str, role: str,
    source_workspace: str, tools: list[dict], stop_requested: StopFn | None,
    stage_input: HandoffEnvelope | None = None, task: str = "", research_plan: str = "",
    native_openrouter_tool_calling: bool = False, event_fn: EventFn | None = None,
    request_timeout: int = 300,
) -> AgentStageResult:
    if stage_input is None:
        legacy = {"user_task": task, "research_contract": research_plan}
        stage_input = make_handoff(
            "stageoff", json.dumps(legacy, ensure_ascii=False, indent=2),
            max_chars=120000, source_stage="plan_research",
        )
    external_research_allowed = _task_requires_external_research(task)
    research_scope_note = (
        "EXTERNAL RESEARCH REQUIRED/ALLOWED: the original task depends on outside or freshness-sensitive facts.\n"
        if external_research_allowed else
        "SELF-CONTAINED TASK: external web search/crawl/fetch is disabled for this research stage. Use the immutable user task and repository evidence; do not pad the report with generic web sources. An empty greenfield workspace is expected evidence, not a blocker: do not ask the user to create files and do not treat missing implementation files as inability to complete the research contract.\n"
    )
    prompt = (
        "PREVIOUS STAGE OUTPUT (authoritative input; do not infer hidden prior conversation):\n"
        f"{stage_input.render()}\n\n"
        "AUTHORITATIVE ORIGINAL USER TASK (immutable; never replace it with a coordinator summary):\n"
        + str(task or "")[:16000] + "\n\n"
        + research_scope_note + "\n"
        + f"Repository root for read-only inspection: {source_workspace}\n\n"
        + RESEARCH_INSTRUCTIONS[role] + "\n\n" + RESEARCH_OUTPUT_CONTRACT
    )
    system = build_system_prompt(tools, source_workspace).rstrip() + (
        "\n\n## RESEARCH AGENT ROLE\n" + RESEARCH_INSTRUCTIONS[role] + "\n\n" + RESEARCH_OUTPUT_CONTRACT
        + _USER_CONSTRAINT_DISCIPLINE
        + _OBSERVATIONAL_STAGE_DISCIPLINE
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
    attempt = 0
    contract_repairs = 0
    non_provider_resumes = 0
    while True:
        attempt += 1
        active_prompt = current_prompt
        runtime = NativeLightRuntime(
            client=client, model_client=model_client, initial_prompt=current_prompt,
            model=model, fallback_model=None, workspace_root=source_workspace,
            plan_workspace_root=source_workspace, protected_workspace_root=None,
            tools=tools, system_prompt=system, load_tools_on_start=True,
            quick_chat=False, persistent_plan=False, approval_fn=_research_approval_for_task(task),
            max_iterations=60, max_output_tokens=1200, max_tool_calls_per_turn=4,
            max_context_chars=_TEAM_OBSERVATIONAL_CONTEXT_CHARS, stop_requested=stop_requested,
            progressive_tool_disclosure=False,
            base_timeout=max(10, min(300, int(request_timeout))), event_fn=research_event,
            native_openrouter_tool_calling=bool(native_openrouter_tool_calling),
            allow_mixed_tool_protocol_final=True,
            enforce_post_mutation_verification=False,
            conversation=conversation,
        )
        result = runtime.run()
        if result.status == "completed":
            contract_issues = _contract_issues(str(result.response or ""), RESEARCH_SECTIONS)
            constraint_issues = _research_constraint_issues(str(result.response or ""), task)
            grounding_issues = _research_grounding_issues(
                str(result.response or ""), external_allowed=external_research_allowed,
                evidence_events=evidence_events,
            )
            for issue in constraint_issues + grounding_issues:
                if issue not in contract_issues:
                    contract_issues.append(issue)
            if contract_issues and contract_repairs < 4:
                contract_repairs += 1
                _emit(
                    event_fn, "team_worker_event", role=f"research:{role}", event="runtime_status",
                    category="contract", status="repairing", phase="research_contract",
                    message=f"research contract repair {contract_repairs}/4: {'; '.join(contract_issues)[:1000]}",
                )
                conversation = []
                current_prompt = (
                    "RESEARCH CONTRACT REPAIR. Preserve all valid evidence already gathered. "
                    "Use tools only for missing evidence. Your FINAL response must contain non-empty "
                    "FINDINGS, SOURCES, APPLICABILITY, RISKS, RECOMMENDATIONS sections and must not be tool-call syntax.\n\n"
                    "ISSUES:\n- " + "\n- ".join(contract_issues)
                    + "\n\nORIGINAL ASSIGNMENT:\n" + prompt
                    + "\n\nINVALID PREVIOUS RESPONSE:\n" + str(result.response or "")[:24000]
                )
                continue
            if contract_issues:
                sanitized = _sanitize_research_constraints(str(result.response or ""), task)
                if not external_research_allowed:
                    sanitized = _sanitize_self_contained_research(sanitized)
                remaining = (
                    _contract_issues(sanitized, RESEARCH_SECTIONS)
                    + _research_constraint_issues(sanitized, task)
                    + _research_grounding_issues(
                        sanitized, external_allowed=external_research_allowed,
                        evidence_events=evidence_events,
                    )
                )
                if remaining:
                    sanitized = _deterministic_research_fallback(external_allowed=external_research_allowed)
                    remaining = _contract_issues(sanitized, RESEARCH_SECTIONS)
                result.response = sanitized
                if remaining:
                    result.status = "failed"
                    result.error = "research output contract invalid: " + "; ".join(remaining)
                else:
                    _emit(
                        event_fn, "team_worker_event", role=f"research:{role}", event="runtime_status",
                        category="contract", status="normalized", phase="research_contract",
                        message="research report normalized after bounded repair using only grounded evidence and explicit user constraints",
                    )
        if result.status != "paused" or (stop_requested and stop_requested()):
            break
        reason = str(result.response or result.error or "research worker paused")
        provider_retry = _provider_pause_is_retryable(result)
        if not provider_retry:
            if non_provider_resumes >= 4:
                break
            non_provider_resumes += 1
        resume_number = attempt
        if not _wait_before_resume(
            result, resume_number, event_fn=event_fn, role=f"research:{role}",
            phase="research_resume", stop_requested=stop_requested,
        ):
            break
        if _is_incomplete_envelope_reason(reason):
            conversation = []
            current_prompt = _fresh_worker_recovery_prompt(result, reason, resume_number, label=f"research:{role}") + (
                "\n\nContinue read-only, gather only missing evidence, then return the required compact research report."
            )
            recovery_status = "fresh_chat"
        else:
            conversation = _candidate_conversation(
                result, max_chars=_TEAM_RECOVERY_CONTEXT_CHARS
            )
            current_prompt = (
                f"AUTONOMOUS RESEARCH RESUME {resume_number}{' (unlimited provider recovery)' if provider_retry else '/4'}\n\nPrevious pause reason:\n{reason[:1600]}\n\n"
                "Continue the same read-only research assignment from existing evidence. Do not restart or modify state. "
                "Resolve the blocker, gather only missing evidence, then return the required compact research report.\n\n"
                "AUTHORITATIVE ACTIVE RESEARCH CONTINUATION/REPAIR TASK (highest priority):\n"
                + active_prompt[:30000]
                + "\n\nAUTHORITATIVE ORIGINAL RESEARCH ASSIGNMENT:\n"
                + prompt[:30000]
            )
            recovery_status = "resuming"
        _emit(event_fn, "team_worker_event", role=f"research:{role}", event="runtime_status",
              category="recovery", status=recovery_status, phase="research_resume",
              message=f"automatic research resume {resume_number}{' (unlimited provider recovery)' if provider_retry else '/4'}: {reason[:500]}")
    assert result is not None
    research_tool_names = {
        str(item.get("name") or "") for item in evidence_events
        if item.get("kind") == "tool_result"
        and not bool(item.get("is_error"))
        and "stage_policy_denied" not in str(item.get("result") or "")
    }
    external_tools = sorted(name for name in research_tool_names if name in {
        "search", "crawl", "web_fetch_local",
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
    seen_roles: set[str] = set()

    def add(label: str, model: str | None, perspective: str) -> None:
        value = str(model or "").strip()
        role_key = str(label or "").strip().lower()
        if not value or not role_key or role_key in seen_roles or len(rows) >= max(1, int(limit)):
            return
        # Distinct perspectives remain valuable even when every slot uses the same
        # provider/model. Each participant is a fresh isolated model process.
        seen_roles.add(role_key)
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


def _candidate_prompt(stage_input: HandoffEnvelope, strategy: str) -> str:
    return (
        "PREVIOUS STAGE OUTPUT (authoritative input; this is a fresh model process):\n"
        f"{stage_input.render()}\n\n"
        f"Your strategy emphasis is {strategy}. Implement the complete contract contained in the handoff, "
        "not only the strategy-specific parts. Do not assume any conversation from an earlier stage."
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

_TEAM_CANDIDATE_MAX_AUTO_RESUMES = 2
_TEAM_CANDIDATE_MAX_VERIFICATION_REPAIRS = 2
_TEAM_MERGE_MAX_AUTO_RESUMES = 4


def _resume_delay_seconds(run: AgentRunResult, attempt: int) -> float:
    """Respect provider cooldown hints; otherwise use bounded exponential backoff."""
    retry_after = getattr(run, "retry_after", None)
    if isinstance(retry_after, int) and retry_after > 0:
        return float(min(300, retry_after))
    reason = str(run.response or run.error or "").lower()
    if getattr(run, "failure_category", "") == "transient" or any(
        marker in reason for marker in ("transient", "timeout", "readtimeout", "overloaded", "http 429", "http 5")
    ):
        return float(min(30, 2 ** max(0, attempt - 1)))
    return 0.0


def _wait_before_resume(
    run: AgentRunResult, attempt: int, *, event_fn: EventFn | None, role: str,
    phase: str, stop_requested: StopFn | None,
) -> bool:
    """Wait for provider recovery without losing cancellation responsiveness."""
    delay = _resume_delay_seconds(run, attempt)
    if delay <= 0:
        return True
    _emit(
        event_fn, "team_worker_event", role=role, event="runtime_status", category="recovery",
        status="backoff", phase=phase,
        message=f"resume {attempt}: provider recovery backoff {int(delay)}s before continuing preserved state",
        retry_after=int(delay),
    )
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if stop_requested and stop_requested():
            return False
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return True


def _provider_pause_is_retryable(run: AgentRunResult) -> bool:
    """Return True for provider/transport/envelope pauses that must retry until stopped."""
    if run.status != "paused":
        return False
    reason = str(run.response or run.error or "")
    return bool(
        getattr(run, "failure_category", "") == "transient"
        or _is_incomplete_envelope_reason(reason)
        or _advisor_retryable(reason)
    )


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
        # Runtime-level authoritative verification stagnation is terminal for
        # this candidate. Auto-resuming it only repeats the same failing
        # strategy and can block already-verified sibling candidates from
        # reaching ensemble merge. Provider/transport pauses remain resumable.
        "authoritative verification reproduced the same non-transient failure",
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


def _candidate_verification_repair_prompt(evaluation: dict[str, Any], attempt: int) -> str:
    """Return a focused repair turn for a candidate workspace rejected by team verification."""
    checks = evaluation.get("checks") or {}
    failed_rows: list[str] = []
    for name, row in checks.items():
        if not isinstance(row, dict) or row.get("ok") is not False:
            continue
        output = str(row.get("output") or "").strip().replace("\x00", "")
        failed_rows.append(f"- {name}: {output[:1800] or 'failed'}")
    evidence = evaluation.get("test_evidence") or {}
    if evidence.get("behavior_change") and not evidence.get("coverage_evidence_ok"):
        sources = ", ".join(str(path) for path in (evidence.get("source_paths") or [])[:12]) or "(unknown source paths)"
        failed_rows.append(
            "- regression-test-evidence: source code changed without a changed/new test. "
            f"Affected source paths: {sources}"
        )
    failures = "\n".join(failed_rows) or "- candidate verification did not pass; inspect the project verification plan and repair it"
    return (
        f"AUTONOMOUS CANDIDATE VERIFICATION REPAIR {attempt}/{_TEAM_CANDIDATE_MAX_VERIFICATION_REPAIRS}\n\n"
        "This is a FRESH repair chat. The current isolated RAM workspace is the authoritative state. "
        "Do not rely on filenames, commands, or assumptions from any previous assistant conversation. "
        "Inspect the current workspace first. If task context is needed, read `.aicoder-team/coder-handoff.json`. "
        "Preserve correct implementation work and repair only the concrete deterministic failures below.\n\n"
        f"FAILED TEAM VERIFICATION:\n{failures}\n\n"
        "Use repository-native test configuration and the provided test tool. Do not invent test paths or unsupported "
        "pytest flags, and do not create virtual environments or install packages just to make verification run. "
        "If behavior-changing source code was modified, add or update a focused regression test that exercises the "
        "changed behavior. Fix genuine failures rather than weakening tests. Finish with DONE: only after the repaired "
        "candidate is ready for deterministic reevaluation."
    )


def _candidate_rejection_reason(candidate: CandidateResult) -> str:
    evaluation = candidate.evaluation or {}
    checks = evaluation.get("checks") or {}
    failed_checks = [
        str(name) for name, row in checks.items()
        if isinstance(row, dict) and row.get("required", True) and row.get("ok") is False
    ]
    parts = [f"slot {candidate.slot} ({candidate.run.model or candidate.model}) status={candidate.run.status}"]
    if candidate.run.error:
        parts.append(f"error={str(candidate.run.error)[:500]}")
    if failed_checks:
        parts.append("failed_checks=" + ",".join(failed_checks[:8]))
    evidence = evaluation.get("test_evidence") or {}
    if evidence.get("behavior_change") and not evidence.get("coverage_evidence_ok"):
        parts.append("missing regression-test change")
    delta = evaluation.get("delta") or {}
    parts.append(
        f"changed={int(delta.get('changed_count') or 0)} deleted={int(delta.get('deleted_count') or 0)}"
    )
    return " ".join(parts)


def _is_incomplete_envelope_reason(reason: str) -> bool:
    text = str(reason or "").lower()
    return (
        "transient incomplete chat response" in text
        or "no recognized assistant response envelope" in text
        or "no usable final response" in text
        or "final-response repair request" in text
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


def _candidate_conversation(
    run: AgentRunResult, *, max_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Carry recent model/tool history forward without duplicating the old system prompt.

    When a character cap is supplied, reuse the runtime's tool-pair-aware trimmer so
    provider recovery cannot grow context indefinitely across fresh model processes.
    """
    rows = [
        dict(message) for message in (run.messages or [])
        if isinstance(message, dict) and str(message.get("role") or "") != "system"
    ]
    if isinstance(max_chars, int) and max_chars > 0 and rows:
        bounded = trim_messages(
            [{"role": "system", "content": "recovery-context"}, *rows],
            max_chars=max_chars,
        )
        rows = [dict(message) for message in bounded[1:]]
    return rows


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
    slot: int, model: str, strategy: str, stage_input: HandoffEnvelope | None = None,
    task: str = "", plan: str = "", coordinator: str = "",
    tools: list[dict], stop_requested: StopFn | None, native_openrouter_tool_calling: bool = False,
    request_timeout: int = 300, event_fn: EventFn | None = None, liveness_timeout_s: int = 1200,
    stage_handoffs: dict[str, Any] | None = None,
) -> CandidateResult:
    if stage_input is None:
        legacy_stageoff = {
            "schema": "aicoder-stageoff-v1", "user_task": task,
            "stages": [{"stage": "plan_code", "output": {"implementation_contract": plan},
                        "coordinator_review": coordinator}],
        }
        stage_input = make_handoff(
            "stageoff", json.dumps(legacy_stageoff, ensure_ascii=False, indent=2),
            max_chars=120000, source_stage="plan_code",
        )
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
        contract = compile_task_contract(task)
        system = (
            build_system_prompt(tools, str(backend.info.execution_root)).rstrip()
            + "\n\n" + CODER_SYSTEM_TEMPLATE.format(slot=slot, strategy=strategy)
            + "\n\n" + contract.prompt_projection()
            + test_runtime_note
        )
        started = time.monotonic()
        last_progress_at = [started]
        worker_role = f"coder:{slot}"
        backend.write_candidate_artifact(
            ".aicoder-team/coder-handoff.json",
            json.dumps({
                "stage_input": stage_input.render(), "handoff_id": stage_input.handoff_id,
                "source_stage": stage_input.source_stage, "strategy": strategy,
            }, ensure_ascii=False, indent=2),
        )
        if stage_handoffs:
            backend.write_candidate_artifact(
                ".aicoder-team/handoffs.json",
                json.dumps(stage_handoffs, ensure_ascii=False, indent=2),
            )
            if isinstance(stage_handoffs.get("stageoff"), dict):
                backend.write_candidate_artifact(
                    ".aicoder-team/stageoff.json",
                    json.dumps(stage_handoffs["stageoff"], ensure_ascii=False, indent=2),
                )
        prompt = _candidate_prompt(stage_input, strategy)
        forward = _worker_event_forwarder(event_fn, worker_role)

        def candidate_event(kind: str, payload: dict[str, Any]) -> None:
            # Liveness is progress/activity based, not a hard wall-clock cap.
            # A candidate actively editing/testing must not be killed just because
            # the model is slow and the task legitimately exceeds 20 minutes.
            if kind in {"model_response", "tool_result", "completion_signal"}:
                last_progress_at[0] = time.monotonic()
            forward(kind, payload)

        conversation: list[dict[str, Any]] = []
        run: AgentRunResult | None = None
        auto_resumes = 0
        verification_repairs = 0
        cached_verification: dict[str, Any] = {}

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
                quick_chat=False, persistent_plan=False, approval_fn=_approval_with_task_backend_policy(_candidate_approval, contract),
                max_iterations=None, max_output_tokens=12000,
                stop_requested=lambda: bool(
                    (stop_requested and stop_requested())
                    or (time.monotonic() - last_progress_at[0]) >= max(60, int(liveness_timeout_s))
                ),
                base_timeout=max(10, min(300, int(request_timeout))), event_fn=candidate_event, conversation=conversation,
                require_mutation_or_explicit_no_change=not has_existing_delta,
                require_test_verification=True, allow_completion_signal=True,
                native_openrouter_tool_calling=bool(native_openrouter_tool_calling),
            )
            run = runtime.run()
            if (
                (time.monotonic() - last_progress_at[0]) >= max(60, int(liveness_timeout_s))
                and not (stop_requested and stop_requested())
                and run.status != "completed"
            ):
                reason = f"candidate liveness timeout after {int(liveness_timeout_s)}s without progress"
                run.status = "paused"; run.response = reason; run.error = ""
                run.failure_category = "transient"
                _emit(event_fn, "team_worker_event", role=worker_role, event="runtime_status", category="liveness",
                      status="paused", phase="candidate_timeout", message=reason)
            if run.status == "completed":
                probe = CandidateResult(
                    slot=slot, model=model, strategy=strategy, workspace=backend, run=run,
                )
                verification = evaluate_candidate(probe)
                cached_verification = verification
                if bool(verification.get("verification_passed")):
                    break
                if verification_repairs >= _TEAM_CANDIDATE_MAX_VERIFICATION_REPAIRS:
                    _emit(
                        event_fn, "team_worker_event", role=worker_role, event="runtime_status",
                        category="verification", status="failed", phase="candidate_verification",
                        message="candidate exhausted automatic deterministic verification repairs",
                    )
                    break
                verification_repairs += 1
                conversation = []
                prompt = _candidate_verification_repair_prompt(verification, verification_repairs)
                _emit(
                    event_fn, "team_worker_event", role=worker_role, event="runtime_status",
                    category="verification", status="repairing", phase="candidate_verification",
                    message=(
                        f"automatic candidate verification repair {verification_repairs}/"
                        f"{_TEAM_CANDIDATE_MAX_VERIFICATION_REPAIRS}"
                    ),
                )
                continue
            if not _candidate_pause_is_resumable(run, stop_requested):
                break

            if not _wait_before_resume(
                run, auto_resumes + 1, event_fn=event_fn, role=worker_role,
                phase="candidate_resume", stop_requested=stop_requested,
            ):
                break

            # Once a coder has produced workspace changes, the filesystem and deterministic
            # checks are more trustworthy than another long free-form continuation. Evaluate
            # immediately: accept objectively good paused work, otherwise launch a clean,
            # failure-focused repair chat with no inherited assistant conversation.
            paused_delta = backend.delta_summary()
            has_paused_delta = bool(
                int(paused_delta.get("changed_count") or 0)
                or int(paused_delta.get("deleted_count") or 0)
            )
            if has_paused_delta:
                probe = CandidateResult(
                    slot=slot, model=model, strategy=strategy, workspace=backend, run=run,
                )
                verification = evaluate_candidate(probe)
                cached_verification = verification
                if bool(verification.get("verification_passed")):
                    _emit(
                        event_fn, "team_worker_event", role=worker_role, event="runtime_status",
                        category="verification", status="accepted", phase="candidate_verification",
                        message="paused candidate accepted from deterministic workspace verification",
                    )
                    break
                if verification_repairs >= _TEAM_CANDIDATE_MAX_VERIFICATION_REPAIRS:
                    break
                verification_repairs += 1
                conversation = []
                prompt = _candidate_verification_repair_prompt(verification, verification_repairs)
                _emit(
                    event_fn, "team_worker_event", role=worker_role, event="runtime_status",
                    category="verification", status="repairing", phase="candidate_verification",
                    message=(
                        "paused candidate has workspace changes; starting fresh deterministic verification repair "
                        f"{verification_repairs}/{_TEAM_CANDIDATE_MAX_VERIFICATION_REPAIRS}"
                    ),
                )
                continue

            provider_retry = _provider_pause_is_retryable(run)
            if not provider_retry and auto_resumes >= _TEAM_CANDIDATE_MAX_AUTO_RESUMES:
                break
            auto_resumes += 1
            reason = str(run.response or run.error or "")
            delta = paused_delta
            if _is_incomplete_envelope_reason(reason):
                prompt = _fresh_worker_recovery_prompt(run, reason, auto_resumes, label=worker_role)
                conversation = []
                _emit(event_fn, "team_worker_event", role=worker_role, event="runtime_status",
                      category="recovery", status="fresh_chat", phase="candidate_resume",
                      message=f"starting fresh provider chat after incomplete response envelope (retry {auto_resumes}, unlimited provider recovery)")
            else:
                conversation = _candidate_conversation(run)
                prompt = _candidate_resume_prompt(run, delta, auto_resumes)

        assert run is not None
        if hasattr(run, "performance") and isinstance(run.performance, dict):
            run.performance.setdefault("team_auto_resumes", auto_resumes)
            run.performance.setdefault("team_verification_repairs", verification_repairs)
        return CandidateResult(
            slot=slot, model=model, strategy=strategy, workspace=backend, run=run,
            evaluation=cached_verification,
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
    run_eligible = candidate.run.status in {"completed", "paused"}
    has_delta = bool(delta.get("changed_count", 0) or delta.get("deleted_count", 0))
    if run_eligible and has_delta:
        score = 40 + passed * 25 - failed * 60 + 10
        if candidate.run.error:
            score -= 20
    else:
        # Failed runs and unchanged workspaces are never candidates. A paused run
        # with real changes may still be objectively complete; deterministic gates
        # below decide that rather than requiring a cosmetic DONE envelope.
        score = 0
    diff = candidate.workspace.delta_diff() if isinstance(candidate.workspace, RamWorkspace) else _git_diff(root)
    coverage = test_change_evidence(delta)
    deterministic_ok = verification_passed(results)
    coverage_ok = bool(coverage.get("coverage_evidence_ok"))
    if run_eligible and has_delta and not coverage_ok:
        score -= 120
        checks["test-change-evidence"] = {
            "name": "test-change-evidence", "ok": False, "required": True,
            "output": "behavior-changing source code requires a changed or newly created regression test",
            **coverage,
        }
    return {
        "score": score, "delta": delta, "checks": checks, "diff": diff,
        "test_evidence": coverage, "candidate_id": blind_candidate_id(diff),
        "verification_passed": run_eligible and has_delta and deterministic_ok and coverage_ok,
    }


def _candidate_is_mergeable(candidate: CandidateResult) -> bool:
    return candidate.run.status in {"completed", "paused"} and bool(candidate.evaluation.get("verification_passed"))


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
            event_fn=events, stop_requested=stop_requested, run_id=run_id,
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
    run_id: str = "",
) -> TeamRunResult:
    errors = config.validate()
    if errors:
        return TeamRunResult("failed", "", "", [], [], {}, "; ".join(errors))
    task_contract = compile_task_contract(task)
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
    stageoff: dict[str, Any] = {
        "schema": "aicoder-stageoff-v1",
        "user_task": task,
        "task_contract": task_contract.as_dict(),
        "repository_context": _repository_context(source_workspace),
        "current_stage": "run_start",
        "handoff_id": "",
        "stages": [],
        "latest_coordinator_review": "",
    }
    stageoff_handoff = make_handoff(
        "stageoff", json.dumps(stageoff, ensure_ascii=False, indent=2), max_chars=120000, source_stage="run_start"
    )
    stageoff_file = Path("/tmp") / f"aicoder-stageoff-{run_id or uuid.uuid4().hex[:16]}.json"
    atomic_write_text(stageoff_file, json.dumps(stageoff, ensure_ascii=False, indent=2) + "\n")
    try:
        os.chmod(stageoff_file, 0o600)
    except OSError:
        pass
    all_tools = load_tools(client)
    # Every team worker sees the same authenticated runtime tool catalogue.
    # Role prompts and execution-risk policy control HOW tools are used; we do not
    # hide capabilities per role because that causes inconsistent model behavior.
    research_tools = [dict(tool) for tool in all_tools]
    coder_tools = [dict(tool) for tool in all_tools]
    _emit(event_fn, "team_start", agents=config.active_count, research=len(config.research), coders=len(config.coders))

    # 1) plan_research -- coordinator bootstraps cumulative Session Memory / StageOff and research assignments.
    _stage_start(ledger, TeamStage.PLAN_RESEARCH, event_fn)
    research_planner_model = config.coordinator_model or config.planner_model or ""
    deterministic_greenfield_bootstrap = bool(
        str(task or "").strip()
        and not _task_requires_external_research(task)
        and not _workspace_has_meaningful_project_files(source_workspace)
    )
    if deterministic_greenfield_bootstrap:
        bootstrap_response = _deterministic_greenfield_bootstrap_plan(task)
        research_plan = AgentStageResult(
            role="coordinator:plan_research", model="deterministic", status="completed",
            response=bootstrap_response, elapsed_ms=0,
            evidence={"deterministic_greenfield": True, "model_skipped": True},
        )
        _emit(
            event_fn, "team_worker_event", role="coordinator:plan_research", event="runtime_status",
            category="research", status="deterministic", phase="greenfield_bootstrap",
            message="self-contained greenfield task detected; bootstrapping StageOff deterministically from the immutable user task",
        )
    else:
        research_plan = _call_stage_agent(
            client=client, model_client=model_client, model=research_planner_model,
            system=RESEARCH_PLANNER_SYSTEM_PROMPT, tools=all_tools, workspace_root=source_workspace,
            prompt=(
                "BOOTSTRAP SESSION MEMORY / STAGEOFF FROM THIS RUN.\n\n"
                f"USER TASK:\n{_task_handoff(task).render()}\n\n"
                f"REPOSITORY CONTEXT:\n{make_handoff('repository-context', _repository_context(source_workspace), max_chars=5000).render()}\n\n"
                "Create a task-specific research plan for all four researcher roles. Inspect the actual project with tools where useful. "
                "The resulting Session Memory becomes the authoritative cumulative working state for the next stage."
            ),
            required_sections=_BOOTSTRAP_SECTIONS, max_tokens=2200, max_iterations=30,
            event_fn=event_fn, role="coordinator:plan_research", stop_requested=stop_requested,
            approval_fn=_approval_with_task_backend_policy(_planning_approval, task_contract), request_timeout=request_timeout,
            native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
        )
    research_plan.role = "coordinator:plan_research"; stages.append(research_plan)
    if research_plan.status != "completed":
        return TeamRunResult("failed", "", research_plan.model, stages, [], {"ledger": ledger.as_dict(), "stageoff": stageoff}, research_plan.error)

    research_contract_handoff = _research_plan_handoff(research_plan.response)
    handoff_metrics.append(research_contract_handoff.metrics())
    handoff_archive[research_contract_handoff.handoff_id] = {
        "kind": research_contract_handoff.kind, "raw": research_contract_handoff.raw,
        "compact": research_contract_handoff.compact,
    }

    # The coordinator may replace/reorganize working memory at bootstrap; AICoder keeps immutable run identity separately.
    stageoff = {
        "schema": "aicoder-stageoff-v1",
        "user_task": task,
        "repository_context": _repository_context(source_workspace),
        "current_stage": TeamStage.PLAN_RESEARCH.value,
        "handoff_id": "",
        "session_memory": research_plan.response,
        "research_plan": research_plan.response,
        "latest_coordinator_review": research_plan.response,
        "working_memory": {
            "session_memory": _extract_contract_sections(research_plan.response, _BOOTSTRAP_SECTIONS).get("SESSION MEMORY", ""),
            "research_plan": _extract_contract_sections(research_plan.response, _BOOTSTRAP_SECTIONS).get("RESEARCH PLAN", ""),
            "evidence_gaps": _extract_contract_sections(research_plan.response, _BOOTSTRAP_SECTIONS).get("EVIDENCE GAPS", ""),
            "next_stage_instructions": _extract_contract_sections(research_plan.response, _BOOTSTRAP_SECTIONS).get("NEXT STAGE INSTRUCTIONS", ""),
        },
        "stages": [{
            "stage": TeamStage.PLAN_RESEARCH.value,
            "sequence": 1,
            "output": {"research_contract": research_plan.response},
            "coordinator_review": research_plan.response,
            "coordinator_status": "completed",
        }],
    }
    stageoff_handoff = make_handoff(
        "stageoff", json.dumps(stageoff, ensure_ascii=False, indent=2), max_chars=120000,
        source_stage=TeamStage.PLAN_RESEARCH.value,
    )
    stageoff["handoff_id"] = stageoff_handoff.handoff_id
    atomic_write_text(stageoff_file, json.dumps(stageoff, ensure_ascii=False, indent=2) + "\n")
    handoff_metrics.append(stageoff_handoff.metrics())
    handoff_archive[stageoff_handoff.handoff_id] = {
        "kind": stageoff_handoff.kind, "raw": stageoff_handoff.raw, "compact": stageoff_handoff.compact,
        "source_stage": stageoff_handoff.source_stage, "parent_handoff_id": stageoff_handoff.parent_handoff_id,
    }
    _emit(
        event_fn, "team_stageoff", stage=TeamStage.PLAN_RESEARCH.value, handoff_id=stageoff_handoff.handoff_id,
        parent_handoff_id="", entries=1, coordinator_status="completed", fresh_coordinator_process=True,
        stageoff_path=str(stageoff_file), stageoff=stageoff,
    )
    _emit_stage_handoff(event_fn, stageoff_handoff, next_stage=TeamStage.RESEARCH)
    _stage_complete(ledger, TeamStage.PLAN_RESEARCH, event_fn)

    # 2) research
    _stage_start(ledger, TeamStage.RESEARCH, event_fn)
    research_results: list[AgentStageResult] = []
    if config.research:
        with ThreadPoolExecutor(max_workers=len(config.research), thread_name_prefix="aicoder-research") as pool:
            futures = {
                pool.submit(
                    _run_researcher, client=client, model_client=model_client, model=slot.model,
                    role=slot.role, source_workspace=source_workspace,
                    tools=research_tools, stop_requested=stop_requested,
                    stage_input=stageoff_handoff, task=task, research_plan=research_plan.response,
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
    research_stage_payload = {
        "user_task": task,
        "repository_context": _repository_context(source_workspace),
        "research_contract": research_plan.response,
        "reports": [],
    }
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
        research_stage_payload["reports"].append({
            "role": item.role, "status": item.status, "report": report_handoff.compact,
            "evidence": item.evidence, "error": item.error,
        })
    stageoff, coordinator_stage, stageoff_handoff = _coordinate_stageoff(
        current=stageoff, stage=TeamStage.RESEARCH, stage_payload=research_stage_payload,
        client=client, model_client=model_client, coordinator_model=config.coordinator_model,
        tools=all_tools, workspace_root=source_workspace,
        event_fn=event_fn, stop_requested=stop_requested, request_timeout=request_timeout,
        native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
        stageoff_path=stageoff_file,
    )
    if coordinator_stage is not None:
        coordinator_stage.role = "coordinator:research"; stages.append(coordinator_stage)
    handoff_metrics.append(stageoff_handoff.metrics())
    handoff_archive[stageoff_handoff.handoff_id] = {
        "kind": stageoff_handoff.kind, "raw": stageoff_handoff.raw, "compact": stageoff_handoff.compact,
        "source_stage": stageoff_handoff.source_stage, "parent_handoff_id": stageoff_handoff.parent_handoff_id,
    }
    _emit_stage_handoff(event_fn, stageoff_handoff, next_stage=TeamStage.BRAINSTORM)
    _stage_complete(ledger, TeamStage.RESEARCH, event_fn)

    # 3) brainstorm -- divergent multi-model reasoning after research, before implementation planning.
    _stage_start(ledger, TeamStage.BRAINSTORM, event_fn)
    brainstorm_results: list[AgentStageResult] = []
    brainstorm_state = ""
    brainstorm_participants = _brainstorm_participants(config)
    configured_rounds = _brainstorm_rounds(state)
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
                    _call_stage_agent, client=client, model_client=model_client, model=model, system=system_prompt,
                    tools=all_tools, workspace_root=source_workspace,
                    prompt=(
                        "PREVIOUS STAGE OUTPUT (authoritative; fresh model process):\n"
                        + stageoff_handoff.render()
                        + f"\n\nBRAINSTORM ROUND: {round_index}\nYOUR PERSPECTIVE: {perspective}\n\n"
                        + f"CURRENT ANONYMIZED BRAINSTORM STATE:\n{brainstorm_state or '(none - create independent ideas)'}"
                    ),
                    required_sections=BRAINSTORM_SECTIONS, max_tokens=4000, max_iterations=35,
                    event_fn=event_fn, role=f"brainstorm:r{round_index}:{label}", stop_requested=stop_requested,
                    approval_fn=_research_approval_for_task(task_contract), request_timeout=request_timeout,
                    native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
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
        operator = _call_stage_agent(
            client=client, model_client=model_client, model=synthesis_model, system=BRAINSTORM_OPERATOR_SYSTEM_PROMPT,
            tools=[], workspace_root=source_workspace,
            prompt=_build_brainstorm_operator_prompt(task, round_index, usable, brainstorm_state),
            required_sections=BRAINSTORM_SECTIONS, max_tokens=5000, max_iterations=30,
            event_fn=event_fn, role=f"brainstorm_state:r{round_index}", stop_requested=stop_requested,
            approval_fn=_research_approval_for_task(task_contract), request_timeout=request_timeout,
            native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
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
        brainstorm_synthesis = _call_stage_agent(
            client=client, model_client=model_client, model=synthesis_model, system=BRAINSTORM_SYNTHESIS_SYSTEM_PROMPT,
            tools=[], workspace_root=source_workspace,
            prompt=_build_brainstorm_synthesis_prompt(task, brainstorm_state, brainstorm_results),
            required_sections=BRAINSTORM_SECTIONS, max_tokens=6000, max_iterations=30,
            event_fn=event_fn, role="brainstorm_synthesis", stop_requested=stop_requested,
            approval_fn=_research_approval_for_task(task_contract), request_timeout=request_timeout,
            native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
        )
        brainstorm_synthesis.role = "brainstorm_synthesis"
        stages.append(brainstorm_synthesis)
        synthesis_error = str(brainstorm_synthesis.error or brainstorm_synthesis.response or "")
        if (
            brainstorm_synthesis.status != "completed"
            and "safety pause after an unusually long run" in synthesis_error.lower()
            and not (stop_requested and stop_requested())
        ):
            _emit(
                event_fn, "team_worker_event", role="brainstorm_synthesis", event="runtime_status",
                category="brainstorm", status="retrying", phase="synthesis",
                message="brainstorm synthesis hit the bounded safety pause; retrying once from compact authoritative evidence",
            )
            retry_prompt = (
                "AUTONOMOUS SYNTHESIS RETRY. Do not inspect broadly and do not repeat completed research. "
                "Use only the compact authoritative evidence below and immediately produce the required brainstorm contract.\n\n"
                + _build_brainstorm_synthesis_prompt(task, brainstorm_state, brainstorm_results)
            )
            retried = _call_stage_agent(
                client=client, model_client=model_client, model=synthesis_model,
                system=BRAINSTORM_SYNTHESIS_SYSTEM_PROMPT, tools=[],
                workspace_root=source_workspace, prompt=retry_prompt,
                required_sections=BRAINSTORM_SECTIONS, max_tokens=3500, max_iterations=12,
                event_fn=event_fn, role="brainstorm_synthesis:retry", stop_requested=stop_requested,
                approval_fn=_research_approval_for_task(task_contract), request_timeout=request_timeout,
                native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
            )
            retried.role = "brainstorm_synthesis:retry"
            stages.append(retried)
            if retried.status == "completed":
                brainstorm_synthesis = retried
        if brainstorm_synthesis.status != "completed":
            _emit(event_fn, "team_worker_event", role="brainstorm_synthesis", event="runtime_status",
                  category="brainstorm", status="warning", phase="synthesis",
                  message=f"brainstorm synthesis unavailable; planner continues from research evidence: {str(brainstorm_synthesis.error or brainstorm_synthesis.response)[:500]}")
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
    stageoff, coordinator_stage, stageoff_handoff = _coordinate_stageoff(
        current=stageoff, stage=TeamStage.BRAINSTORM,
        stage_payload={
            "brainstorm_synthesis": brainstorm_contract_handoff.compact,
            "brainstorm_state": brainstorm_state,
        },
        client=client, model_client=model_client, coordinator_model=config.coordinator_model,
        tools=all_tools, workspace_root=source_workspace,
        event_fn=event_fn, stop_requested=stop_requested, request_timeout=request_timeout,
        native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
        stageoff_path=stageoff_file,
    )
    if coordinator_stage is not None:
        coordinator_stage.role = "coordinator:brainstorm"; stages.append(coordinator_stage)
    handoff_metrics.append(stageoff_handoff.metrics())
    handoff_archive[stageoff_handoff.handoff_id] = {
        "kind": stageoff_handoff.kind, "raw": stageoff_handoff.raw, "compact": stageoff_handoff.compact,
        "source_stage": stageoff_handoff.source_stage, "parent_handoff_id": stageoff_handoff.parent_handoff_id,
    }
    _emit_stage_handoff(event_fn, stageoff_handoff, next_stage=TeamStage.PLAN_CODE)
    _stage_complete(ledger, TeamStage.BRAINSTORM, event_fn)

    # 4) plan_code
    _stage_start(ledger, TeamStage.PLAN_CODE, event_fn)
    code_plan = _call_stage_agent(
        client=client, model_client=model_client, model=config.planner_model or "", system=PLANNER_SYSTEM_PROMPT,
        tools=all_tools, workspace_root=source_workspace,
        prompt=(
            "PREVIOUS STAGE OUTPUT (authoritative; fresh model process):\n"
            + stageoff_handoff.render()
            + "\n\nCreate the implementation contract from this handoff only. Inspect the actual repository with tools where needed. "
              "Do not assume prior-stage conversation."
        ),
        required_sections=CODE_PLAN_SECTIONS, max_tokens=6500, max_iterations=50,
        event_fn=event_fn, role="plan_code", stop_requested=stop_requested, approval_fn=_approval_with_task_backend_policy(_planning_approval, task_contract),
        request_timeout=request_timeout, native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
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

    stageoff, coordinator_stage, stageoff_handoff = _coordinate_stageoff(
        current=stageoff, stage=TeamStage.PLAN_CODE,
        stage_payload={
            "implementation_contract": code_plan.response,
            "model": code_plan.model, "status": code_plan.status,
        },
        client=client, model_client=model_client, coordinator_model=config.coordinator_model,
        tools=all_tools, workspace_root=source_workspace,
        event_fn=event_fn, stop_requested=stop_requested, request_timeout=request_timeout,
        native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
        stageoff_path=stageoff_file,
    )
    if coordinator_stage is not None:
        coordinator_stage.role = "coordinator:plan_code"; stages.append(coordinator_stage)
    handoff_metrics.append(stageoff_handoff.metrics())
    handoff_archive[stageoff_handoff.handoff_id] = {
        "kind": stageoff_handoff.kind, "raw": stageoff_handoff.raw, "compact": stageoff_handoff.compact,
        "source_stage": stageoff_handoff.source_stage, "parent_handoff_id": stageoff_handoff.parent_handoff_id,
    }
    _emit_stage_handoff(event_fn, stageoff_handoff, next_stage=TeamStage.CODE)

    candidate_handoffs = {
        "stageoff": stageoff,
        "stageoff_handoff": stageoff_handoff.render(),
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
                    strategy=slot.strategy, stage_input=stageoff_handoff,
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
                    if not candidate.evaluation:
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
            details = "; ".join(_candidate_rejection_reason(candidate) for candidate in candidates[:8])
            error = "no verified coding candidate completed" + (f": {details}" if details else "")
            return TeamRunResult("failed", "", "", stages, candidates, {"ledger": ledger.as_dict()}, error)
        winner = max(viable, key=lambda item: objective_rank_key(item.evaluation))
        code_stage_payload = {
            "winner_candidate_id": str(winner.evaluation.get("candidate_id")),
            "winner_score": winner.score,
            "candidates": [
                {
                    "candidate_id": str(item.evaluation.get("candidate_id")),
                    "status": item.run.status, "score": item.score,
                    "evaluation": item.evaluation,
                }
                for item in candidates
            ],
        }
        stageoff, coordinator_stage, stageoff_handoff = _coordinate_stageoff(
            current=stageoff, stage=TeamStage.CODE, stage_payload=code_stage_payload,
            client=client, model_client=model_client, coordinator_model=config.coordinator_model,
            tools=all_tools, workspace_root=source_workspace,
            event_fn=event_fn, stop_requested=stop_requested, request_timeout=request_timeout,
            native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
            stageoff_path=stageoff_file,
        )
        if coordinator_stage is not None:
            coordinator_stage.role = "coordinator:code"; stages.append(coordinator_stage)
        handoff_metrics.append(stageoff_handoff.metrics())
        _emit_stage_handoff(event_fn, stageoff_handoff, next_stage=TeamStage.MERGE_PLAN)
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
        integration.write_candidate_artifact(
            ".aicoder-team/stageoff.json", json.dumps(stageoff, ensure_ascii=False, indent=2)
        )
        winner_id = str(winner.evaluation.get("candidate_id"))

        # 5) merge_plan — blind to model/provider/slot identity.
        _stage_start(ledger, TeamStage.MERGE_PLAN, event_fn)
        merge_planner_model = config.coordinator_model or config.planner_model or ""
        merge_plan = _call_stage_agent(
            client=client, model_client=model_client, model=merge_planner_model, system=MERGE_PLANNER_SYSTEM_PROMPT,
            tools=all_tools, workspace_root=str(integration.info.execution_root),
            prompt=(
                "PREVIOUS STAGEOFF (authoritative; fresh model process):\n" + stageoff_handoff.render()
                + "\n\nANONYMIZED CANDIDATE EVIDENCE:\n"
                + make_handoff("candidate-evidence", json.dumps(_compact_candidate_evidence(blind_evidence), ensure_ascii=False, indent=2), max_chars=30000).render()
                + f"\n\nDETERMINISTIC BASE CANDIDATE: {winner_id}"
            ),
            required_sections=MERGE_PLAN_SECTIONS, max_tokens=4000, max_iterations=40,
            event_fn=event_fn, role="merge_plan", stop_requested=stop_requested, approval_fn=_approval_with_task_backend_policy(_planning_approval, task_contract),
            request_timeout=request_timeout, native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
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
        stageoff, coordinator_stage, stageoff_handoff = _coordinate_stageoff(
            current=stageoff, stage=TeamStage.MERGE_PLAN,
            stage_payload={"merge_contract": merge_plan.response, "base_candidate": winner_id},
            client=client, model_client=model_client, coordinator_model=config.coordinator_model,
            tools=all_tools, workspace_root=source_workspace,
            event_fn=event_fn, stop_requested=stop_requested, request_timeout=request_timeout,
            native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
            stageoff_path=stageoff_file,
        )
        if coordinator_stage is not None:
            coordinator_stage.role = "coordinator:merge_plan"; stages.append(coordinator_stage)
        handoff_metrics.append(stageoff_handoff.metrics())
        _emit_stage_handoff(event_fn, stageoff_handoff, next_stage=TeamStage.MERGE)
        integration.write_candidate_artifact(".aicoder-team/merge-plan.txt", merge_plan.response)
        integration.write_candidate_artifact(".aicoder-team/stageoff.json", json.dumps(stageoff, ensure_ascii=False, indent=2))
        integration.write_candidate_artifact(
            ".aicoder-team/handoffs.json",
            json.dumps(handoff_archive, ensure_ascii=False, indent=2),
        )
        _stage_complete(ledger, TeamStage.MERGE_PLAN, event_fn)

        # 6) merge — ensemble integration is always attempted for verified candidates.
        # A dedicated merge model remains optional; when omitted, reuse the coordinator,
        # planner, or verified base model instead of silently degrading to winner-only.
        _stage_start(ledger, TeamStage.MERGE, event_fn)
        merge_model = (
            config.merge_model
            or config.coordinator_model
            or config.planner_model
            or winner.run.model
            or winner.model
        )
        if merge_model:
            merge_prompt = (
                "PREVIOUS STAGEOFF (authoritative; fresh model process):\n" + stageoff_handoff.render()
                + "\n\nCandidate snapshots are under .aicoder-team/candidates/. "
                "Integrate only evidence-backed improvements required by the cumulative StageOff. "
                "Do not assume any prior model conversation."
            )
            merge_system = build_system_prompt(coder_tools, str(integration.info.execution_root)).rstrip()+"\n\n"+MERGE_SYSTEM_PROMPT+"\n\n"+task_contract.prompt_projection()
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
                    approval_fn=_approval_with_task_backend_policy(_candidate_approval, task_contract), max_iterations=14, max_output_tokens=10000, stop_requested=stop_requested,
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
                provider_retry = _provider_pause_is_retryable(merge_run)
                if not provider_retry and merge_auto_resumes >= _TEAM_MERGE_MAX_AUTO_RESUMES:
                    break
                if not _wait_before_resume(
                    merge_run, merge_auto_resumes + 1, event_fn=event_fn, role="merge",
                    phase="merge_resume", stop_requested=stop_requested,
                ):
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
                          message=f"starting fresh provider chat after incomplete response envelope (retry {merge_auto_resumes}, unlimited provider recovery)")
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
            # Defensive fallback for malformed configurations with no usable model at all.
            stages.append(AgentStageResult(
                "merge", "deterministic", "completed",
                f"Selected {winner_id}; ensemble merge unavailable because no merge-capable model resolved", 0,
            ))
            final_response = f"Selected verified base candidate {winner_id}; no merge-capable model resolved."
            result_model = winner.run.model
        stageoff, coordinator_stage, stageoff_handoff = _coordinate_stageoff(
            current=stageoff, stage=TeamStage.MERGE,
            stage_payload={
                "merge_status": "completed", "merge_response": final_response,
                "result_model": result_model, "workspace_delta": integration.delta_summary(),
            },
            client=client, model_client=model_client, coordinator_model=config.coordinator_model,
            tools=all_tools, workspace_root=source_workspace,
            event_fn=event_fn, stop_requested=stop_requested, request_timeout=request_timeout,
            native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
            stageoff_path=stageoff_file,
        )
        if coordinator_stage is not None:
            coordinator_stage.role = "coordinator:merge"; stages.append(coordinator_stage)
        handoff_metrics.append(stageoff_handoff.metrics())
        integration.write_candidate_artifact(".aicoder-team/stageoff.json", json.dumps(stageoff, ensure_ascii=False, indent=2))
        _emit_stage_handoff(event_fn, stageoff_handoff, next_stage=TeamStage.PLAN_TESTS)
        _stage_complete(ledger, TeamStage.MERGE, event_fn)

        # 7) plan_tests — model may explain/extend intent, deterministic commands remain authoritative.
        _stage_start(ledger, TeamStage.PLAN_TESTS, event_fn)
        deterministic_plan = merge_verification_plans(
            project_verification_plan(integration.info.execution_root),
            task_acceptance_verification_plan(task_contract, integration.info.execution_root),
        )
        test_plan_text = json.dumps([
            {"name": item.name, "argv": list(item.argv), "timeout": item.timeout, "required": item.required}
            for item in deterministic_plan
        ], ensure_ascii=False, indent=2)
        if config.test_planner_model:
            test_plan = _call_stage_agent(
                client=client, model_client=model_client, model=config.test_planner_model, system=TEST_PLANNER_SYSTEM_PROMPT,
                tools=all_tools, workspace_root=str(integration.info.execution_root),
                prompt=(
                    "PREVIOUS STAGEOFF (authoritative; fresh model process):\n" + stageoff_handoff.render()
                    + "\n\nDETERMINISTIC REPOSITORY CHECKS (authoritative):\n"
                    + make_handoff("deterministic-checks", test_plan_text, max_chars=6000).render()
                    + "\n\nPlan verification from this cumulative state only; inspect repository state with tools when needed. "
                      "Do not assume prior conversation."
                ),
                required_sections=_TEST_PLAN_SECTIONS, max_tokens=3000, max_iterations=35,
                event_fn=event_fn, role="plan_tests", stop_requested=stop_requested, approval_fn=_approval_with_task_backend_policy(_planning_approval, task_contract),
                request_timeout=request_timeout, native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
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
        stageoff, coordinator_stage, stageoff_handoff = _coordinate_stageoff(
            current=stageoff, stage=TeamStage.PLAN_TESTS,
            stage_payload={
                "deterministic_plan": json.loads(test_plan_text),
                "model_plan": (test_plan.response if config.test_planner_model and test_plan.status == "completed" else ""),
            },
            client=client, model_client=model_client, coordinator_model=config.coordinator_model,
            tools=all_tools, workspace_root=source_workspace,
            event_fn=event_fn, stop_requested=stop_requested, request_timeout=request_timeout,
            native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
            stageoff_path=stageoff_file,
        )
        if coordinator_stage is not None:
            coordinator_stage.role = "coordinator:plan_tests"; stages.append(coordinator_stage)
        handoff_metrics.append(stageoff_handoff.metrics())
        integration.write_candidate_artifact(".aicoder-team/stageoff.json", json.dumps(stageoff, ensure_ascii=False, indent=2))
        _emit_stage_handoff(event_fn, stageoff_handoff, next_stage=TeamStage.TESTS_FUNCTION_OK)
        _stage_complete(ledger, TeamStage.PLAN_TESTS, event_fn)

        # 8) tests_function_ok — only executable evidence can open the disk-write gate.
        _stage_start(ledger, TeamStage.TESTS_FUNCTION_OK, event_fn)
        verification_results = execute_verification_plan(integration.info.execution_root, deterministic_plan)
        verification_payload = [item.as_dict() for item in verification_results]
        integration.write_candidate_artifact(".aicoder-team/final-verification.json", json.dumps(verification_payload, ensure_ascii=False, indent=2))
        if not verification_passed(verification_results):
            return TeamRunResult(
                "failed", "", result_model, stages, candidates,
                {"ledger": ledger.as_dict(), "verification": verification_payload, "stageoff": stageoff},
                "tests_function_ok gate failed; persistent workspace was not modified",
            )
        stageoff, coordinator_stage, stageoff_handoff = _coordinate_stageoff(
            current=stageoff, stage=TeamStage.TESTS_FUNCTION_OK, stage_payload={"verification": verification_payload},
            client=client, model_client=model_client, coordinator_model=config.coordinator_model,
            tools=all_tools, workspace_root=source_workspace,
            event_fn=event_fn, stop_requested=stop_requested, request_timeout=request_timeout,
            native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
            stageoff_path=stageoff_file,
        )
        if coordinator_stage is not None:
            coordinator_stage.role = "coordinator:tests_function_ok"; stages.append(coordinator_stage)
        handoff_metrics.append(stageoff_handoff.metrics())
        integration.write_candidate_artifact(".aicoder-team/stageoff.json", json.dumps(stageoff, ensure_ascii=False, indent=2))
        _emit_stage_handoff(event_fn, stageoff_handoff, next_stage=TeamStage.ATOMIC_DISK_WRITE)
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
        stageoff, coordinator_stage, stageoff_handoff = _coordinate_stageoff(
            current=stageoff, stage=TeamStage.ATOMIC_DISK_WRITE,
            stage_payload={"change_manifest": change_manifest, "verification_passed": True},
            client=client, model_client=model_client, coordinator_model=config.coordinator_model,
            tools=all_tools, workspace_root=source_workspace,
            event_fn=event_fn, stop_requested=stop_requested, request_timeout=request_timeout,
            native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
            stageoff_path=stageoff_file,
        )
        if coordinator_stage is not None:
            coordinator_stage.role = "coordinator:atomic_disk_write"; stages.append(coordinator_stage)
        handoff_metrics.append(stageoff_handoff.metrics())
        integration.write_candidate_artifact(".aicoder-team/stageoff.json", json.dumps(stageoff, ensure_ascii=False, indent=2))
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
            "change_manifest": change_manifest, "stageoff": stageoff,
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
