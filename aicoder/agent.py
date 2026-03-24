from __future__ import annotations
"""
agent.py — Autonomer Terminal-Agent (opencode-Style UI).
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .client import ClientError, TriForceClient
from .config import load_session
from .docs_context import read_agents_md
from .history import record as history_record
from .session_state import get_state
from .ui import (
    AgentSpinner, C,
    dim, bold, cyan, green, yellow, red, magenta,
    panel, print_header, print_task, print_thought,
    print_tool_call, print_tool_result, print_final,
    print_error, print_interrupted, print_max_iter,
)

MAX_ITERATIONS = 12
TOOL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)

AGENT_TOOLS = {
    "binary_exec", "shell", "safe_probe", "system_info", "process_control",
    "code_read", "code_search", "code_tree", "code_edit", "code_patch",
    "file_ops", "git_ops", "git",
    "dev_analyze", "dev_debug", "dev_lint", "dev_refactor", "dev_summarize",
    "service_control", "service_status", "container_status",
    "log_viewer", "network_info",
    "web_search", "fetch", "search",
    "memory_search", "memory_store",
    "health",
}

SYSTEM = """\
You are ai-coder, an autonomous terminal coding and DevOps agent on AILinux/TriForce.
{agents_md}

## Rules
- Think step by step. Read before writing. Confirm destructive ops.
- Use tools to gather info first, then act.
- When task is done, start your final reply with: DONE:

## IMPORTANT — Tool Execution Context:
ALL tools run on the REMOTE TriForce backend server (Hetzner, 64GB RAM, hostname=ailinux).
NOT on the user's local machine. safe_probe/binary_exec/shell show BACKEND server stats.
If user asks for LOCAL RAM/disk/CPU: clarify the distinction and show backend stats.

## Tool Call Format (EXACT — prefer one tool per response):
<tool_call>
{{"name": "tool_name", "arguments": {{...}}}}
</tool_call>

After each tool result, continue. When done: reply starting with DONE:

## Tools
{tools}

