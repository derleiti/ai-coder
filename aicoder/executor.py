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

MAX_ITERATIONS = int(os.environ.get("AICODER_MAX_ITERATIONS", "30"))
MAX_CONTEXT_MESSAGES = int(os.environ.get("AICODER_MAX_CONTEXT", "50"))

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_TERMUX = bool(os.environ.get("TERMUX_VERSION") or os.path.exists("/data/data/com.termux"))
OS_NAME = "Android/Termux" if IS_TERMUX else platform.system()

TOOL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)

# Destructive patterns for local_exec approval
DESTRUCTIVE_PATTERNS = [
    # Linux/Mac destructive
    "rm -rf", "rm -r /", "rm -f /",
    "dd if=", "mkfs", "> /dev/",
    "wipefs", "shred",
    "truncate -s 0",
    "chmod -r 777 /", "chmod 777 /",
    "> /etc/", "> /boot/", "> /usr/", "> /bin/",
    "mv / ",
    ":(){ :|:& };:",       # fork bomb
    # Pipe-to-shell (supply chain / remote exec)
    "| bash", "| sh", "| zsh", "| python",
    "|bash", "|sh", "|zsh",
    "curl | ", "wget | ",
    # Windows destructive
    "format c:", "format d:",
    "del /f /s /q",
    "remove-item -recurse -force",
    "rd /s /q c:",
    # Registry wipes
    "reg delete hklm", "reg delete hkcu",
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
    # MCP v4 Tool Names — READ-ONLY (2026-04-12)
    # Sicherheitsmodell: MCP = nur lesen/suchen/status
    #                    local_exec = alle Änderungen lokal am Client
    #
    # ── Code lesen/analysieren (READ-ONLY) ──
    "code_read", "code_search", "code_tree",
    "debug",
    # ── System Status (READ-ONLY) ──
    "health", "status", "init",
    "logs", "logs_errors", "logs_stats",
    # ── Search & Web (READ-ONLY) ──
    "search", "crawl",
    # ── Memory (READ + WRITE — eigener Namespace) ──
    "memory_search", "memory_store", "memory_clear",
    # ── Models & Chat (READ-ONLY) ──
    "models", "specialist",
    # ── Agents (READ-ONLY Status) ──
    "agents",
    # ── Ollama (READ-ONLY) ──
    "ollama_list", "ollama_status",
    # ── Mesh/Remote (READ-ONLY Status) ──
    "mesh_status", "mesh_agents",
    "remote_hosts", "remote_status",
    # ── Config (READ-ONLY) ──
    "config",
    "vault_keys", "vault_status",
    # ── Research (READ-ONLY) ──
    "gemini_research",
    "evolve_history",
    "prompts",
    #
    # NICHT erlaubt für Clients (nur via Admin-Console):
    # code_edit, code_patch, shell, restart, bootstrap,
    # config_set, prompt_set, vault_add, agent_call,
    # agent_start, agent_stop, agent_broadcast,
    # remote_task, mesh_task, ollama_run, ollama_pull,
    # ollama_delete, gemini_exec, gemini_coordinate, evolve
}

# ══════════════════════════════════════════════════════════════════════
# LOCAL Tool Schemas — alle laufen lokal am Client via subprocess
# Server wählt das Tool, Client führt aus. Kein MCP-Aufruf.
# ══════════════════════════════════════════════════════════════════════

LOCAL_EXEC_SCHEMA = {
    "name": "local_exec",
    "description": (
        "Execute a shell command LOCALLY on the user's machine. "
        "Use for system tasks, package management, services, networking. "
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

LOCAL_FILE_READ_SCHEMA = {
    "name": "file_read",
    "description": "Read a LOCAL file. Use cat, head, tail, or bat. For large files use head -n or sed.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "cat/head/tail/sed command to read file"},
            "cwd": {"type": "string", "description": "Working directory (optional)"},
        },
        "required": ["command"]
    }
}

LOCAL_FILE_EDIT_SCHEMA = {
    "name": "file_edit",
    "description": "Edit a LOCAL file. Use sed, awk, or python/perl one-liners. For new files use tee or cat >.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "sed/awk/tee command to edit file"},
            "cwd": {"type": "string", "description": "Working directory (optional)"},
        },
        "required": ["command"]
    }
}

