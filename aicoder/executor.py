"""
executor.py — Shared Agent Execution Engine.

Used by both CLI (agent.py) and GUI (chat_widget.py).
Eliminates code duplication for: tool parsing, tool execution,
message management, destructive-command guards, audit logging.
"""
from __future__ import annotations
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import ClientError, TriForceClient
from .config import load_session
from .docs_context import read_agents_md
from .session_state import get_state
from . import audit

MAX_ITERATIONS = 12
MAX_CONTEXT_MESSAGES = 24

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_TERMUX = bool(os.environ.get("TERMUX_VERSION") or os.path.exists("/data/data/com.termux"))
OS_NAME = "Android/Termux" if IS_TERMUX else platform.system()

TOOL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)

# Destructive patterns for local_exec approval
DESTRUCTIVE_PATTERNS = [
    "rm -rf", "rm -r /", "dd if=", "mkfs", "> /dev/",
    "format c:", "del /f /s /q", "remove-item -recurse -force",
    ":(){ :|:& };:", "chmod -r 777 /", "truncate -s 0",
    "wipefs", "shred", "> /etc/", "mv / ",
]

# OS-specific instructions
if IS_TERMUX:
    OS_INSTRUCTIONS = """- local_exec uses sh/bash in Termux (Android).
- No sudo. Use 'pkg install <pkg>' for packages.
- Home: /data/data/com.termux/files/home
- Prefer: pkg, pip, git, curl, python3, termux-* commands."""
elif IS_WINDOWS:
    OS_INSTRUCTIONS = """- local_exec uses PowerShell. Use PowerShell commands.
  NO sudo. NO bash syntax. NO apt/systemctl/cat/sed."""
else:
    OS_INSTRUCTIONS = """- local_exec uses bash. Use standard Linux/macOS commands.
  Use sudo for privileged operations."""

# MCP-Tools whitelist — READONLY only, run on backend (never write to server)
# file_ops/git_ops/git/memory_store removed: clients must not write to server
AGENT_TOOLS = {
    "safe_probe", "system_info", "process_control", "health",
    "code_read", "code_search", "code_tree",
    "dev_analyze", "dev_debug", "dev_lint", "dev_refactor", "dev_summarize",
    "dev_links",
    "service_status", "container_status", "log_viewer", "network_info",
    "web_search", "browser_search", "search", "fetch", "crawl",
    "memory_search",
    "current_time", "hive_recall", "hive_stats",
}

LOCAL_EXEC_SCHEMA = {
    "name": "local_exec",
    "description": (
        "Execute a command LOCALLY on the user's machine (subprocess, not MCP). "
        "Use for ALL system changes: file edits, installs, git, etc. "
        + ("Windows: use PowerShell syntax. " if IS_WINDOWS else "Linux: use bash syntax. ")
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": (
                "PowerShell command" if IS_WINDOWS else "bash command")},
            "cwd": {"type": "string", "description": "Working directory (optional)"},
            **({"sudo": {"type": "boolean", "description": "Run with sudo"}}
               if not IS_WINDOWS else {}),
        },
        "required": ["command"]
    }
}

SYSTEM_TEMPLATE = """\
You are ai-coder — autonomous coding and DevOps agent on AILinux/TriForce (api.ailinux.me).
{agents_md}

## INIT — Before every task:
1. current_time → check date, compare with training cutoff
2. memory_search → known solutions/context?
3. If time-sensitive or version question: search FIRST, then answer. Never guess.

## Tool Model:
- local_exec: Runs LOCALLY on user machine (file edits, installs, git, package management)
- MCP tools: Run on REMOTE backend (Hetzner). Use for code reading, search, memory, system info.

## When to use which:
- READ/ANALYZE: code_read, code_search, code_tree, dev_analyze, dev_debug, dev_lint
- STATUS: health, safe_probe, log_viewer, service_status
- SEARCH: memory_search (first!) → search → fetch → crawl
- CHANGE: local_exec (local files) or code_edit/shell via MCP (backend files)
- STUCK >2 rounds: Stop guessing. Use memory_search, then search, then ask user.

## Rules:
- Read before write. Diagnose before patch.
- Smallest effective change first.
- After change: dev_lint + health check.
- When done: start reply with DONE:

## OS: {os_name}
{os_instructions}

## Tool Call Format (one per response):
<tool_call>
{{"name": "tool_name", "arguments": {{...}}}}
</tool_call>

## Tools
{tools}

## Workspace
{workspace}
"""

