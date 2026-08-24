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

from .agent_runtime import AgentRunResult, NativeLightRuntime
from .executor import build_system_prompt, load_tools
from .model_transport import ModelTransport
from .performance import RuntimePerformance
from .team_runtime import (
    CODER_SYSTEM_TEMPLATE, MERGE_PLANNER_SYSTEM_PROMPT, MERGE_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT, RESEARCH_INSTRUCTIONS, RESEARCH_OUTPUT_CONTRACT,
    RESEARCH_PLANNER_SYSTEM_PROMPT, TEST_PLANNER_SYSTEM_PROMPT, TeamConfig,
)
from .team_pipeline import (
    StageLedger, TeamStage, blind_candidate_id, execute_verification_plan,
    objective_rank_key, project_verification_plan, verification_passed,
)
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
    research_plan: str = "",
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

    def research_event(kind: str, payload: dict[str, Any]) -> None:
        if kind in {"tool_call", "tool_result"}:
            row = {"kind": kind, **dict(payload)}
            evidence_events.append(row)

    runtime = NativeLightRuntime(
        client=client, model_client=model_client, initial_prompt=prompt,
        model=model, fallback_model=None, workspace_root=source_workspace,
        plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
        tools=tools, system_prompt=system, load_tools_on_start=True,
        quick_chat=False, persistent_plan=False, approval_fn=lambda *_: False,
        max_iterations=10, max_output_tokens=6000, stop_requested=stop_requested,
        event_fn=research_event,
    )
    result = runtime.run()
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
    *, client, model_client: ModelTransport, source_workspace: str, backend_mode: str,
    slot: int, model: str, strategy: str, task: str, plan: str, coordinator: str,
    tools: list[dict], stop_requested: StopFn | None,
) -> CandidateResult:
    backend = create_isolated_team_workspace(source_workspace, backend_mode)
    try:
        backend.prepare()
        if not isinstance(backend, RamWorkspace):
            raise WorkspaceError("parallel candidate runtime requires a transactional isolated workspace")
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
    diff = _git_diff(root)
    return {
        "score": score, "delta": delta, "checks": checks, "diff": diff,
        "candidate_id": blind_candidate_id(diff),
        "verification_passed": verification_passed(results),
    }


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
        evidence.append({
            "candidate_id": cid,
            "score": int(candidate.evaluation.get("score") or 0),
            "verification_passed": bool(candidate.evaluation.get("verification_passed")),
            "checks": candidate.evaluation.get("checks") or {},
            "delta": candidate.evaluation.get("delta") or {},
            "diff": str(candidate.evaluation.get("diff") or "")[:50000],
            "snapshot": f".aicoder-team/candidates/{cid}",
        })
    integration.write_candidate_artifact(
        ".aicoder-team/candidates.json", json.dumps(evidence, ensure_ascii=False, indent=2)
    )
    return evidence


def _blind_merge_prompt(task: str, code_plan: str, evidence: list[dict[str, Any]]) -> str:
    return (
        f"USER TASK:\n{task}\n\nSHARED CODE CONTRACT:\n{code_plan}\n\n"
        "ANONYMIZED CANDIDATE EVIDENCE:\n" + json.dumps(evidence, ensure_ascii=False, indent=2)
    )


