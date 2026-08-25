"""chat_history.py — SQLite-based persistent chat sessions for ai-coder."""
from __future__ import annotations
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import CONFIG_DIR, ensure_config_dir

DB_PATH = CONFIG_DIR / "chat_history.db"


def _connect() -> sqlite3.Connection:
    ensure_config_dir()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'New Chat',
            model TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            meta TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_session
        ON messages(session_id, id)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            tool TEXT NOT NULL,
            status TEXT NOT NULL,
            iteration INTEGER NOT NULL DEFAULT 0,
            plan_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_tool_events_session
        ON tool_events(session_id, id)
    """)
    conn.commit()
    try:
        os.chmod(DB_PATH, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(DB_PATH) + suffix)
            if sidecar.exists():
                os.chmod(sidecar, 0o600)
    except OSError:
        pass
    return conn


def create_session(title: str = "New Chat", model: str = "") -> str:
    """Create a new chat session. Returns session ID."""
    sid = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO sessions (id, title, model, created_at, updated_at) VALUES (?,?,?,?,?)",
        (sid, title, model, now, now),
    )
    conn.commit()
    conn.close()
    return sid


def save_message(session_id: str, role: str, content: str, meta: str = "") -> None:
    """Append a message to a session."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
        (session_id, role, content, meta, now),
    )
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (now, session_id),
    )
    conn.commit()
    conn.close()


def save_tool_event(
    session_id: str,
    tool: str,
    status: str,
    *,
    iteration: int = 0,
    plan_id: str = "",
) -> None:
    """Persist minimal tool evidence for a chat session, never arguments or output."""
    sid = str(session_id or "").strip()
    name = str(tool or "").strip()
    state = str(status or "").strip().lower()
    if not sid or not name or state not in {"ok", "error", "blocked"}:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO tool_events (session_id, tool, status, iteration, plan_id, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (sid, name[:120], state, max(0, int(iteration or 0)), str(plan_id or "")[:120], now),
    )
    conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, sid))
    conn.commit()
    conn.close()


def load_tool_events(session_id: str, limit: int = 80) -> List[Dict[str, Any]]:
    """Load recent minimal tool evidence oldest-to-newest for context replay."""
    cap = max(1, min(200, int(limit or 80)))
    conn = _connect()
    rows = conn.execute(
        "SELECT tool, status, iteration, plan_id, created_at FROM ("
        "SELECT id, tool, status, iteration, plan_id, created_at FROM tool_events "
        "WHERE session_id = ? ORDER BY id DESC LIMIT ?"
        ") ORDER BY id",
        (session_id, cap),
    ).fetchall()
    conn.close()
    return [
        {"tool": r[0], "status": r[1], "iteration": int(r[2] or 0),
         "plan_id": r[3] or "", "created_at": r[4]}
        for r in rows
    ]


def render_tool_evidence(session_id: str, limit: int = 40) -> str:
    """Render compact, output-free tool activity metadata for model context."""
    events = load_tool_events(session_id, limit=limit)
    if not events:
        return ""
    rows = [
        "Prior tool activity in this chat (metadata only; no arguments or outputs were persisted):"
    ]
    for event in events:
        stamp = str(event.get("created_at") or "")
        hhmmss = stamp[11:19] if len(stamp) >= 19 else stamp[:19]
        iteration = int(event.get("iteration") or 0)
        suffix = f" · step {iteration}" if iteration else ""
        rows.append(f"- {hhmmss} · {event['tool']} · {event['status']}{suffix}")
    rows.append("Re-inspect current workspace state whenever exact evidence is required.")
    return "\n".join(rows)


def update_title(session_id: str, title: str) -> None:
    """Update session title (e.g. from first user message)."""
    conn = _connect()
    conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
    conn.commit()
    conn.close()


def list_sessions(limit: int = 30) -> List[Dict[str, Any]]:
    """List recent sessions, newest first."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, title, model, created_at, updated_at FROM sessions "
        "ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "title": r[1], "model": r[2], "created_at": r[3], "updated_at": r[4]}
        for r in rows
    ]


def load_messages(session_id: str) -> List[Dict[str, str]]:
    """Load all messages for a session."""
    conn = _connect()
    rows = conn.execute(
        "SELECT role, content, meta, created_at FROM messages "
        "WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    conn.close()
    return [
        {"role": r[0], "content": r[1], "meta": r[2], "created_at": r[3]}
        for r in rows
    ]


def delete_session(session_id: str) -> None:
    """Delete a session and all its messages."""
    conn = _connect()
    conn.execute("DELETE FROM tool_events WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()


def clear_history() -> None:
    """Delete every persisted chat session and its display/tool history."""
    conn = _connect()
    conn.execute("DELETE FROM tool_events")
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM sessions")
    conn.commit()
    conn.close()


def get_session_messages_for_api(session_id: str) -> List[Dict[str, str]]:
    """Load messages in API format (role + content only, for context replay)."""
    msgs = load_messages(session_id)
    return [{"role": m["role"], "content": m["content"]} for m in msgs
            if m["role"] in ("system", "user", "assistant")]
