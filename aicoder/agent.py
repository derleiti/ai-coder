from __future__ import annotations
"""
agent.py — CLI Agent runner. Uses shared executor for tool execution.
"""
import json
import sys
from typing import Optional

from .client import TriForceClient
from .config import load_session
from .executor import (
    AGENT_TOOLS, FALLBACK_TOOLS as _FALLBACK_TOOLS, OS_INSTRUCTIONS, OS_NAME,
    SYSTEM_TEMPLATE as SYSTEM, is_destructive, is_short_confirmation,
    is_simple_chat_message, run_tool as run_tool, should_load_tools,
)
from .history import record as history_record
from .privileges import (
    PrivilegeBroker, assess_execution, format_request,
)
from .session_state import DEFAULT_RUNTIME_MODE, get_state
from .workspace import active_workspace
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
    from .agent_runtime import NativeLightRuntime

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

    def on_event(kind: str, payload: dict) -> None:
        nonlocal header_printed
        if json_events:
            print(json.dumps({"type": kind, **payload}, ensure_ascii=False, default=_json_default))
        if json_output or json_events:
            return
        if kind == "run_start":
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
            print_final(
                response=str(payload.get("response") or ""),
                model=str(payload.get("model") or "?"),
                latency_ms=int(payload.get("latency_ms") or 0),
                total_iters=int(payload.get("iterations") or 0),
                fallback_used=bool(payload.get("fallback_used")),
            )
        elif kind == "error":
            print_error(str(payload.get("message") or "native-light runtime failed"))
        elif kind == "paused":
            print_error(str(payload.get("reason") or "native-light runtime paused"))

    runtime = NativeLightRuntime(
        client=client,
        initial_prompt=initial_prompt,
        model=model,
        fallback_model=fallback_model,
        workspace_root=str(ws_path),
        tools=None if should_load_tools_now else [],
        load_tools_on_start=should_load_tools_now,
        enabled_tool_names=enabled_tool_names,
        quick_chat=quick_chat,
        approval_fn=_headless_approval if (json_output or json_events) else _cli_approval,
        event_fn=on_event,
        conversation=conversation,
        persistent_plan=persistent_plan,
        resume=resume_requested,
        resume_plan_id=resume_plan_id if persistent_plan else None,
        base_timeout=request_timeout,
        max_output_tokens=int(state.get("max_output_tokens", 16384)),
        tools_unavailable_reason=tools_unavailable_reason,
        progressive_tool_disclosure=(tool_mode == "on_demand"),
        native_openrouter_tool_calling=bool(state.get("native_openrouter_tool_calling", False)),
    )
    result = runtime.run()
    if not header_printed and not (json_output or json_events):
        print_header(
            model=model or "backend-default", fallback=fallback_model or "", tools=0,
            workspace=ws_path.name, tool_mode=f"{runtime_label}/{tool_mode}", timeout=request_timeout,
        )
        print_task(initial_prompt)

    swarm_mode = state.get("swarm_mode", "off")
    if swarm_mode == "auto":
        from .swarm_runner import should_auto_swarm
        swarm_mode = "review" if should_auto_swarm(initial_prompt) else "off"
    if (
        not (json_output or json_events)
        and swarm_mode in {"on", "review"}
        and result.response
        and result.status == "completed"
    ):
        from .swarm_runner import run_swarm_review
        run_swarm_review(
            original_task=initial_prompt,
            operator_response=result.response,
            operator_model=result.model,
            fallback_model=state.get("fallback_model"),
            system_prompt=result.system_prompt,
            client=client,
        )

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