LOCAL_FILE_TREE_SCHEMA = {
    "name": "file_tree",
    "description": "Show LOCAL directory structure. Use tree, ls -la, or find.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "tree/ls/find command"},
            "cwd": {"type": "string", "description": "Working directory (optional)"},
        },
        "required": ["command"]
    }
}

LOCAL_CODE_SEARCH_SCHEMA = {
    "name": "code_grep",
    "description": "Search LOCAL codebase. Use grep -rn, rg (ripgrep), or ag (silver searcher).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "grep/rg/ag command"},
            "cwd": {"type": "string", "description": "Working directory (optional)"},
        },
        "required": ["command"]
    }
}

LOCAL_GIT_SCHEMA = {
    "name": "git",
    "description": "Git operations on LOCAL repo. Commit, push, pull, diff, log, branch, stash.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "git command (e.g. git diff, git commit -m ...)"},
            "cwd": {"type": "string", "description": "Repository directory (optional)"},
        },
        "required": ["command"]
    }
}

LOCAL_LINT_SCHEMA = {
    "name": "lint",
    "description": "Lint/analyze LOCAL code. Use python -m py_compile, pylint, flake8, shellcheck, eslint.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Linter command"},
            "cwd": {"type": "string", "description": "Working directory (optional)"},
        },
        "required": ["command"]
    }
}

LOCAL_TEST_SCHEMA = {
    "name": "test",
    "description": "Run LOCAL tests. Use pytest, python -m unittest, npm test, make test.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Test command"},
            "cwd": {"type": "string", "description": "Working directory (optional)"},
        },
        "required": ["command"]
    }
}

LOCAL_DEVOPS_SCHEMA = {
    "name": "devops",
    "description": "LOCAL DevOps: docker, systemctl, journalctl, nginx, apache, pip, npm, apt.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "DevOps command"},
            "cwd": {"type": "string", "description": "Working directory (optional)"},
            "sudo": {"type": "boolean", "description": "Run with sudo"},
        },
        "required": ["command"]
    }
}

LOCAL_CLIPBOARD_READ_SCHEMA = {
    "name": "clipboard_read",
    "description": "Read current clipboard content from user's desktop.",
    "inputSchema": {
        "type": "object",
        "properties": {},
    }
}

LOCAL_CLIPBOARD_WRITE_SCHEMA = {
    "name": "clipboard_write",
    "description": "Write/copy text to user's clipboard.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to copy to clipboard"},
        },
        "required": ["text"]
    }
}

LOCAL_WEB_SEARCH_SCHEMA = {
    "name": "web_search_local",
    "description": "Search the web locally via DuckDuckGo (no API key needed).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"]
    }
}

LOCAL_WEB_FETCH_SCHEMA = {
    "name": "web_fetch_local",
    "description": "Fetch and extract text from a URL locally.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
        },
        "required": ["url"]
    }
}

# All local tool schemas — each maps to subprocess execution
LOCAL_TOOL_SCHEMAS = [
    LOCAL_EXEC_SCHEMA,
    LOCAL_FILE_READ_SCHEMA,
    LOCAL_FILE_EDIT_SCHEMA,
    LOCAL_FILE_TREE_SCHEMA,
    LOCAL_CODE_SEARCH_SCHEMA,
    LOCAL_GIT_SCHEMA,
    LOCAL_LINT_SCHEMA,
    LOCAL_TEST_SCHEMA,
    LOCAL_DEVOPS_SCHEMA,
    LOCAL_CLIPBOARD_READ_SCHEMA,
    LOCAL_CLIPBOARD_WRITE_SCHEMA,
    LOCAL_WEB_SEARCH_SCHEMA,
    LOCAL_WEB_FETCH_SCHEMA,
]

# Names of all local tools (for dispatch in execute_tool)
LOCAL_TOOL_NAMES = {t["name"] for t in LOCAL_TOOL_SCHEMAS}

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
- READ/ANALYZE: code_read, code_search, code_tree, debug (READ-ONLY, remote Backend)
- WRITE/MODIFY: ONLY local_exec! All changes happen locally on the user's machine.
- STATUS: health, status, logs, logs_errors (READ-ONLY, remote Backend)
- SEARCH: memory_search (first!) → search → crawl
- MODELS: models, specialist (info only)
- STUCK >2 rounds: Stop guessing. Use memory_search, then search, then ask user.