def run_team(
    *, task: str, state: dict[str, Any], config: TeamConfig, client,
    model_client: ModelTransport, source_workspace: str,
    event_fn: EventFn | None = None, stop_requested: StopFn | None = None,
) -> TeamRunResult:
    errors = config.validate()
    if errors:
        return TeamRunResult("failed", "", "", [], [], {}, "; ".join(errors))

    started = time.monotonic()
    ledger = StageLedger()
    stages: list[AgentStageResult] = []
    candidates: list[CandidateResult] = []
    all_tools = load_tools(client)
    research_tools = _filtered_tools(all_tools, _RESEARCH_TOOL_NAMES)
    coder_tools = _filtered_tools(all_tools, _CODER_TOOL_NAMES)
    _emit(event_fn, "team_start", agents=config.active_count, research=len(config.research), coders=len(config.coders))

    # 1) plan_research
    _stage_start(ledger, TeamStage.PLAN_RESEARCH, event_fn)
    research_planner_model = config.coordinator_model or config.planner_model or ""
    research_plan = _call_advisor(
        model_client, model=research_planner_model, system=RESEARCH_PLANNER_SYSTEM_PROMPT,
        prompt=f"USER TASK:\n{task}\n\nREPOSITORY CONTEXT:\n{_repository_context(source_workspace)}",
        max_tokens=5000,
    )
    research_plan.role = "plan_research"; stages.append(research_plan)
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
                    research_plan=research_plan.response,
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
    _stage_complete(ledger, TeamStage.RESEARCH, event_fn)

    # 3) plan_code
    _stage_start(ledger, TeamStage.PLAN_CODE, event_fn)
    code_plan = _call_advisor(
        model_client, model=config.planner_model or "", system=PLANNER_SYSTEM_PROMPT,
        prompt=_build_planner_prompt(task, _repository_context(source_workspace), research_results)
        + "\n\nRESEARCH CONTRACT:\n" + research_plan.response,
        max_tokens=9000,
    )
    code_plan.role = "plan_code"; stages.append(code_plan)
    if code_plan.status != "completed":
        return TeamRunResult("failed", "", code_plan.model, stages, [], {"ledger": ledger.as_dict()}, code_plan.error)
    _stage_complete(ledger, TeamStage.PLAN_CODE, event_fn)

    # 4) code — isolated parallel candidates with one fair global backing mode.
    workspace_plan = team_workspace_plan(
        source_workspace, len(config.coders), str(state.get("workspace_mode") or "auto")
    )
    _emit(event_fn, "team_workspace_plan", **workspace_plan.as_dict())
    _stage_start(ledger, TeamStage.CODE, event_fn)
    with ThreadPoolExecutor(max_workers=len(config.coders), thread_name_prefix="aicoder-coder") as pool:
        futures = {
            pool.submit(
                _run_candidate, client=client, model_client=model_client, source_workspace=source_workspace,
                backend_mode=workspace_plan.backend_mode, slot=slot.slot, model=slot.model,
                strategy=slot.strategy, task=task, plan=code_plan.response, coordinator="",
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
        max_tokens=6000,
    )
    merge_plan.role = "merge_plan"; stages.append(merge_plan)
    if merge_plan.status != "completed":
        integration.abort(); [c.workspace.abort() for c in candidates]
        return TeamRunResult("failed", "", merge_plan.model, stages, candidates, {"ledger": ledger.as_dict()}, merge_plan.error)
    integration.write_candidate_artifact(".aicoder-team/merge-plan.txt", merge_plan.response)
    _stage_complete(ledger, TeamStage.MERGE_PLAN, event_fn)

    # 6) merge — optional LLM. Empty merge slot means deterministic winner only.
    _stage_start(ledger, TeamStage.MERGE, event_fn)
    merge_model = config.merge_model
    if merge_model:
        merge_runtime = NativeLightRuntime(
            client=client, model_client=model_client,
            initial_prompt=(f"USER TASK:\n{task}\n\nCODE CONTRACT:\n{code_plan.response}\n\n"
                            f"BLIND MERGE CONTRACT:\n{merge_plan.response}\n\n"
                            "Candidate snapshots are under .aicoder-team/candidates/. Integrate only evidence-backed improvements."),
            model=merge_model, fallback_model=None, workspace_root=str(integration.info.execution_root),
            plan_workspace_root=source_workspace, protected_workspace_root=source_workspace,
            tools=coder_tools, system_prompt=build_system_prompt(coder_tools, str(integration.info.execution_root)).rstrip()+"\n\n"+MERGE_SYSTEM_PROMPT,
            load_tools_on_start=True, quick_chat=False, persistent_plan=False,
            approval_fn=_candidate_approval, max_iterations=14, max_output_tokens=10000, stop_requested=stop_requested,
        )
        merge_started = time.monotonic(); merge_run = merge_runtime.run()
        merge_elapsed = int((time.monotonic() - merge_started) * 1000)
        stages.append(AgentStageResult("merge", merge_run.model or merge_model, merge_run.status, merge_run.response, merge_elapsed, merge_run.error))
        if merge_run.status != "completed":
            integration.abort(); [c.workspace.abort() for c in candidates]
            return TeamRunResult("failed", "", merge_run.model, stages, candidates, {"ledger": ledger.as_dict()}, merge_run.error or "merge failed")
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
            prompt=(f"USER TASK:\n{task}\n\nCODE CONTRACT:\n{code_plan.response}\n\nMERGE CONTRACT:\n{merge_plan.response}\n\n"
                    f"DETERMINISTIC REPOSITORY CHECKS (authoritative):\n{test_plan_text}"),
            max_tokens=5000,
        )
        test_plan.role = "plan_tests"; stages.append(test_plan)
        if test_plan.status != "completed":
            integration.abort(); [c.workspace.abort() for c in candidates]
            return TeamRunResult("failed", "", test_plan.model, stages, candidates, {"ledger": ledger.as_dict()}, test_plan.error)
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
        integration.abort(); [c.workspace.abort() for c in candidates]
        return TeamRunResult(
            "failed", "", result_model, stages, candidates,
            {"ledger": ledger.as_dict(), "verification": verification_payload},
            "tests_function_ok gate failed; persistent workspace was not modified",
        )
    _stage_complete(ledger, TeamStage.TESTS_FUNCTION_OK, event_fn)

    # 9) atomic_disk_write — the only persistent mutation stage.
    _stage_start(ledger, TeamStage.ATOMIC_DISK_WRITE, event_fn)
    integration.finalize(verified=True)
    for c in candidates:
        c.workspace.abort()
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
        "ledger": ledger.as_dict(), "verification": verification_payload,
        "stage_timings": [
            {"role": stage.role, "model": stage.model, "status": stage.status, "elapsed_ms": stage.elapsed_ms}
            for stage in stages
        ],
    }
    _emit(event_fn, "team_complete", **perf)
    return TeamRunResult("completed", final_response, result_model or "", stages, candidates, perf)
