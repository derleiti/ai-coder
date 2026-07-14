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
    MAX_ITERATIONS,
    is_destructive, load_tools, build_system_prompt,
    normalize_tool_calls, parse_tool_calls, strip_tool_calls, trim_messages, run_tool,
    is_simple_chat_message,
    # Re-export for backwards compat (GUI imports these)
    AGENT_TOOLS, LOCAL_EXEC_SCHEMA, SYSTEM_TEMPLATE as SYSTEM,
    FALLBACK_TOOLS as _FALLBACK_TOOLS, OS_NAME, OS_INSTRUCTIONS,
)
from .history import record as history_record
from .session_state import get_state
from .ui import (
    AgentSpinner, C,
    print_header, print_task, print_thought,
    print_tool_call, print_tool_result, print_final,
    print_error, print_max_iter,
)


def _cli_approval(tool_name: str, args: dict) -> bool:
    """CLI approval: ask user for destructive commands, auto-approve safe ones."""
    if tool_name != "local_exec":
        return True
    cmd = args.get("command", "")
    if not is_destructive(cmd):
        return True  # Safe commands run without asking
    print("\n⚠️  DESTRUCTIVE COMMAND DETECTED:", file=sys.stderr)
    print(f"   {cmd}", file=sys.stderr)
    try:
        confirm = input("Execute? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        confirm = "n"
    return confirm == "y"


def run_agent(
    initial_prompt: str,
    model: Optional[str],
    fallback_model: Optional[str],
    verbose: bool = False,
) -> int:
    session = load_session()
    state = get_state()
    request_timeout = int(state.get("request_timeout", 30))
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

    effective_model = model
    effective_fallback = fallback_model
    fast_route = quick_chat and fallback_model and fallback_model != model
    if fast_route:
        effective_model = fallback_model
        effective_fallback = None

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

    # Message array for multi-turn context
    messages: list[dict] = [{"role": "system", "content": system}]
    current_input = initial_prompt
    full_response = ""
    model_used = effective_model or "?"
    total_latency = 0
    fallback_used = False

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
        tool_results = []
        for call in calls:
            tname = call.get("name", "?")
            targs = call.get("arguments", {})
            print_tool_call(tname, targs, i)

            with AgentSpinner(tname, tool=tname) as sp:
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

        messages.append({"role": "assistant", "content": response})
        current_input = "\n\n".join(tool_results)

        if response.strip().upper().startswith("DONE:"):
            print_final(response, model_used, total_latency, i+1, fallback_used)
            break
    else:
        print_max_iter(MAX_ITERATIONS)

    try:
        history_record(kind="ask", prompt=initial_prompt,
                       response=full_response, model=model_used,
                       latency_ms=total_latency)
    except Exception:
        pass
    return 0
