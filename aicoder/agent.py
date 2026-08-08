from __future__ import annotations
"""
agent.py — CLI Agent runner. Uses shared executor for tool execution.
"""
import json
import sys
import time
from pathlib import Path
from typing import Optional

from .client import ClientError, TriForceClient
from .config import load_session
from .executor import (
    AGENT_CHECKPOINT_INTERVAL, MAX_CONTEXT_MESSAGES, MAX_ITERATIONS,
    STALL_FALLBACK_REPEATS, STALL_NUDGE_REPEATS, STALL_RECOVERY_PROMPT,
    AgentLoopGuard, agent_checkpoint,
    is_destructive, load_tools, build_system_prompt,
    normalize_tool_calls, parse_tool_calls, strip_tool_calls, trim_messages, run_tool,
    is_action_request, is_short_confirmation, is_simple_chat_message,
    # Re-export for backwards compat (GUI imports these)
    AGENT_TOOLS, LOCAL_EXEC_SCHEMA, SYSTEM_TEMPLATE as SYSTEM,
    FALLBACK_TOOLS as _FALLBACK_TOOLS, OS_NAME, OS_INSTRUCTIONS,
)
from .history import record as history_record
from .privileges import (
    approval_is_automatic, assess_execution, format_request, validate_sudo_session,
)
from .session_state import get_state
from .ui import (
    AgentSpinner, C,
    print_header, print_task, print_thought,
    print_tool_call, print_tool_result, print_final,
    print_error,
)


