from __future__ import annotations
"""
agent.py — CLI Agent runner. Uses shared executor for tool execution.
"""
import sys
import time
from pathlib import Path
from typing import Optional

from .client import ClientError, TriForceClient
from .config import load_session
from .executor import (
    MAX_ITERATIONS,
    is_destructive, load_tools, build_system_prompt,
    parse_tool_calls, strip_tool_calls, trim_messages, run_tool,
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
    client = TriForceClient(session.base_url, token=session.token, timeout=120)
    ws_path = Path(state.get("workspace_root") or ".").resolve()

    # Load tools
    with AgentSpinner("loading tools", color=C.DIM):
        tools = load_tools(client)

    system = build_system_prompt(tools, str(ws_path))

    # Header
    print_header(
        model=model or "backend-default",
        fallback=fallback_model or "",
        tools=len(tools),
        workspace=ws_path.name,
    )
    print_task(initial_prompt)

    # Message array for multi-turn context
    messages: list[dict] = [{"role": "system", "content": system}]
    current_input = initial_prompt
    full_response = ""
    model_used = model or "?"
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
                    model=model,
                    fallback_model=fallback_model,
                    temperature=0.3,
                    max_tokens=4096,
                )
            except (ClientError, RuntimeError) as e:
                print_error(str(e))
                return 1
            llm_ms = int((time.time() - t0) * 1000)

        response = result.get("response", "").strip()
        model_used = result.get("model", model or "?")
        lat = result.get("latency_ms") or llm_ms
        total_latency += lat
        if result.get("fallback_used"):
            fallback_used = True
        full_response = response

        calls = parse_tool_calls(response)
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

        if "DONE:" in response[:200].upper():
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
