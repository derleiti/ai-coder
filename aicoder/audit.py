"""
audit.py — Persistent audit log for all tool executions.
Every local capability and MCP tool call is recorded with timestamp,
command, result, duration, and error status.

Storage: ~/.config/ai-coder/audit.jsonl (append-only, one JSON per line)
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .config import ensure_config_dir

AUDIT_DIR = Path.home() / ".config/ai-coder"
AUDIT_FILE = AUDIT_DIR / "audit.jsonl"
MAX_RESULT_LEN = 2000  # Truncate results in log

# Patterns that indicate sensitive data in command output / results
_SECRET_PATTERNS = (
    "password", "passwd", "token", "bearer", "secret", "api_key",
    "authorization", "private_key", "client_secret", "access_token",
)
_SENSITIVE_KEYS = {item.replace("_", "") for item in _SECRET_PATTERNS}
_INLINE_SECRET_RE = __import__("re").compile(
    r"(?i)\b(password|passwd|token|bearer|secret|api[_-]?key|authorization|"
    r"private[_-]?key|client[_-]?secret|access[_-]?token)\b(\s*[:=]\s*|\s+)([^\s]+)"
)


def _redact_inline(value: str) -> str:
    return _INLINE_SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)

def _redact_result(result: str) -> str:
    """Redact lines that likely contain secrets from logged output."""
    out = []
    for line in result.split("\n"):
        ll = line.lower()
        if any(pat in ll for pat in _SECRET_PATTERNS) and ("=" in line or ":" in line):
            # Keep key name, redact value
            idx = max(line.find("="), line.find(":"))
            out.append(line[:idx+1] + " [REDACTED]")
        else:
            out.append(line)
    return "\n".join(out)


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
        ensure_config_dir()
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "args": _sanitize_args(tool_name, arguments),
            "result": _redact_result(result[:MAX_RESULT_LEN]),
            "duration_s": round(duration_s, 3),
            "error": is_error,
            "model": model,
            "iteration": iteration,
            "session": session_id,
        }
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        try:
            os.chmod(AUDIT_FILE, 0o600)
        except OSError:
            pass
    except Exception:
        pass  # Audit logging must never crash the agent


def _sanitize_args(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively redact sensitive values and bound the audit payload."""

    def clean(key: str, value: Any) -> Any:
        normalized = key.lower().replace("_", "").replace("-", "")
        if normalized in _SENSITIVE_KEYS:
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): clean(str(k), v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(key, item) for item in value[:100]]
        if isinstance(value, str):
            redacted = _redact_inline(value)
            return redacted[:2000] if tool_name == "local_exec" else redacted[:500]
        return value

    return {str(key): clean(str(key), value) for key, value in args.items()}


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
