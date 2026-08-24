"""Experimental RAM-backed multi-agent orchestration for AICoder.

The orchestrator deliberately reuses NativeLightRuntime for every tool-capable
worker so tool calling, approvals, recovery, telemetry and workspace protection
stay identical to normal AICoder runs.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable

from .agent_runtime import AgentRunResult, NativeLightRuntime
from .executor import build_system_prompt, load_tools
from .model_transport import ModelTransport
from .performance import RuntimePerformance
from .team_runtime import (
    CODER_SYSTEM_TEMPLATE, COORDINATOR_SYSTEM_PROMPT, FINALIZER_SYSTEM_PROMPT,
    MERGE_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT, RESEARCH_INSTRUCTIONS,
    RESEARCH_OUTPUT_CONTRACT, TeamConfig,
)
from .workspace_backend import RamWorkspace, WorkspaceBackend, WorkspaceError, create_workspace_backend

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


def _call_advisor(
    model_client: ModelTransport,
    *,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int = 6000,
) -> AgentStageResult:
    started = time.monotonic()
    try:
        result = model_client.chat(
            message=prompt, model=model, system_prompt=system, temperature=0.2,
            max_tokens=max_tokens, fallback_model=None, tools=None, tool_choice="none",
        )
        response = str(result.get("response") or "").strip() if isinstance(result, dict) else ""
        if not response:
            return AgentStageResult("advisor", model, "failed", "", int((time.monotonic()-started)*1000), "empty response")
        return AgentStageResult("advisor", str(result.get("model") or model), "completed", response, int((time.monotonic()-started)*1000))
    except Exception as exc:
        return AgentStageResult("advisor", model, "failed", "", int((time.monotonic()-started)*1000), f"{type(exc).__name__}: {exc}")


def _filtered_tools(catalogue: list[dict], names: frozenset[str]) -> list[dict]:
    return [dict(tool) for tool in catalogue if str(tool.get("name") or "") in names]


def _run_researcher(
    *, client, model_client: ModelTransport, model: str, role: str, task: str,
    source_workspace: str, tools: list[dict], stop_requested: StopFn | None,
) -> AgentStageResult:
    prompt = (
        f"User task:\n{task}\n\nRepository root for read-only inspection: {source_workspace}\n\n"
        + RESEARCH_INSTRUCTIONS[role] + "\n\n" + RESEARCH_OUTPUT_CONTRACT
    )
    system = build_system_prompt(tools, source_workspace).rstrip() + (
        "\n\n## RESEARCH AGENT ROLE\n" + RESEARCH_INSTRUCTIONS[role] + "\n\n" + RESEARCH_OUTPUT_CONTRACT
    )
    started = time.monotonic()
    runtime = NativeLightRuntime(
        client=client, model_client=model_client, initial_prompt=prompt,
        model=model, fallback_model=None, workspace_root=source_workspace,
        plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
        tools=tools, system_prompt=system, load_tools_on_start=True,
        quick_chat=False, persistent_plan=False, approval_fn=lambda *_: False,
        max_iterations=10, max_output_tokens=6000, stop_requested=stop_requested,
    )
    result = runtime.run()
    return AgentStageResult(
        role=f"research:{role}", model=result.model or model, status=result.status,
        response=result.response, elapsed_ms=int((time.monotonic()-started)*1000), error=result.error,
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


def _build_planner_prompt(task: str, repo_context: str, research: list[AgentStageResult]) -> str:
    reports = []
    for item in research:
        reports.append(f"### {item.role} · model={item.model} · status={item.status}\n{item.response or item.error}")
    return (
        f"ORIGINAL USER TASK:\n{task}\n\nREPOSITORY CONTEXT:\n{repo_context}\n\n"
        "INDEPENDENT RESEARCH REPORTS:\n" + "\n\n".join(reports)
    )


def _candidate_prompt(task: str, plan: str, coordinator: str, strategy: str) -> str:
    return (
        f"ORIGINAL USER TASK:\n{task}\n\nSHARED IMPLEMENTATION CONTRACT:\n{plan}\n\n"
        f"COORDINATION NOTES:\n{coordinator or '(none)'}\n\n"
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


def _run_candidate(
    *, client, model_client: ModelTransport, source_workspace: str, workspace_mode: str,
    slot: int, model: str, strategy: str, task: str, plan: str, coordinator: str,
    tools: list[dict], stop_requested: StopFn | None,
) -> CandidateResult:
    backend = create_workspace_backend(source_workspace, "ram" if workspace_mode != "disk" else "disk")
    backend.prepare()
    if not isinstance(backend, RamWorkspace):
        # Multiple candidates must never share the persistent workspace. If RAM is
        # unavailable, use an isolated temporary disk copy rather than direct DiskWorkspace.
        backend.abort()
        raise WorkspaceError("parallel candidate runtime requires an isolated RAM workspace")
    system = build_system_prompt(tools, str(backend.info.execution_root)).rstrip() + "\n\n" + CODER_SYSTEM_TEMPLATE.format(slot=slot, strategy=strategy)
    started = time.monotonic()
    runtime = NativeLightRuntime(
        client=client, model_client=model_client,
        initial_prompt=_candidate_prompt(task, plan, coordinator, strategy),
        model=model, fallback_model=None, workspace_root=str(backend.info.execution_root),
        plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
        tools=tools, system_prompt=system, load_tools_on_start=True,
        quick_chat=False, persistent_plan=False, approval_fn=_candidate_approval,
        max_iterations=18, max_output_tokens=12000, stop_requested=stop_requested,
    )
    run = runtime.run()
    return CandidateResult(
        slot=slot, model=model, strategy=strategy, workspace=backend, run=run,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


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
    checks: dict[str, Any] = {}
    # Portable, deterministic baseline checks. Project-specific tests still run
    # inside the coder runtime according to the shared plan.
    checks["compile"] = _run_check(root, ["python3", "-m", "compileall", "-q", "."], timeout=120)
    if (root / "tests").is_dir():
        checks["tests"] = _run_check(root, ["python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"], timeout=180)
    score = 0
    if candidate.run.status == "completed": score += 40
    if delta.get("changed_count", 0) or delta.get("deleted_count", 0): score += 10
    for check in checks.values():
        score += 25 if check.get("ok") else -35
    if candidate.run.error: score -= 20
    return {"score": score, "delta": delta, "checks": checks, "diff": _git_diff(root)}


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


def run_team(
    *, task: str, state: dict[str, Any], config: TeamConfig, client,
    model_client: ModelTransport, source_workspace: str,
    event_fn: EventFn | None = None, stop_requested: StopFn | None = None,
) -> TeamRunResult:
    errors = config.validate()
    if errors:
        return TeamRunResult("failed", "", "", [], [], {}, "; ".join(errors))
    started = time.monotonic()
    stages: list[AgentStageResult] = []
    candidates: list[CandidateResult] = []
    all_tools = load_tools(client)
    research_tools = _filtered_tools(all_tools, _RESEARCH_TOOL_NAMES)
    coder_tools = _filtered_tools(all_tools, _CODER_TOOL_NAMES)

    _emit(event_fn, "team_start", agents=config.active_count, research=len(config.research), coders=len(config.coders))

    # Research in parallel. Read-only against the authoritative source workspace.
    research_results: list[AgentStageResult] = []
    if config.research:
        with ThreadPoolExecutor(max_workers=len(config.research), thread_name_prefix="aicoder-research") as pool:
            futures = {
                pool.submit(
                    _run_researcher, client=client, model_client=model_client, model=slot.model,
                    role=slot.role, task=task, source_workspace=source_workspace,
                    tools=research_tools, stop_requested=stop_requested,
                ): slot for slot in config.research
            }
            for future in as_completed(futures):
                slot = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = AgentStageResult(f"research:{slot.role}", slot.model, "failed", "", 0, f"{type(exc).__name__}: {exc}")
                research_results.append(result); stages.append(result)
                _emit(event_fn, "team_stage", role=result.role, status=result.status, model=result.model, elapsed_ms=result.elapsed_ms)

    planner = _call_advisor(
        model_client, model=config.planner_model or "",
        system=PLANNER_SYSTEM_PROMPT,
        prompt=_build_planner_prompt(task, _repository_context(source_workspace), research_results),
        max_tokens=9000,
    )
    planner.role = "planner"; stages.append(planner)
    _emit(event_fn, "team_stage", role="planner", status=planner.status, model=planner.model, elapsed_ms=planner.elapsed_ms)
    if planner.status != "completed":
        return TeamRunResult("failed", "", planner.model, stages, [], {"wall_ms": int((time.monotonic()-started)*1000)}, planner.error)

    coordinator_text = ""
    if config.coordinator_model:
        coordinator = _call_advisor(
            model_client, model=config.coordinator_model, system=COORDINATOR_SYSTEM_PROMPT,
            prompt=f"USER TASK:\n{task}\n\nSHARED PLAN:\n{planner.response}", max_tokens=4000,
        )
        coordinator.role = "coordinator"; stages.append(coordinator)
        coordinator_text = coordinator.response if coordinator.status == "completed" else ""
        _emit(event_fn, "team_stage", role="coordinator", status=coordinator.status, model=coordinator.model, elapsed_ms=coordinator.elapsed_ms)

    # Candidate coders in parallel, each with its own RAM tree.
    with ThreadPoolExecutor(max_workers=len(config.coders), thread_name_prefix="aicoder-coder") as pool:
        futures = {
            pool.submit(
                _run_candidate, client=client, model_client=model_client, source_workspace=source_workspace,
                workspace_mode=str(state.get("workspace_mode") or "auto"), slot=slot.slot, model=slot.model,
                strategy=slot.strategy, task=task, plan=planner.response, coordinator=coordinator_text,
                tools=coder_tools, stop_requested=stop_requested,
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
                _emit(
                    event_fn, "team_candidate", slot=candidate.slot, status=candidate.run.status,
                    model=candidate.model, score=candidate.score, elapsed_ms=candidate.elapsed_ms,
                    evaluation_ms=candidate.evaluation_ms,
                )
            except Exception as exc:
                _emit(event_fn, "team_candidate", slot=slot.slot, status="failed", model=slot.model, score=-999, error=f"{type(exc).__name__}: {exc}")

    viable = [candidate for candidate in candidates if candidate.run.status == "completed"]
    if not viable:
        for c in candidates: c.workspace.abort()
        return TeamRunResult("failed", "", "", stages, candidates, {"wall_ms": int((time.monotonic()-started)*1000)}, "no coding candidate completed")
    winner = max(viable, key=lambda item: (item.score, -item.slot))

    # Integration workspace starts from the strongest measured candidate and gets
    # read-only snapshots of the alternatives for the merge model to inspect.
    integration = create_workspace_backend(source_workspace, "ram")
    integration.prepare()
    if not isinstance(integration, RamWorkspace):
        for c in candidates: c.workspace.abort()
        integration.abort()
        return TeamRunResult("failed", "", "", stages, candidates, {}, "integration requires RAM isolation")
    integration.seed_from(winner.workspace.info.execution_root)
    candidate_meta = []
    for c in candidates:
        candidate_meta.append({
            "slot": c.slot, "model": c.model, "strategy": c.strategy, "score": c.score,
            "evaluation": {k: v for k, v in c.evaluation.items() if k != "diff"},
            "diff": c.evaluation.get("diff", "")[:50000],
        })
    integration.write_candidate_artifact(
        ".aicoder-team/candidates.json", json.dumps(candidate_meta, ensure_ascii=False, indent=2)
    )
    merge_prompt = (
        f"USER TASK:\n{task}\n\nSHARED PLAN:\n{planner.response}\n\n"
        f"The integration workspace is seeded from candidate {winner.slot}, the highest deterministic score. "
        "Candidate evidence is in .aicoder-team/candidates.json. Inspect it and the current code. Integrate only "
        "changes that improve contract compliance and verification. Then run tests/checks."
    )
    merge_model = config.merge_model or winner.model
    merge_runtime = NativeLightRuntime(
        client=client, model_client=model_client, initial_prompt=merge_prompt,
        model=merge_model, fallback_model=None, workspace_root=str(integration.info.execution_root),
        plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
        tools=coder_tools, system_prompt=build_system_prompt(coder_tools, str(integration.info.execution_root)).rstrip()+"\n\n"+MERGE_SYSTEM_PROMPT,
        load_tools_on_start=True, quick_chat=False, persistent_plan=False,
        approval_fn=_candidate_approval, max_iterations=14, max_output_tokens=10000, stop_requested=stop_requested,
    )
    merge_started = time.monotonic()
    merge_run = merge_runtime.run()
    merge_elapsed = int((time.monotonic() - merge_started) * 1000)
    stages.append(AgentStageResult("merge", merge_run.model or merge_model, merge_run.status, merge_run.response, merge_elapsed, merge_run.error))
    _emit(event_fn, "team_stage", role="merge", status=merge_run.status, model=merge_run.model or merge_model, elapsed_ms=merge_elapsed)
    if merge_run.status != "completed":
        integration.abort(); [c.workspace.abort() for c in candidates]
        return TeamRunResult("failed", "", merge_run.model, stages, candidates, {}, merge_run.error or "merge failed")

    final_model = config.finalizer_model or merge_model
    final_runtime = NativeLightRuntime(
        client=client, model_client=model_client,
        initial_prompt=f"USER TASK:\n{task}\n\nSHARED PLAN:\n{planner.response}\n\nFinalize and fully verify the integrated candidate.",
        model=final_model, fallback_model=None, workspace_root=str(integration.info.execution_root),
        plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
        tools=coder_tools, system_prompt=build_system_prompt(coder_tools, str(integration.info.execution_root)).rstrip()+"\n\n"+FINALIZER_SYSTEM_PROMPT,
        load_tools_on_start=True, quick_chat=False, persistent_plan=False,
        approval_fn=_candidate_approval, max_iterations=10, max_output_tokens=8000, stop_requested=stop_requested,
    )
    final_started = time.monotonic()
    final_run = final_runtime.run()
    final_elapsed = int((time.monotonic() - final_started) * 1000)
    stages.append(AgentStageResult("finalizer", final_run.model or final_model, final_run.status, final_run.response, final_elapsed, final_run.error))
    _emit(event_fn, "team_stage", role="finalizer", status=final_run.status, model=final_run.model or final_model, elapsed_ms=final_elapsed)
    if final_run.status != "completed":
        integration.abort(); [c.workspace.abort() for c in candidates]
        return TeamRunResult("failed", "", final_run.model, stages, candidates, {}, final_run.error or "finalizer failed")

    final_eval_candidate = CandidateResult(0, final_model, "integrated", integration, final_run)
    final_eval_candidate.evaluation = evaluate_candidate(final_eval_candidate)
    final_eval_candidate.score = int(final_eval_candidate.evaluation.get("score") or 0)
    critical_checks = final_eval_candidate.evaluation.get("checks", {})
    if any(not bool(value.get("ok")) for value in critical_checks.values()):
        integration.abort(); [c.workspace.abort() for c in candidates]
        return TeamRunResult("failed", "", final_run.model, stages, candidates, {}, "integrated candidate failed deterministic verification")

    # Only this point may touch the persistent workspace.
    integration.finalize(verified=True)
    for c in candidates:
        c.workspace.abort()
    wall_ms = int((time.monotonic() - started) * 1000)
    accumulated_agent_ms = sum(stage.elapsed_ms for stage in stages) + sum(
        candidate.elapsed_ms + candidate.evaluation_ms for candidate in candidates
    )
    perf = {
        "wall_ms": wall_ms,
        "accumulated_agent_ms": accumulated_agent_ms,
        "parallelism": round(accumulated_agent_ms / wall_ms, 2) if wall_ms else 0.0,
        "research_agents": len(research_results), "coding_candidates": len(candidates),
        "winner_slot": winner.slot, "winner_score": winner.score,
        "final_score": final_eval_candidate.score,
        "stage_timings": [
            {"role": stage.role, "model": stage.model, "status": stage.status, "elapsed_ms": stage.elapsed_ms}
            for stage in stages
        ],
        "candidate_timings": [
            {
                "slot": candidate.slot, "model": candidate.model,
                "elapsed_ms": candidate.elapsed_ms, "evaluation_ms": candidate.evaluation_ms,
                "score": candidate.score,
            }
            for candidate in sorted(candidates, key=lambda item: item.slot)
        ],
    }
    _emit(event_fn, "team_complete", **perf)
    return TeamRunResult("completed", final_run.response, final_run.model or final_model, stages, candidates, perf)