## SECURITY MODEL:
- MCP tools (code_read, search, health, etc.) = READ-ONLY info from remote backend
- LOCAL tools (local_exec, file_edit, file_read, git, lint, test, devops, etc.) = ALL execution on THIS machine
- All code changes, file edits, installs, git, docker — use LOCAL tools only.
- Choose the most specific local tool: file_edit for edits, git for version control,
  lint for code checks, test for testing, devops for services/containers.

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
# Fallback: READ-ONLY only -- must match AGENT_TOOLS whitelist
FALLBACK_TOOLS: list[dict] = [
    # READ-ONLY Fallback Tools — keine destruktiven Tools
    {"name": "code_read",      "description": "Read source file (remote, read-only)", "inputSchema": {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
    {"name": "code_search",    "description": "Search codebase (regex, read-only)", "inputSchema": {"type":"object","properties":{"pattern":{"type":"string"}},"required":["pattern"]}},
    {"name": "code_tree",      "description": "Show directory structure (read-only)", "inputSchema": {"type":"object","properties":{"path":{"type":"string"}}}},
    {"name": "search",         "description": "Web search", "inputSchema": {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name": "memory_search",  "description": "Search persistent memory", "inputSchema": {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name": "health",         "description": "Backend health check", "inputSchema": {"type":"object","properties":{}}},
    {"name": "status",         "description": "Full system status", "inputSchema": {"type":"object","properties":{}}},
    {"name": "logs",           "description": "Get recent system logs", "inputSchema": {"type":"object","properties":{"lines":{"type":"integer"}}}},
    {"name": "models",         "description": "List all available AI models", "inputSchema": {"type":"object","properties":{}}},
]


_OBFUSCATION_PATTERNS = [
    "base64 -d", "base64 --decode", "eval ", "eval(",
    "exec(", "exec (", "python -c", "python3 -c",
    "perl -e", "ruby -e", "bash -c", "sh -c", "zsh -c",
]

def is_destructive(cmd: str) -> bool:
    """Check if a command matches known destructive or obfuscation patterns."""
    cmd_lower = cmd.lower().strip()
    if any(pat.lower() in cmd_lower for pat in DESTRUCTIVE_PATTERNS):
        return True
    if any(pat in cmd_lower for pat in _OBFUSCATION_PATTERNS):
        return True
    if "|" in cmd_lower and any(sh in cmd_lower for sh in ("bash", "sh", "python", "perl", "ruby")):
        return True
    return False


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
    return LOCAL_TOOL_SCHEMAS + mcp_tools


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
    """Keep system prompt (msgs[0]) + last MAX_CONTEXT_MESSAGES conversation messages."""
    if len(msgs) <= 1 + MAX_CONTEXT_MESSAGES:
        return msgs
    return [msgs[0]] + msgs[-(MAX_CONTEXT_MESSAGES):]


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
    # Route local tools (all execute via subprocess on client machine)
    _is_local = name in LOCAL_TOOL_NAMES
    _SAFE_LOCAL_TOOLS = {"clipboard_read", "clipboard_write", "web_search_local", "web_fetch_local"}
    if _is_local and name not in _SAFE_LOCAL_TOOLS:
        cmd = args.get("command", "")
        if approval_fn is not None:
            if not approval_fn(name, args):
                return f"{name}: aborted by user", True
        elif is_destructive(cmd):
            import sys as _sys
            print(f"\033[31m⚠ BLOCKED (destructive, no approval_fn): {cmd[:120]}\033[0m",
                  file=_sys.stderr)
            return f"{name}: blocked — destructive command requires explicit approval: {cmd[:120]}", True

    t_start = time.time()

    if name == "clipboard_read":
        from .clipboard import clipboard_read
        result, is_error = clipboard_read()
    elif name == "clipboard_write":
        from .clipboard import clipboard_write
        result, is_error = clipboard_write(args.get("text", ""))
    elif name == "web_search_local":
        from .web_search import web_search_duckduckgo
        result, is_error = web_search_duckduckgo(args.get("query", ""))
    elif name == "web_fetch_local":
        from .web_search import web_fetch
        result, is_error = web_fetch(args.get("url", ""))
    elif _is_local:
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
