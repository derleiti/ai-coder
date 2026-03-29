"""
audit.py — Persistent audit log for all tool executions.
Every local_exec and MCP tool call is recorded with timestamp,
command, result, duration, and error status.

Storage: ~/.config/ai-coder/audit.jsonl (append-only, one JSON per line)
"""
from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

AUDIT_DIR = Path.home() / ".config/ai-coder"
AUDIT_FILE = AUDIT_DIR / "audit.jsonl"
MAX_RESULT_LEN = 2000  # Truncate results in log


def log_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    result: str,
    duration_s: float,
    is_error: bool,
    model: str = "",
    iteration: int = 0,
    session_id: str = "",
) -> None:
    """Append a tool execution record to the audit log."""
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "tool": tool_name,
            "args": _sanitize_args(tool_name, arguments),
            "result": result[:MAX_RESULT_LEN],
            "duration_s": round(duration_s, 3),
            "error": is_error,
            "model": model,
            "iteration": iteration,
            "session": session_id,
        }
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Audit logging must never crash the agent


def _sanitize_args(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Keep full args for local_exec (audit trail), truncate large MCP args."""
    if tool_name == "local_exec":
        return dict(args)  # Full command logged — this is the whole point
    # For MCP tools, truncate large values
    out = {}
    for k, v in args.items():
        sv = str(v)
        out[k] = sv[:500] if len(sv) > 500 else v
    return out


def get_recent(n: int = 50) -> list[Dict[str, Any]]:
    """Read last N audit entries (for GUI display)."""
    if not AUDIT_FILE.exists():
        return []
    try:
        lines = AUDIT_FILE.read_text(encoding="utf-8").strip().split("\n")
        entries = []
        for line in lines[-n:]:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
        return entries
    except Exception:
        return []


def get_local_exec_history(n: int = 20) -> list[Dict[str, Any]]:
    """Get last N local_exec entries specifically."""
    all_entries = get_recent(200)
    return [e for e in all_entries if e.get("tool") == "local_exec"][-n:]
