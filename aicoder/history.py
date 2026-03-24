from __future__ import annotations
"""
history.py — Persistent call history for ask/task results.
Saves last N entries to ~/.config/ai-coder/history.json
"""
import json, os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_DIR = Path.home() / ".config/ai-coder"
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
        "prompt": prompt[:500],
        "response": response[:2000],
        "files": files or [],
        "latency_ms": latency_ms,
    })
    _save(entries[-MAX_ENTRIES:])


def get_history(n: int = 10) -> List[Dict[str, Any]]:
    return _load()[-n:]


def clear_history() -> None:
    if HISTORY_FILE.exists():
        HISTORY_FILE.unlink()