# Fallback tool definitions if tools/list fails
FALLBACK_TOOLS: list[dict] = [
    {"name": "safe_probe",  "description": "Read-only system diagnostics", "inputSchema": {"type":"object","properties":{"action":{"type":"string"},"probe":{"type":"string"}},"required":["action"]}},
    {"name": "code_read",   "description": "Read source file", "inputSchema": {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
    {"name": "code_tree",   "description": "Show directory structure", "inputSchema": {"type":"object","properties":{"path":{"type":"string"}}}},
    {"name": "git_ops",     "description": "Git operations", "inputSchema": {"type":"object","properties":{"action":{"type":"string"}},"required":["action"]}},
    {"name": "dev_analyze",  "description": "Analyze code quality", "inputSchema": {"type":"object","properties":{"path":{"type":"string"}}}},
    {"name": "web_search",   "description": "Search the web", "inputSchema": {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name": "file_ops",     "description": "File operations", "inputSchema": {"type":"object","properties":{"action":{"type":"string"},"path":{"type":"string"}},"required":["action","path"]}},
    {"name": "health",       "description": "Backend health check", "inputSchema": {"type":"object","properties":{}}},
    {"name": "shell",        "description": "Execute shell command", "inputSchema": {"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}},
]


def is_destructive(cmd: str) -> bool:
    """Check if a command matches known destructive patterns."""
    cmd_lower = cmd.lower().strip()
    return any(pat.lower() in cmd_lower for pat in DESTRUCTIVE_PATTERNS)


def load_tools(client: TriForceClient) -> list[dict]:
    """Load MCP tool schemas + local_exec pseudo-tool."""
    mcp_tools = []
    err_msg = ""
    try:
        short_client = TriForceClient(client.base_url, token=client.token, timeout=20)
        r = short_client._request("POST", "/v1/mcp",
            {"jsonrpc":"2.0","method":"tools/list","params":{},"id":1},
            require_auth=True, _label="tools/list")
        mcp_tools = [t for t in r.get("result",{}).get("tools",[]) if t["name"] in AGENT_TOOLS]
    except Exception as e:
        err_msg = str(e)
    if not mcp_tools:
        hint = f" ({err_msg[:80]})" if err_msg else ""
        print(f"  \033[33m⚠ tools/list fehlgeschlagen{hint} — Fallback\033[0m", file=sys.stderr)
        mcp_tools = FALLBACK_TOOLS
    return [LOCAL_EXEC_SCHEMA] + mcp_tools


def build_tool_desc(tools: list[dict]) -> str:
    """Build tool description string for system prompt."""
    out = []
    for t in sorted(tools, key=lambda x: x["name"]):
        props = list(t.get("inputSchema",{}).get("properties",{}).keys())
        req = t.get("inputSchema",{}).get("required",[])
        sig = ", ".join(f"{p}*" if p in req else p for p in props)
        desc = (t.get("description","") or "")[:100].replace("\n"," ")
        out.append(f"- {t['name']}({sig}): {desc}")
    return "\n".join(out)


def build_system_prompt(tools: list[dict], workspace_root: Optional[str] = None) -> str:
    """Build the system prompt with tools, workspace, and OS info."""
    ws_path = Path(workspace_root or ".").resolve()
    try:
        entries = sorted(
            e.name for e in ws_path.iterdir()
            if e.name not in {".git",".venv","__pycache__","node_modules"}
        )[:20]
        ws_str = f"path: {ws_path}\nfiles: {', '.join(entries)}"
    except Exception:
        ws_str = f"path: {ws_path}"
    try:
        r = subprocess.run(
            ["git","branch","--show-current"], cwd=str(ws_path),
            capture_output=True, text=True, timeout=3
        )
        branch = r.stdout.strip()
        if branch:
            ws_str += f"\ngit: {branch}"
    except Exception:
        pass

    agents_md = read_agents_md(str(ws_path)) or ""
    agents_short = agents_md[:1500] if agents_md else ""

    tool_str = build_tool_desc(tools)[:4000]
    return SYSTEM_TEMPLATE.format(
        agents_md=("## AGENTS.md\n" + agents_short) if agents_short else "",
        tools=tool_str,
        workspace=ws_str[:300],
        os_name=OS_NAME,
        os_instructions=OS_INSTRUCTIONS,
    )


def parse_tool_calls(text: str) -> list[dict]:
    """Extract tool calls from LLM response text. Supports JSON and XML format."""
    calls = []
    for m in TOOL_RE.finditer(text):
        raw = m.group(1).strip()
        # Format 1: JSON {"name": ..., "arguments": {...}}
        try:
            c = json.loads(raw)
            if "name" in c:
                calls.append(c)
                continue
        except Exception:
            pass
        # Format 2: XML <n>tool_name</n><arguments><key>val</key></arguments>
        try:
            import re as _re
            name_m = _re.search(r"<n>(.*?)</n>", raw, _re.DOTALL)
            if name_m:
                name = name_m.group(1).strip()
                args = {}
                args_m = _re.search(r"<arguments>(.*?)</arguments>", raw, _re.DOTALL)
                if args_m:
                    for km in _re.finditer(r"<(\w+)>(.*?)</\1>", args_m.group(1), _re.DOTALL):
                        args[km.group(1)] = km.group(2).strip()
                calls.append({"name": name, "arguments": args})
        except Exception:
            pass
    return calls


def strip_tool_calls(text: str) -> str:
    """Remove tool call blocks from text."""
    return TOOL_RE.sub("", text).strip()


def trim_messages(msgs: list[dict]) -> list[dict]:
    """Keep system prompt + last MAX_CONTEXT_MESSAGES conversation messages."""
    if len(msgs) <= 1 + MAX_CONTEXT_MESSAGES:
        return msgs
    return [msgs[0], msgs[1]] + msgs[-(MAX_CONTEXT_MESSAGES - 1):]


def run_local_exec(args: dict) -> Tuple[str, bool]:
    """Execute a local command via subprocess. Returns (output, is_error)."""
    cmd = args.get("command", "")
    cwd = args.get("cwd") or None

    if IS_WINDOWS:
        run_args = ["powershell", "-NoProfile", "-Command", cmd]
        try:
            r = subprocess.run(run_args, cwd=cwd, capture_output=True, text=True, timeout=60)
            out = (r.stdout or "") + (r.stderr or "")
            return (out[:4000] or "(no output)"), r.returncode != 0
        except Exception as e:
            return f"local_exec error: {e}", True
    else:
        use_sudo = args.get("sudo", False)
        if use_sudo and not cmd.strip().startswith("sudo "):
            cmd = "sudo " + cmd
        try:
            r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=60)
            out = (r.stdout or "") + (r.stderr or "")
            return (out[:4000] or "(no output)"), r.returncode != 0
        except Exception as e:
            return f"local_exec error: {e}", True


def run_mcp_tool(client: TriForceClient, name: str, args: dict) -> Tuple[str, bool]:
    """Execute an MCP tool on the backend. Returns (output, is_error)."""
    try:
        r = client.mcp_call(name, args)
        text = r.get("result",{}).get("content",[{}])[0].get("text","")
        is_error = r.get("result",{}).get("isError", False)
        if is_error or text.startswith('{"error"'):
            return text[:4000], True
        return text[:4000] + ("…" if len(text) > 4000 else ""), False
    except ClientError as e:
        return f"TOOL FAILED: {e}", True


def run_tool(
    client: TriForceClient,
    name: str,
    args: dict,
    approval_fn: Optional[Callable[[str, dict], bool]] = None,
    model: str = "",
    iteration: int = 0,
) -> Tuple[str, bool]:
    """
    Execute a tool with audit logging and optional approval.
    
    approval_fn(tool_name, args) -> bool: Called for local_exec commands.
      If it returns False, execution is aborted.
      If None, all commands run without approval (legacy behavior).
    """
    # Approval check for local_exec
    if name == "local_exec":
        cmd = args.get("command", "")
        if approval_fn is not None:
            if not approval_fn(name, args):
                return "local_exec: aborted by user", True
        elif is_destructive(cmd):
            # No approval_fn set but command is destructive — block it
            import sys as _sys
            print(f"\033[31m⚠ BLOCKED (destructive, no approval_fn): {cmd[:120]}\033[0m",
                  file=_sys.stderr)
            return f"local_exec: blocked — destructive command requires explicit approval: {cmd[:120]}", True

    t_start = time.time()

    if name == "local_exec":
        result, is_error = run_local_exec(args)
    else:
        result, is_error = run_mcp_tool(client, name, args)

    duration = time.time() - t_start

    # Audit log — always, for every tool call
    audit.log_tool(
        tool_name=name,
        arguments=args,
        result=result,
        duration_s=duration,
        is_error=is_error,
        model=model,
        iteration=iteration,
    )

    return result, is_error
