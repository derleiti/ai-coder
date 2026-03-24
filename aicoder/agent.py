from __future__ import annotations
"""
agent.py — Autonomer Terminal-Agent für ai-coder (Claude Code / Codex Style).

Loop:
  User gibt Aufgabe → LLM denkt → ruft Tools via <tool_call> → sieht Ergebnis → weiter
  Bis LLM "DONE:" schreibt oder kein Tool mehr aufruft.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .client import ClientError, TriForceClient
from .config import load_session
from .docs_context import read_agents_md
from .history import record as history_record
from .session_state import get_state
from .status import Spinner

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

## Tool Call Format
Use this EXACT format to call a tool (one per message if possible):

<tool_call>
{{"name": "tool_name", "arguments": {{...}}}}
</tool_call>

After each tool result, continue reasoning. When finished: reply normally starting with DONE:

## Tools Available
{tools}

## Workspace
{workspace}
"""


def _get_tools(client: TriForceClient) -> list[dict]:
    try:
        r = client._request("POST", "/v1/mcp",
            {"jsonrpc":"2.0","method":"tools/list","params":{},"id":1},
            require_auth=True, _label="tools/list")
        all_tools = r.get("result",{}).get("tools",[])
        return [t for t in all_tools if t["name"] in AGENT_TOOLS]
    except Exception:
        return []


def _tool_desc(tools: list[dict]) -> str:
    out = []
    for t in sorted(tools, key=lambda x: x["name"]):
        props = list(t.get("inputSchema",{}).get("properties",{}).keys())
        req   = t.get("inputSchema",{}).get("required",[])
        sig   = ", ".join(f"{p}*" if p in req else p for p in props)
        desc  = (t.get("description","") or "")[:100].replace("\n"," ")
        out.append(f"- {t['name']}({sig}): {desc}")
    return "\n".join(out)


def _run_tool(client: TriForceClient, name: str, args: dict) -> str:
    try:
        r = client.mcp_call(name, args)
        text = r.get("result",{}).get("content",[{}])[0].get("text","")
        if r.get("result",{}).get("isError"):
            return f"[ERROR] {text}"
        return text[:4000] + ("…" if len(text)>4000 else "")
    except ClientError as e:
        return f"[TOOL FAILED] {e}"


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

    ws_path = Path(state.get("workspace_root") or ".").resolve()
    agents_md = read_agents_md(str(ws_path)) or ""

    # Workspace snapshot
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

    print("Loading tools...", end="\r", file=sys.stderr)
    tools = _get_tools(client)
    sys.stderr.write("              \r")

    system = SYSTEM.format(
        agents_md=("## AGENTS.md\n" + agents_md) if agents_md else "",
        tools=_tool_desc(tools),
        workspace=ws_str,
    )

    print(f"\n\033[1m[agent]\033[0m model={model or 'default'}  "
          f"tools={len(tools)}  ws={ws_path.name}")
    print("─"*60)

    history: list[dict] = []
    current_input = initial_prompt
    full_response  = ""

    for i in range(MAX_ITERATIONS):
        # Kontext aufbauen
        if history:
            ctx = "\n\n".join(
                f"User: {h['user']}\nAssistant: {h['assistant'][:600]}"
                for h in history[-3:]
            )
            msg = ctx + f"\n\nUser: {current_input}"
        else:
            msg = current_input

        label = "thinking..." if i == 0 else "continuing..."
        with Spinner(label):
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
                print(f"\n[agent error] {e}", file=sys.stderr)
                return 1

        response = result.get("response","").strip()
        model_used = result.get("model", model or "?")
        full_response = response

        # Tool calls parsen
        calls = _parse_calls(response)
        visible = _strip_calls(response)

        # Gedanken/Text zeigen (vor tool calls)
        if visible and (calls or verbose):
            print(f"\n\033[2m{visible}\033[0m")

        if not calls:
            # Kein Tool → finale Antwort
            print()
            print(response)
            print(f"\n\033[2m[{model_used} · {result.get('latency_ms','?')}ms]\033[0m", file=sys.stderr)
            break

        # Tool calls ausführen
        tool_results = []
        for call in calls:
            tname = call.get("name","?")
            targs = call.get("arguments",{})
            print(f"\n\033[33m▶ {tname}\033[0m  {json.dumps(targs, ensure_ascii=False)[:120]}")
            with Spinner(f"  running {tname}..."):
                tr = _run_tool(client, tname, targs)
            # Ergebnis zeigen (erste 40 Zeilen)
            tr_lines = tr.splitlines()
            preview = "\n".join(tr_lines[:40])
            if len(tr_lines) > 40:
                preview += f"\n\033[2m... +{len(tr_lines)-40} more lines\033[0m"
            print(f"\033[32m{preview}\033[0m")
            tool_results.append(f"Tool {tname} result:\n{tr}")

        # Tool-Ergebnis als nächsten Input
        history.append({"user": current_input, "assistant": response})
        current_input = "\n\n".join(tool_results)

        # DONE-Check
        if response.upper().startswith("DONE:") or "DONE:" in response[:200]:
            break
    else:
        print(f"\n[agent] Maximale Iterationen ({MAX_ITERATIONS}) erreicht.", file=sys.stderr)

    # History speichern
    try:
        history_record(kind="ask", prompt=initial_prompt,
                       response=full_response, model=model_used)
    except Exception:
        pass
    return 0