## Workspace
{workspace}
"""


# Minimale Tool-Definitionen als Fallback wenn tools/list scheitert
_FALLBACK_TOOLS: list[dict] = [
    {"name": "binary_exec",  "description": "Run system programs (uptime, df, git, docker, ps, systemctl...)", "inputSchema": {"type":"object","properties":{"action":{"type":"string"},"program":{"type":"string"},"arguments":{"type":"array","items":{"type":"string"}}},"required":["action"]}},
    {"name": "shell",        "description": "Execute shell command string", "inputSchema": {"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}},
    {"name": "safe_probe",   "description": "Read-only system diagnostics (overview, run, service_status, journal)", "inputSchema": {"type":"object","properties":{"action":{"type":"string"},"probe":{"type":"string"}},"required":["action"]}},
    {"name": "code_read",    "description": "Read source file", "inputSchema": {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
    {"name": "code_tree",    "description": "Show directory structure", "inputSchema": {"type":"object","properties":{"path":{"type":"string"}}}},
    {"name": "git_ops",      "description": "Git operations: log, status, diff, branch", "inputSchema": {"type":"object","properties":{"action":{"type":"string"},"lines":{"type":"integer"}},"required":["action"]}},
    {"name": "dev_analyze",  "description": "Analyze code quality", "inputSchema": {"type":"object","properties":{"path":{"type":"string"}}}},
    {"name": "web_search",   "description": "Search the web", "inputSchema": {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name": "file_ops",     "description": "File operations: read, write, list, find", "inputSchema": {"type":"object","properties":{"action":{"type":"string"},"path":{"type":"string"}},"required":["action","path"]}},
    {"name": "health",       "description": "Backend health check", "inputSchema": {"type":"object","properties":{}}},
]

def _get_tools(client: TriForceClient) -> list[dict]:
    # Kurzer Timeout (8s) damit tools/list schnell scheitert statt zu hängen
    from .config import load_session
    from .client import TriForceClient as TFC
    session = load_session()
    short_client = TFC(session.base_url, token=session.token, timeout=8)
    try:
        r = short_client._request("POST", "/v1/mcp",
            {"jsonrpc":"2.0","method":"tools/list","params":{},"id":1},
            require_auth=True, _label="tools/list")
        found = [t for t in r.get("result",{}).get("tools",[]) if t["name"] in AGENT_TOOLS]
        if found:
            return found
    except Exception:
        pass
    # Fallback: hardcoded minimal tool set — Agent kann trotzdem arbeiten
    import sys
    print("  \033[33m⚠ tools/list timeout — using built-in tool set\033[0m", file=sys.stderr)
    return _FALLBACK_TOOLS


def _tool_desc(tools: list[dict]) -> str:
    out = []
    for t in sorted(tools, key=lambda x: x["name"]):
        props = list(t.get("inputSchema",{}).get("properties",{}).keys())
        req   = t.get("inputSchema",{}).get("required",[])
        sig   = ", ".join(f"{p}*" if p in req else p for p in props)
        desc  = (t.get("description","") or "")[:100].replace("\n"," ")
        out.append(f"- {t['name']}({sig}): {desc}")
    return "\n".join(out)


def _run_tool(client: TriForceClient, name: str, args: dict) -> tuple[str, bool]:
    """Returns (result_text, is_error)."""
    try:
        r = client.mcp_call(name, args)
        text = r.get("result",{}).get("content",[{}])[0].get("text","")
        is_error = r.get("result",{}).get("isError", False)
        if is_error or text.startswith('{"error"'):
            return text[:4000], True
        return text[:4000] + ("…" if len(text)>4000 else ""), False
    except ClientError as e:
        return f"TOOL FAILED: {e}", True


def _parse_calls(text: str) -> list[dict]:
    calls = []
    for m in TOOL_RE.finditer(text):
        try:
            c = json.loads(m.group(1).strip())
            if "name" in c:
                calls.append(c)
        except Exception:
            pass
    return calls


def _strip_calls(text: str) -> str:
    return TOOL_RE.sub("", text).strip()


def run_agent(
    initial_prompt: str,
    model: Optional[str],
    fallback_model: Optional[str],
    verbose: bool = False,
) -> int:
    session = load_session()
    state   = get_state()
    client  = TriForceClient(session.base_url, token=session.token, timeout=120)

    ws_path   = Path(state.get("workspace_root") or ".").resolve()
    agents_md = read_agents_md(str(ws_path)) or ""

    # Workspace info
    try:
        entries = sorted(
            e.name for e in ws_path.iterdir()
            if e.name not in {".git",".venv","__pycache__","node_modules"}
        )[:20]
        ws_str = f"path: {ws_path}\nfiles: {', '.join(entries)}"
    except Exception:
        ws_str = f"path: {ws_path}"
    try:
        branch = subprocess.check_output(
            ["git","branch","--show-current"], cwd=str(ws_path),
            capture_output=True, text=True, timeout=3
        ).stdout.strip()
        if branch:
            ws_str += f"\ngit: {branch}"
    except Exception:
        pass

    # Lade Tools
    with AgentSpinner("loading tools", color=C.DIM):
        tools = _get_tools(client)

    tool_str = _tool_desc(tools)[:4000]  # Cap: verhindert zu langen system_prompt
    agents_short = agents_md[:1500] if agents_md else ""
    system = SYSTEM.format(
        agents_md=("## AGENTS.md\n" + agents_short) if agents_short else "",
        tools=tool_str,
        workspace=ws_str[:300],
    )

    # Header
    print_header(
        model=model or "backend-default",
        fallback=fallback_model or "",
        tools=len(tools),
        workspace=ws_path.name,
    )
    print_task(initial_prompt)

    history: list[dict] = []
    current_input = initial_prompt
    full_response  = ""
    model_used     = model or "?"
    total_latency  = 0
    fallback_used  = False

    for i in range(MAX_ITERATIONS):
        # Kontext
        if history:
            ctx = "\n\n".join(
                f"User: {h['user']}\nAssistant: {h['assistant'][:600]}"
                for h in history[-3:]
            )
            msg = ctx + f"\n\nUser: {current_input}"
        else:
            msg = current_input

        label = "thinking" if i == 0 else f"step {i+1}"

        with AgentSpinner(label, color=C.CYAN):
            t0 = time.time()
            try:
                result = client.chat(
                    message=msg,
                    model=model,
                    fallback_model=fallback_model,
                    system_prompt=system,
                    temperature=0.3,
                    max_tokens=4096,
                )
            except (ClientError, RuntimeError) as e:
                print_error(str(e))
                return 1
            llm_ms = int((time.time() - t0) * 1000)

        response  = result.get("response","").strip()
        model_used = result.get("model", model or "?")
        lat        = result.get("latency_ms") or llm_ms
        total_latency += lat
        if result.get("fallback_used"):
            fallback_used = True
        full_response = response

        calls   = _parse_calls(response)
        visible = _strip_calls(response)

        # Gedanken anzeigen (gedimmt, nur wenn Tool folgt)
        if visible and calls:
            print_thought(visible)

        if not calls:
            # Finale Antwort
            print_final(
                response=response,
                model=model_used,
                latency_ms=total_latency,
                total_iters=i + 1,
                fallback_used=fallback_used,
            )
            break

        # Tool-Loop
        tool_results = []
        for call in calls:
            tname = call.get("name","?")
            targs = call.get("arguments",{})

            print_tool_call(tname, targs, i)

            with AgentSpinner(tname, tool=tname) as sp:
                t_start = time.time()
                tr, is_err = _run_tool(client, tname, targs)
                t_elapsed = time.time() - t_start

            print_tool_result(tname, tr, t_elapsed, error=is_err)
            tool_results.append(f"Tool {tname} result:\n{tr}")

        history.append({"user": current_input, "assistant": response})
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
