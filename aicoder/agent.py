from __future__ import annotations
"""
agent.py — CLI Agent runner. Uses shared executor for tool execution.
"""
import json
import sys
import threading
import time
from typing import Optional

from .client import TriForceClient
from .config import load_session
from .executor import (
    AGENT_TOOLS, RECOVERY_TOOLS as _RECOVERY_TOOLS, OS_INSTRUCTIONS, OS_NAME,
    SYSTEM_TEMPLATE as SYSTEM, is_destructive, is_short_confirmation,
    is_simple_chat_message, run_tool as run_tool, should_load_tools,
)
from .history import record as history_record
from .privileges import (
    PrivilegeBroker, assess_execution, format_request,
)
from .session_state import DEFAULT_RUNTIME_MODE, get_state
from .workspace import active_workspace, workspace_from_task
from .workspace_backend import open_workspace_for_run, preserve_workspace_for_resume
from .ui import (
    C,
    print_header, print_task, print_thought,
    print_tool_call, print_tool_result, print_final,
    print_error,
)


def _cli_approval(tool_name: str, args: dict) -> bool:
    """Explain and approve one risky workspace action in the terminal."""
    cmd = args.get("command", "")
    risk = assess_execution(tool_name, args, destructive=is_destructive(cmd))
    scope_target = str(args.get("_workspace_escape") or "")
    scope_root = str(args.get("_workspace_root") or "")
    if not risk.needs_approval and not scope_target:
        return True

    mode = get_state().get("approval_mode", "ask")
    decision = PrivilegeBroker.evaluate(mode, risk, workspace_escape=bool(scope_target), headless=False)
    automatic = decision.automatic
    if automatic:
        print(f"\n{C.BGREEN}✓ Automatisch freigegeben ({mode}){C.RESET}", file=sys.stderr)
    else:
        print(f"\n{C.BYELLOW}{C.BOLD}◆ Lokale Freigabe erforderlich{C.RESET}", file=sys.stderr)
    print(format_request(risk), file=sys.stderr)
    if scope_target:
        print(f"  Scope   : Workspace verlassen", file=sys.stderr)
        print(f"  Root    : {scope_root}", file=sys.stderr)
        print(f"  Ziel    : {scope_target}", file=sys.stderr)
    if not automatic:
        try:
            confirm = input("  Einmal erlauben? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "n"
        if confirm not in {"y", "yes", "j", "ja"}:
            print("  Abgelehnt.", file=sys.stderr)
            return False

    if risk.elevation:
        ok, message = PrivilegeBroker.authenticate_terminal()
        if not ok:
            print(f"  Elevation blocked: {message}", file=sys.stderr)
            return False
        args["_elevation_strategy"] = "sudo"

    return True



def _json_default(value):
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return str(value)


def _headless_approval(tool_name: str, args: dict) -> bool:
    risk = assess_execution(
        tool_name, args, destructive=is_destructive(str(args.get("command", "")))
    )
    mode = get_state().get("approval_mode", "ask")
    decision = PrivilegeBroker.evaluate(
        mode, risk, workspace_escape=bool(args.get("_workspace_escape")), headless=True
    )
    return decision.automatic and not decision.denied


def _native_exit_code(status: str) -> int:
    if status == "completed":
        return 0
    if status == "paused":
        return 3
    return 1

def _run_native_light_agent(
    initial_prompt: str,
    model: Optional[str],
    fallback_model: Optional[str],
    *,
    conversation: Optional[list[dict]],
    state: dict,
    resume_plan_id: Optional[str] = None,
    json_output: bool = False,
    json_events: bool = False,
    persistent_plan: bool = True,
) -> int:
    from .agent_runtime import (
        NativeLightRuntime, MAX_AUTO_RESUMES, auto_resumable_pause,
        auto_resume_limit, auto_resume_prompt, continuation_messages,
    )

    session = load_session()
    request_timeout = int(state.get("request_timeout", 300))
    client = TriForceClient(session.base_url, token=session.token, timeout=request_timeout)
    ws_path = active_workspace(state.get("workspace_root"))
    resume_requested = persistent_plan and (
        is_short_confirmation(initial_prompt) or resume_plan_id is not None
    )
    quick_chat = is_simple_chat_message(initial_prompt) and not resume_requested
    runtime_label = "native-light" if persistent_plan else "classic"
    tool_mode = state.get("tool_mode", "on_demand")
    enabled_tool_names = state.get("enabled_tools")
    tools_requested = should_load_tools(tool_mode, initial_prompt, resume=resume_requested)
    should_load_tools_now = tools_requested and enabled_tool_names != []
    tools_unavailable_reason = ""
    if tools_requested and enabled_tool_names == []:
        tools_unavailable_reason = (
            "No tools are enabled. Complete tool onboarding in Settings by loading and "
            "selecting tools before running an action task."
        )

    header_printed = False
    heartbeat_lock = threading.Lock()
    heartbeat_stop: threading.Event | None = None

    def stop_model_heartbeat() -> None:
        nonlocal heartbeat_stop
        with heartbeat_lock:
            if heartbeat_stop is not None:
                heartbeat_stop.set()
                heartbeat_stop = None

    def start_model_heartbeat(payload: dict) -> None:
        nonlocal heartbeat_stop
        stop_model_heartbeat()
        stop = threading.Event()
        with heartbeat_lock:
            heartbeat_stop = stop
        started = time.monotonic()
        model_name = str(payload.get("model") or "backend")
        phase = str(payload.get("phase") or "planning")
        timeout_s = int(payload.get("timeout") or request_timeout)
        request_id = str(payload.get("request_id") or "")
        req = f" · req {request_id[-8:]}" if request_id else ""
        print(f"\n  {C.DIM}⏳ Waiting for model · {model_name} · {phase} · idle timeout {timeout_s}s{req}{C.RESET}", file=sys.stderr, flush=True)

        def heartbeat() -> None:
            while not stop.wait(10.0):
                elapsed = int(time.monotonic() - started)
                print(f"  {C.DIM}… model still running · {elapsed}s elapsed · idle timeout {timeout_s}s{req}{C.RESET}", file=sys.stderr, flush=True)

        threading.Thread(target=heartbeat, name="aicoder-cli-model-heartbeat", daemon=True).start()

    def on_event(kind: str, payload: dict) -> None:
        nonlocal header_printed
        if json_events:
            print(json.dumps({"type": kind, **payload}, ensure_ascii=False, default=_json_default))
        if json_output or json_events:
            return
        if kind == "run_start":
            if not header_printed:
                print_header(
                    model=payload.get("model") or "backend-default",
                    fallback=payload.get("fallback") or "",
                    tools=int(payload.get("tools") or 0),
                    workspace=ws_path.name,
                    tool_mode=f"{runtime_label}/{tool_mode}",
                    timeout=request_timeout,
                )
                print_task(initial_prompt)
                plan_id = str(payload.get("plan_id") or "")
                if plan_id:
                    print(f"  {C.DIM}plan {plan_id} · persistent native-light runtime{C.RESET}")
                header_printed = True
            else:
                print(f"  {C.BMAGENTA}↻ runtime resumed{C.RESET} {C.DIM}{payload.get('model') or 'backend-default'}{C.RESET}", file=sys.stderr, flush=True)
        elif kind == "model_start":
            start_model_heartbeat(payload)
        elif kind == "model_response":
            stop_model_heartbeat()
            elapsed = int(payload.get("elapsed_ms") or 0) / 1000.0
            request_id = str(payload.get("request_id") or "")
            req = f" · req {request_id[-8:]}" if request_id else ""
            model_name = str(payload.get("model") or payload.get("requested") or "?")
            token_meta = " · tok/s n/a"
            if payload.get("usage_available"):
                token_meta = (f" · in {int(payload.get('input_tokens') or 0)} · out {int(payload.get('output_tokens') or 0)}"
                              f" · {float(payload.get('tokens_per_second') or 0.0):.1f} tok/s")
            print(f"  {C.DIM}✓ Model response · {model_name} · {elapsed:.1f}s{token_meta}{req}{C.RESET}", file=sys.stderr, flush=True)
        elif kind == "runtime_status":
            category = str(payload.get("category") or "run").lower()
            status = str(payload.get("status") or "info").lower()
            phase = str(payload.get("phase") or "runtime")
            message = str(payload.get("message") or "").strip()
            iteration = int(payload.get("iteration") or 0)
            runtime_mode = str(payload.get("runtime_mode") or runtime_label)
            icon = "◆"
            color = C.BCYAN
            if category == "verify":
                icon, color = ("✓", C.BGREEN) if status == "ok" else ("◇", C.BYELLOW)
            elif category == "policy":
                icon, color = ("✓", C.BGREEN) if status == "ok" else ("⚠", C.BYELLOW)
            elif category == "recovery":
                icon, color = "↻", C.BMAGENTA
            elif category == "error" or status in {"failed", "error", "blocked"}:
                icon, color = "✗", C.BRED
            elif status == "completed":
                icon, color = "✓", C.BGREEN
            step = f" · step {iteration}" if iteration else ""
            print(
                f"  {color}{C.BOLD}{icon} {category.upper()}{C.RESET} "
                f"{C.DIM}{runtime_mode} · {phase}{step}{C.RESET} · {message}",
                file=sys.stderr, flush=True,
            )
        elif kind == "performance_warning":
            warning_kind = str(payload.get("kind") or "performance")
            elapsed = int(payload.get("elapsed_ms") or 0) / 1000.0
            if warning_kind == "model_latency":
                print(
                    f"  {C.BYELLOW}⚠ Performance · hohe Model/API-Latenz: {elapsed:.1f}s{C.RESET}",
                    file=sys.stderr, flush=True,
                )
            elif warning_kind == "filesystem_latency":
                tool_name = str(payload.get("tool") or "filesystem")
                print(
                    f"  {C.BYELLOW}⚠ Performance · langsames I/O: {tool_name} {elapsed:.1f}s{C.RESET}",
                    file=sys.stderr, flush=True,
                )
        elif kind == "performance_summary":
            wall = int(payload.get("wall_ms") or 0) / 1000.0
            model_s = int(payload.get("model_ms") or 0) / 1000.0
            tools_s = int(payload.get("tool_ms") or 0) / 1000.0
            io_s = int(payload.get("filesystem_ms") or 0) / 1000.0
            bottleneck = str(payload.get("bottleneck") or "?")
            token_meta = " · tok/s n/a"
            if int(payload.get("tokenized_model_requests") or 0):
                token_meta = (f" · tokens in/out {int(payload.get('input_tokens') or 0)}/{int(payload.get('output_tokens') or 0)}"
                              f" · {float(payload.get('output_tokens_per_second') or 0.0):.1f} tok/s")
            print(
                f"  {C.DIM}⚡ Performance · wall {wall:.1f}s · model {model_s:.1f}s · "
                f"tools {tools_s:.1f}s · I/O {io_s:.1f}s{token_meta} · bottleneck {bottleneck}{C.RESET}",
                file=sys.stderr, flush=True,
            )
        elif kind == "model_without_tool_support":
            model_name = payload.get("model") or "?"
            print(
                f"\n{C.BYELLOW}◆ {model_name} meldet kein natives Function Calling{C.RESET} — "
                "AICoder verwendet weiterhin sein textbasiertes Tool-Calling; "
                "native Provider-Toolschemas werden nicht mitgesendet.",
                file=sys.stderr,
            )
        elif kind == "thought":
            print_thought(str(payload.get("text") or ""))
        elif kind == "tool_call":
            stop_model_heartbeat()
            print_tool_call(
                str(payload.get("name") or "?"),
                payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
                max(0, int(payload.get("iteration") or 1) - 1),
            )
        elif kind == "tool_result":
            print_tool_result(
                str(payload.get("name") or "?"),
                str(payload.get("result") or ""),
                float(payload.get("elapsed") or 0.0),
                error=bool(payload.get("is_error")),
            )
        elif kind == "model_switch":
            print_thought(
                f"Loop recovery: {payload.get('previous', '?')} → {payload.get('model', '?')}"
            )
        elif kind == "final":
            stop_model_heartbeat()
            print_final(
                response=str(payload.get("response") or ""),
                model=str(payload.get("model") or "?"),
                latency_ms=int(payload.get("latency_ms") or 0),
                total_iters=int(payload.get("iterations") or 0),
                fallback_used=bool(payload.get("fallback_used")),
            )
        elif kind == "error":
            stop_model_heartbeat()
            print_error(str(payload.get("message") or "native-light runtime failed"))
        elif kind == "paused":
            stop_model_heartbeat()
            print_error(str(payload.get("reason") or "native-light runtime paused"))

    workspace_mode = str(state.get("workspace_mode", "auto") or "auto")
    workspace_backend = open_workspace_for_run(
        str(ws_path), workspace_mode, resume=resume_requested, resume_plan_id=resume_plan_id,
    )
    execution_root = workspace_backend.info.execution_root
    if not (json_output or json_events):
        info = workspace_backend.info
        detail = f"execution workspace: {info.mode}"
        if info.mode == "ram":
            detail += f" · RAM transactional · budget {info.safe_budget_bytes // (1024**2)} MiB"
            if info.restored_checkpoint:
                detail += " · checkpoint restored"
        elif info.fallback_reason:
            detail += f" · fallback: {info.fallback_reason}"
        print(f"  {C.DIM}⚙ {detail}{C.RESET}", file=sys.stderr, flush=True)

    def make_runtime(
        prompt_text: str, conv: Optional[list[dict]], *, resume_flag: bool,
        plan_id: Optional[str], force_full: bool = False,
    ) -> NativeLightRuntime:
        return NativeLightRuntime(
            client=client,
            initial_prompt=prompt_text,
            model=model,
            fallback_model=None,
            workspace_root=str(execution_root),
            plan_workspace_root=str(ws_path),
            protected_workspace_root=(str(ws_path) if workspace_backend.info.transactional else None),
            completion_guard=(lambda: workspace_backend.finalize(verified=True)),
            tools=None if should_load_tools_now else [],
            load_tools_on_start=should_load_tools_now,
            enabled_tool_names=enabled_tool_names,
            quick_chat=(False if force_full else quick_chat),
            approval_fn=_headless_approval if (json_output or json_events) else _cli_approval,
            event_fn=on_event,
            conversation=conv,
            persistent_plan=persistent_plan,
            resume=resume_flag,
            resume_plan_id=plan_id if persistent_plan else None,
            base_timeout=request_timeout,
            max_output_tokens=int(state.get("max_output_tokens", 16384)),
            tools_unavailable_reason=tools_unavailable_reason,
            progressive_tool_disclosure=(tool_mode == "on_demand"),
            native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
        )

    runtime = make_runtime(
        initial_prompt, conversation, resume_flag=resume_requested,
        plan_id=resume_plan_id if persistent_plan else None,
    )
    result = runtime.run()
    auto_resume_attempts = {"continuation": 0, "recovery": 0}
    while result.status == "paused" and auto_resumable_pause(result.response or result.error):
        reason = result.response or result.error
        slice_mode = "continuation" if "safety pause after an unusually long run" in reason.lower() else "recovery"
        limit = auto_resume_limit(reason)
        if auto_resume_attempts[slice_mode] >= limit:
            break
        auto_resume_attempts[slice_mode] += 1
        auto_resume_attempt = auto_resume_attempts[slice_mode]
        on_event("runtime_status", {
            "category": "recovery", "status": "resuming", "phase": "auto_resume",
            "runtime_mode": runtime_label,
            "message": f"automatic {slice_mode} {auto_resume_attempt}/{limit}: {reason[:1000]}",
        })
        if persistent_plan and result.plan_id:
            next_prompt = "continue"
            next_conversation = conversation
            next_resume = True
            next_plan_id = result.plan_id
        else:
            next_prompt = auto_resume_prompt(reason, auto_resume_attempt, limit)
            next_conversation = continuation_messages(result)
            next_resume = False
            next_plan_id = None
        runtime = make_runtime(
            next_prompt, next_conversation, resume_flag=next_resume,
            plan_id=next_plan_id, force_full=True,
        )
        result = runtime.run()
    if result.status == "paused" and auto_resumable_pause(result.response or result.error):
        reason = result.response or result.error
        slice_mode = "continuation" if "safety pause after an unusually long run" in reason.lower() else "recovery"
        limit = auto_resume_limit(reason)
        on_event("runtime_status", {
            "category": "recovery", "status": "failed", "phase": "auto_resume",
            "runtime_mode": runtime_label,
            "message": (f"automatic {slice_mode} budget exhausted after {auto_resume_attempts[slice_mode]}/{limit} attempt(s); "
                        f"continuations={auto_resume_attempts['continuation']}, recoveries={auto_resume_attempts['recovery']}"),
        })
    if result.status != "completed":
        preserve_workspace_for_resume(workspace_backend, result.plan_id or None)
    stop_model_heartbeat()
    if not header_printed and not (json_output or json_events):
        print_header(
            model=model or "backend-default", fallback="", tools=0,
            workspace=ws_path.name, tool_mode=f"{runtime_label}/{tool_mode}", timeout=request_timeout,
        )
        print_task(initial_prompt)

    try:
        history_record(
            kind="ask", prompt=initial_prompt, response=result.response,
            model=result.model, latency_ms=result.latency_ms,
        )
    except Exception:
        pass
    if json_output or json_events:
        print(json.dumps({
            "type": "result",
            "status": result.status,
            "response": result.response,
            "model": result.model,
            "plan_id": result.plan_id,
            "iterations": result.iterations,
            "latency_ms": result.latency_ms,
            "fallback_used": result.fallback_used,
            "error": result.error,
        }, ensure_ascii=False))
    if persistent_plan:
        return _native_exit_code(result.status)
    return 1 if result.status == "failed" else 0


def run_agent(
    initial_prompt: str,
    model: Optional[str],
    fallback_model: Optional[str],
    verbose: bool = False,
    conversation: Optional[list[dict]] = None,
    runtime_mode: Optional[str] = None,
    resume_plan_id: Optional[str] = None,
    json_output: bool = False,
    json_events: bool = False,
) -> int:
    state = get_state()
    from .team_runtime import config_from_state, should_use_team
    from .team_orchestrator import load_latest_team_checkpoint
    team_checkpoint = None
    if not resume_plan_id and is_short_confirmation(initial_prompt):
        team_checkpoint = load_latest_team_checkpoint(str(active_workspace(state.get("workspace_root"))))
    if (
        not resume_plan_id
        and (
            team_checkpoint is not None
            or (not is_short_confirmation(initial_prompt) and should_use_team(initial_prompt, str(state.get("team_runtime_mode") or "off")))
        )
    ):
        from .model_transport import native_model_transport_from_env
        from .team_orchestrator import run_team
        session = load_session()
        request_timeout = int(state.get("request_timeout", 300))
        client = TriForceClient(session.base_url, token=session.token, timeout=request_timeout)
        if team_checkpoint is not None:
            source_workspace = str(team_checkpoint.get("source_workspace") or active_workspace(state.get("workspace_root")))
            team_task = str(team_checkpoint.get("task") or initial_prompt)
        else:
            source_workspace = str(workspace_from_task(initial_prompt, state.get("workspace_root")))
            team_task = initial_prompt
        model_client, _ = native_model_transport_from_env(client, default_model=model or state.get("selected_model"))

        def team_event(kind: str, payload: dict) -> None:
            if json_events:
                print(json.dumps({"type": kind, **payload}, ensure_ascii=False, default=_json_default))
                return
            if json_output:
                return
            if kind == "team_start":
                print(f"  {C.DIM}◆ Team runtime · research={payload.get('research')} · coders={payload.get('coders')}{C.RESET}", file=sys.stderr)
            elif kind == "team_resume":
                print(f"  {C.BMAGENTA}{C.BOLD}↻ TEAM RESUME · {payload.get('stage') or '?'}{C.RESET} {C.DIM}· {payload.get('source_workspace') or ''}{C.RESET}", file=sys.stderr, flush=True)
            elif kind == "team_pipeline":
                stage = str(payload.get("stage") or "?")
                status = str(payload.get("status") or "?")
                marker = "→" if status == "started" else "✓"
                print(f"  {C.DIM}{marker} {stage} · {status}{C.RESET}", file=sys.stderr, flush=True)
            elif kind == "team_workspace_plan":
                reason = str(payload.get("reason") or "")
                suffix = f" · {reason}" if reason else ""
                print(
                    f"  {C.DIM}◆ workspace={payload.get('backend_mode')} · candidates={payload.get('candidate_count')}{suffix}{C.RESET}",
                    file=sys.stderr,
                )
            elif kind == "team_stage":
                telemetry = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else {}
                elapsed = int(payload.get("elapsed_ms") or 0) / 1000.0
                token_meta = " · tok/s n/a"
                if telemetry.get("usage_available"):
                    token_meta = f" · in/out {int(telemetry.get('input_tokens') or 0)}/{int(telemetry.get('output_tokens') or 0)} · {float(telemetry.get('tokens_per_second') or 0.0):.1f} tok/s"
                error = str(payload.get("error") or "").strip()
                detail = str(payload.get("detail") or "").strip().replace("\n", " ")
                suffix = f" · ERROR {error}" if error else (f" · {detail[:320]}" if detail else "")
                print(f"  {C.DIM}✓ {payload.get('role')} · {payload.get('status')} · {payload.get('model')} · {elapsed:.1f}s{token_meta}{suffix}{C.RESET}", file=sys.stderr, flush=True)
            elif kind == "team_worker_event":
                role = str(payload.get("role") or "worker")
                event = str(payload.get("event") or "?")
                if event == "runtime_status":
                    category = str(payload.get("category") or "run").upper()
                    status = str(payload.get("status") or "info").lower()
                    color = C.BGREEN if status in {"ok", "completed"} else (C.BYELLOW if status in {"warning", "required", "blocked"} else C.BCYAN)
                    if status in {"failed", "error"}:
                        color = C.BRED
                    print(f"  {color}{C.BOLD}◆ {role} · {category}{C.RESET} {C.DIM}{payload.get('phase') or 'runtime'}{C.RESET} · {payload.get('message') or ''}", file=sys.stderr, flush=True)
                elif event == "model_start":
                    print(f"  {C.DIM}⏳ {role} · {payload.get('model')} · {payload.get('phase')} · timeout {payload.get('timeout')}s{C.RESET}", file=sys.stderr, flush=True)
                elif event == "model_response":
                    elapsed = int(payload.get("elapsed_ms") or 0) / 1000.0
                    tok = f" · {float(payload.get('tokens_per_second') or 0.0):.1f} tok/s · in/out {int(payload.get('input_tokens') or 0)}/{int(payload.get('output_tokens') or 0)}" if payload.get("usage_available") else " · tok/s n/a"
                    print(f"  {C.DIM}✓ {role} · model {payload.get('model')} · {elapsed:.1f}s{tok}{C.RESET}", file=sys.stderr, flush=True)
                elif event == "thought":
                    text = str(payload.get("text") or "").strip()
                    if text:
                        print(f"  {C.DIM}… {role} · {text[:1200]}{C.RESET}", file=sys.stderr, flush=True)
                elif event == "final":
                    text = str(payload.get("response") or "").strip()
                    if text:
                        print(f"  {C.BMAGENTA}{C.BOLD}◆ MODEL OUTPUT · {role}{C.RESET}", file=sys.stderr, flush=True)
                        for line in text.splitlines()[:40]:
                            print(f"    {line[:220]}", file=sys.stderr, flush=True)
                elif event == "tool_call":
                    print(f"  {C.DIM}→ {role} · tool {payload.get('name')}{C.RESET}", file=sys.stderr, flush=True)
                elif event == "tool_result":
                    state = "ERROR" if payload.get("is_error") else "OK"
                    print(f"  {C.DIM}← {role} · tool {payload.get('name')} · {state}{C.RESET}", file=sys.stderr, flush=True)
                    if payload.get("is_error") and payload.get("result"):
                        print(f"    {C.BYELLOW}{str(payload.get('result'))[-800:]}{C.RESET}", file=sys.stderr, flush=True)
                elif event in {"error", "paused"}:
                    print(f"  {C.BYELLOW}⚠ {role} · {event} · {payload.get('message') or payload.get('reason')}{C.RESET}", file=sys.stderr, flush=True)
            elif kind == "team_model_output":
                role = str(payload.get("role") or "planner")
                text = str(payload.get("text") or "").strip()
                if text:
                    print(f"  {C.BMAGENTA}{C.BOLD}◆ MODEL OUTPUT · {role} · {payload.get('model') or '?'}{C.RESET}", file=sys.stderr, flush=True)
                    for line in text.splitlines()[:40]:
                        print(f"    {line[:220]}", file=sys.stderr, flush=True)
            elif kind == "team_candidate":
                print(f"  {C.DIM}◆ {payload.get('candidate_id')} · {payload.get('status')} · score={payload.get('score')}{C.RESET}", file=sys.stderr)
            elif kind == "team_verification":
                state = "OK" if payload.get("ok") else "FAIL"
                elapsed = int(payload.get("elapsed_ms") or 0) / 1000.0
                print(f"  {C.DIM}◆ verify {payload.get('name')} · {state} · {elapsed:.2f}s · exit={payload.get('exit_code')}{C.RESET}", file=sys.stderr, flush=True)
                if not payload.get("ok") and payload.get("output"):
                    print(f"    {C.BYELLOW}{str(payload.get('output'))[-800:]}{C.RESET}", file=sys.stderr, flush=True)
            elif kind == "team_error":
                print(f"  {C.BYELLOW}⚠ team error · {payload.get('stage')} · {payload.get('error')}{C.RESET}", file=sys.stderr, flush=True)
            elif kind == "team_complete":
                print(f"  {C.DIM}⚡ team complete · winner={payload.get('winner_candidate_id')} · wall={int(payload.get('wall_ms') or 0)/1000.0:.1f}s{C.RESET}", file=sys.stderr)

        result = run_team(
            task=team_task, state=state, config=config_from_state(state), client=client,
            model_client=model_client, source_workspace=source_workspace, event_fn=team_event,
            resume_checkpoint=team_checkpoint,
        )
        if json_output or json_events:
            print(json.dumps({
                "type": "result", "status": result.status, "response": result.response,
                "model": result.model, "team": True, "performance": result.performance,
                "error": result.error,
            }, ensure_ascii=False))
        elif result.status == "completed":
            print_final(response=result.response, model=result.model, latency_ms=int(result.performance.get("wall_ms") or 0), total_iters=0, fallback_used=False)
        else:
            print_error(result.error or "Team runtime failed")
        return 0 if result.status == "completed" else 1

    effective_runtime = runtime_mode or state.get("runtime_mode", DEFAULT_RUNTIME_MODE)
    if json_output or json_events:
        effective_runtime = "native-light"
    return _run_native_light_agent(
        initial_prompt, model, fallback_model,
        conversation=conversation, state=state,
        resume_plan_id=resume_plan_id,
        json_output=json_output,
        json_events=json_events,
        persistent_plan=(effective_runtime == "native-light"),
    )
