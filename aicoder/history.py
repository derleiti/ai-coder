from __future__ import annotations
"""
history.py — Persistent call history for ask/task results.
Saves last N entries to ~/.config/ai-coder/history.json
"""
import json, os, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import CONFIG_DIR

HISTORY_FILE = CONFIG_DIR / "history.json"
MAX_ENTRIES = 50


def _load() -> List[Dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(entries: List[Dict[str, Any]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(HISTORY_FILE, 0o600)


_SECRET_RE = re.compile(
    r"(?i)(password|token|bearer|secret|api.?key|authorization"
    r"|private.?key|client.?secret|access.?token)[\s=:]+\S+",
    re.IGNORECASE,
)

def _redact(text: str) -> str:
    """Redact secret-looking patterns from text before storing in history."""
    return _SECRET_RE.sub(lambda m: m.group(1) + "=[REDACTED]", text)


def record(
    kind: str,          # "ask" | "task"
    prompt: str,
    response: str,
    model: Optional[str] = None,
    files: Optional[List[str]] = None,
    latency_ms: Optional[int] = None,
) -> None:
    entries = _load()
    entries.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "model": model,
        "prompt": _redact(prompt[:500]),
        "response": _redact(response[:2000]),
        "files": files or [],
        "latency_ms": latency_ms,
    })
    _save(entries[-MAX_ENTRIES:])


def get_history(n: int = 10) -> List[Dict[str, Any]]:
    return _load()[-n:]


def clear_history() -> None:
    if HISTORY_FILE.exists():
        HISTORY_FILE.unlink()