def _cli_approval(tool_name: str, args: dict) -> bool:
    """Explain and approve one risky local action in the terminal.

    Sudo authentication is intentionally delegated to the local TTY. Password
    bytes never pass through ai-coder, its audit log, the model, or TriForce.
    """
    cmd = args.get("command", "")
    risk = assess_execution(tool_name, args, destructive=is_destructive(cmd))
    if not risk.needs_approval:
        return True

    mode = get_state().get("approval_mode", "ask")
    if risk.elevation and not risk.user_reason:
        print(
            f"\n{C.BRED}✗ Privilegienanfrage abgelehnt:{C.RESET} "
            "Das Modell muss einen konkreten Grund mitliefern.",
            file=sys.stderr,
        )
        return False

    automatic = approval_is_automatic(mode, risk)
    if automatic:
        print(f"\n{C.BGREEN}✓ Automatisch freigegeben ({mode}){C.RESET}", file=sys.stderr)
    else:
        print(f"\n{C.BYELLOW}{C.BOLD}◆ Lokale Freigabe erforderlich{C.RESET}", file=sys.stderr)
    print(format_request(risk), file=sys.stderr)
    if not automatic:
        try:
            confirm = input("  Einmal erlauben? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "n"
        if confirm not in {"y", "yes", "j", "ja"}:
            print("  Abgelehnt.", file=sys.stderr)
            return False

    if risk.sudo:
        print("  sudo authentifiziert jetzt direkt im lokalen Terminal.", file=sys.stderr)
        ok, message = validate_sudo_session()
        color = C.BGREEN if ok else C.BRED
        print(f"  {color}{message}{C.RESET}", file=sys.stderr)
        return ok
    return True


def run_agent(
    initial_prompt: str,
    model: Optional[str],
    fallback_model: Optional[str],
    verbose: bool = False,
    conversation: Optional[list[dict]] = None,
) -> int:
    session = load_session()
    state = get_state()
    request_timeout = int(state.get("request_timeout", 300))
    client = TriForceClient(session.base_url, token=session.token, timeout=request_timeout)
    ws_path = Path(state.get("workspace_root") or ".").resolve()

    # A greeting must not pay for MCP discovery.  REPL and GUI now use the same
    # on-demand policy and the same enabled-tools selection from Settings.
    quick_chat = is_simple_chat_message(initial_prompt)
    tool_mode = state.get("tool_mode", "on_demand")
    enabled_tool_names = state.get("enabled_tools")
    should_load_tools = (
        tool_mode == "always"
        or (tool_mode == "on_demand" and not quick_chat)
    ) and enabled_tool_names != []

    tools = []
    if should_load_tools:
        with AgentSpinner("discovering selected tools", color=C.DIM):
            tools = load_tools(client)
        if enabled_tool_names is not None:
            enabled = set(enabled_tool_names)
            tools = [tool for tool in tools if tool.get("name") in enabled]

    # Primary model stays primary. The fallback is only used after an actual
    # request failure or explicit loop recovery; quick chat must not silently
    # swap the configured route.
    effective_model = model
    effective_fallback = fallback_model

    system = build_system_prompt(tools, str(ws_path))

    # Header
    print_header(
        model=effective_model or "backend-default",
        fallback=effective_fallback or "",
        tools=len(tools),
        workspace=ws_path.name,
        tool_mode="skipped (fast chat)" if not should_load_tools and quick_chat else tool_mode,
        timeout=request_timeout,
    )
    print_task(initial_prompt)

    # Keep one conversation for the lifetime of the interactive REPL.  A
    # direct `aicoder agent <prompt>` call still gets a fresh list by default.
    prior_context = [
        dict(message) for message in (conversation or [])
        if message.get("role") != "system"
    ]
    messages: list[dict] = [
        {"role": "system", "content": system},
        *prior_context[-MAX_CONTEXT_MESSAGES:],
    ]
    current_input = initial_prompt
    full_response = ""
    model_used = effective_model or "?"
    total_latency = 0
    fallback_used = False
    tool_was_called = False
    tool_nudge_sent = False
    loop_guard = AgentLoopGuard()

    pending_continuation = False
    if is_short_confirmation(initial_prompt):
        for message in reversed(prior_context):
            content = str(message.get("content", ""))
            if message.get("role") == "assistant" and content.lstrip().upper().startswith("DONE:"):
                break
            if message.get("role") == "user" and not content.startswith("Tool "):
                pending_continuation = is_action_request(content)
                break
    must_use_tools = bool(tools) and (
        is_action_request(initial_prompt) or pending_continuation
    )

    for i in range(MAX_ITERATIONS):
        messages.append({"role": "user", "content": current_input})
        messages = trim_messages(messages)

        label = "thinking" if i == 0 else f"step {i+1}"

        with AgentSpinner(label, color=C.CYAN):
            t0 = time.time()
            try:
                result = client.chat(
                    messages=messages,
                    model=effective_model,
                    fallback_model=effective_fallback,
                    temperature=0.3,
                    max_tokens=256 if quick_chat else 4096,
                    tools=tools if should_load_tools else None,
                    tool_choice="auto",
                )
            except (ClientError, RuntimeError) as e:
                print_error(str(e))
                return 1
            llm_ms = int((time.time() - t0) * 1000)

        response = result.get("response", "").strip()
        model_used = result.get("model", effective_model or "?")
        lat = result.get("latency_ms") or llm_ms
        total_latency += lat
        if result.get("fallback_used"):
            fallback_used = True
        full_response = response

        native_calls = normalize_tool_calls(result.get("tool_calls") or [])
        calls = native_calls + [
            call for call in parse_tool_calls(response)
            if call not in native_calls
        ]
        if native_calls and not response:
            response = "\n".join(
                f"<tool_call>{json.dumps(call, ensure_ascii=False)}</tool_call>"
                for call in native_calls
            )
        visible = strip_tool_calls(response)

        if visible and calls:
            print_thought(visible)

        if not calls:
            if must_use_tools and not tool_was_called and not tool_nudge_sent:
                if response:
                    print_thought(response)
                messages.append({"role": "assistant", "content": response})
                current_input = (
                    "Continue the requested task now. No tool has been used yet. "
                    "Inspect the real local state with the most specific available tool, "
                    "then perform and verify the task. Do not only repeat a plan or ask "
                    "for generic confirmation. If execution is impossible, name the exact blocker."
                )
                tool_nudge_sent = True
                continue
            messages.append({"role": "assistant", "content": response})
            print_final(
                response=response,
                model=model_used,
                latency_ms=total_latency,
                total_iters=i + 1,
                fallback_used=fallback_used,
            )
            break

        # Tool loop
        tool_was_called = True
        tool_results = []
        for call in calls:
            tname = call.get("name", "?")
            targs = call.get("arguments", {})
            print_tool_call(tname, targs, i)

            risk = assess_execution(
                tname, targs,
                destructive=is_destructive(targs.get("command", "")),
            )
            if risk.needs_approval:
                approved = _cli_approval(tname, targs)
                if not approved:
                    tr, is_err = run_tool(
                        client, tname, targs,
                        approval_fn=lambda _name, _args: False,
                        model=model_used,
                        iteration=i,
                    )
                    t_elapsed = 0.0
                else:
                    with AgentSpinner(tname, tool=tname):
                        t_start = time.time()
                        tr, is_err = run_tool(
                            client, tname, targs,
                            approval_fn=lambda _name, _args: True,
                            model=model_used,
                            iteration=i,
                        )
                        t_elapsed = time.time() - t_start
            else:
                with AgentSpinner(tname, tool=tname):
                    t_start = time.time()
                    tr, is_err = run_tool(
                        client, tname, targs,
                        approval_fn=_cli_approval,
                        model=model_used,
                        iteration=i,
                    )
                    t_elapsed = time.time() - t_start

            print_tool_result(tname, tr, t_elapsed, error=is_err)
            tool_results.append(f"Tool {tname} result:\n{tr}")

        repeats = loop_guard.observe(calls, tool_results)
        if repeats == STALL_NUDGE_REPEATS:
            tool_results.append(STALL_RECOVERY_PROMPT)

        if repeats >= STALL_FALLBACK_REPEATS:
            fallback_candidate = effective_fallback
            if fallback_candidate and fallback_candidate != model_used:
                print_thought(
                    f"Tool loop detected; switching operator to fallback {fallback_candidate}."
                )
                tool_results.append(
                    f"Loop recovery: switch to {fallback_candidate} and continue the task "
                    "with a different approach. Do not repeat the prior call."
                )
                effective_model = fallback_candidate
                effective_fallback = None
                fallback_used = True
                loop_guard.reset()
            else:
                stop_reason = (
                    "Agent paused because the same tool operation kept repeating without "
                    "progress. The conversation context is preserved; correct the task or "
                    "type 'continue' to resume with a different approach."
                )
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": "\n\n".join(tool_results)})
                full_response = stop_reason
                print_error(stop_reason)
                break

        if (i + 1) % AGENT_CHECKPOINT_INTERVAL == 0:
            tool_results.append(agent_checkpoint(i + 1))

        messages.append({"role": "assistant", "content": response})
        current_input = "\n\n".join(tool_results)

        if response.strip().upper().startswith("DONE:"):
            print_final(response, model_used, total_latency, i+1, fallback_used)
            break
    else:
        if current_input:
            messages.append({"role": "user", "content": current_input})
        full_response = (
            "Agent safety pause after an unusually long run. The task context is "
            "preserved; type 'continue' to resume."
        )
        print_error(full_response)

    if conversation is not None:
        conversation[:] = [
            dict(message) for message in messages[1:]
        ][-MAX_CONTEXT_MESSAGES:]

    try:
        history_record(kind="ask", prompt=initial_prompt,
                       response=full_response, model=model_used,
                       latency_ms=total_latency)
    except Exception:
        pass
    return 0
