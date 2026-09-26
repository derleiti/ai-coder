"""Local-first synchronized Project Memory for AICoder."""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, ensure_config_dir
from .evidence_memory import DB_PATH

ALLOWED_KINDS = {
    "todo", "idea", "decision", "architecture", "bug", "lesson",
    "feature", "project_summary", "documentation",
}
_SECRET = re.compile(
    r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)"
    r"|(?:authorization|cookie|set-cookie)\s*[:=][^\r\n]*"
    r"|(?:api[_-]?key|token|password|passwd|client[_-]?secret|session[_-]?secret)"
    r"\s*[:=]\s*[^\s,;}]+"
    r"|\bBearer\s+\S+"
    r"|\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{20,}|github_pat_[\w]+|AKIA[A-Z0-9]{16})"
)

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _normalize_remote(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"^[^@/]+@", "", raw)
    if re.match(r"^[^:/]+:[^/]", raw):
        host, path = raw.split(":", 1)
        raw = f"https://{host}/{path}"
    raw = re.sub(r"(?i)^(https?|ssh)://[^/@]+@", r"\1://", raw)
    raw = raw.rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    return raw.casefold()

def _git_remote(workspace: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        return _normalize_remote(proc.stdout)
    except Exception:
        return ""

def project_identity(workspace: str | Path) -> str:
    root = Path(workspace or ".").expanduser().resolve(strict=False)
    remote = _git_remote(root)
    if remote:
        return "git:" + hashlib.sha256(remote.encode()).hexdigest()[:32]

    git_dir = root / ".git"
    if git_dir.is_dir():
        identity_file = git_dir / "ailinux-project-id"
        if identity_file.exists():
            value = identity_file.read_text(encoding="utf-8").strip()
        else:
            value = str(uuid.uuid4())
            try:
                identity_file.write_text(value + "\n", encoding="utf-8")
                os.chmod(identity_file, 0o600)
            except OSError:
                value = ""
        if value:
            return "repo:" + value

    identity_file = root / ".aicoder-project-id"
    if identity_file.exists():
        value = identity_file.read_text(encoding="utf-8").strip()
        if value:
            return "workspace:" + value
    try:
        value = str(uuid.uuid4())
        identity_file.write_text(value + "\n", encoding="utf-8")
        return "workspace:" + value
    except OSError:
        resolved = str(root)
        return "path:" + hashlib.sha256(resolved.encode()).hexdigest()[:32]

def device_identity() -> str:
    override = str(os.environ.get("AICODER_DEVICE_ID") or "").strip()
    if override:
        return override[:128]
    ensure_config_dir()
    path = CONFIG_DIR / "device_id"
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
        path.write_text(value + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return value
    except OSError:
        return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:20]

def _hash(kind: str, title: str, content: str, status: str, deleted: bool) -> str:
    payload = json.dumps(
        {"kind": kind, "title": title, "content": content, "status": status, "deleted": bool(deleted)},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()

def _validate(kind: str, title: str, content: str, status: str) -> None:
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"unsupported project memory kind: {kind}")
    if not str(title).strip():
        raise ValueError("title is required")
    if len(str(title)) > 512 or len(str(content)) > 12000 or len(str(status)) > 64:
        raise ValueError("project memory field exceeds size limit")
    for value in (title, content, status):
        if _SECRET.search(str(value or "")):
            raise ValueError("project memory contains secret-like material")

class LocalProjectMemory:
    def __init__(self, workspace: str | Path, db_path: Path | None = None):
        self.workspace = str(Path(workspace or ".").expanduser().resolve(strict=False))
        self.project_key = project_identity(self.workspace)
        self.device_id = device_identity()
        self.db_path = Path(db_path) if db_path is not None else DB_PATH
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        ensure_config_dir()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=3)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS project_memory (
                    project_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    base_version INTEGER NOT NULL DEFAULT 0,
                    server_seq INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    sync_state TEXT NOT NULL DEFAULT 'dirty',
                    PRIMARY KEY(project_key, entity_id)
                );
                CREATE INDEX IF NOT EXISTS idx_project_memory_search
                    ON project_memory(project_key, deleted, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_project_memory_dirty
                    ON project_memory(project_key, sync_state);
                CREATE TABLE IF NOT EXISTS project_memory_sync_state (
                    project_key TEXT PRIMARY KEY,
                    cursor INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_memory_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    local_json TEXT NOT NULL,
                    remote_json TEXT NOT NULL,
                    attempted_revision_seq INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0
                );
            """)
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["deleted"] = bool(data["deleted"])
        return data

    def store(
        self, *, kind: str, title: str, content: str = "", status: str = "active",
        entity_id: str | None = None, source: str = "aicoder",
    ) -> dict[str, Any]:
        kind, title, content, status = map(str, (kind, title, content, status))
        _validate(kind, title, content, status)
        entity_id = str(entity_id or uuid.uuid4())
        conn = self._connect()
        try:
            current = conn.execute(
                "SELECT version FROM project_memory WHERE project_key=? AND entity_id=?",
                (self.project_key, entity_id),
            ).fetchone()
            base_version = int(current["version"]) if current else 0
            digest = _hash(kind, title, content, status, False)
            conn.execute(
                """INSERT INTO project_memory
                   (project_key,entity_id,kind,title,content,status,version,base_version,server_seq,
                    content_hash,device_id,source,updated_at,deleted,sync_state)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(project_key,entity_id) DO UPDATE SET
                     kind=excluded.kind,title=excluded.title,content=excluded.content,status=excluded.status,
                     base_version=project_memory.version,content_hash=excluded.content_hash,
                     device_id=excluded.device_id,source=excluded.source,updated_at=excluded.updated_at,
                     deleted=0,sync_state='dirty'""",
                (
                    self.project_key, entity_id, kind, title, content, status,
                    base_version, base_version, 0, digest, self.device_id, source, _now(), 0, "dirty",
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM project_memory WHERE project_key=? AND entity_id=?",
                (self.project_key, entity_id),
            ).fetchone()
            return self._row(row)
        finally:
            conn.close()

    def update(self, entity_id: str, **changes: Any) -> dict[str, Any]:
        current = self.get(entity_id)
        if current is None:
            raise KeyError(entity_id)
        kind = str(changes.get("kind", current["kind"]))
        title = str(changes.get("title", current["title"]))
        content = str(changes.get("content", current["content"]))
        status = str(changes.get("status", current["status"]))
        deleted = bool(changes.get("deleted", current["deleted"]))
        _validate(kind, title, content, status)
        digest = _hash(kind, title, content, status, deleted)
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE project_memory SET kind=?,title=?,content=?,status=?,base_version=version,
                   content_hash=?,device_id=?,source=?,updated_at=?,deleted=?,sync_state='dirty'
                   WHERE project_key=? AND entity_id=?""",
                (
                    kind, title, content, status, digest, self.device_id,
                    str(changes.get("source") or current["source"] or "aicoder"), _now(), int(deleted),
                    self.project_key, entity_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get(entity_id)  # type: ignore[return-value]

    def delete(self, entity_id: str) -> dict[str, Any]:
        return self.update(entity_id, deleted=True, content="")

    def get(self, entity_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM project_memory WHERE project_key=? AND entity_id=?",
                (self.project_key, str(entity_id)),
            ).fetchone()
            return self._row(row) if row else None
        finally:
            conn.close()

    def list(self, *, kind: str = "", include_deleted: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            sql = "SELECT * FROM project_memory WHERE project_key=?"
            params: list[Any] = [self.project_key]
            if kind:
                sql += " AND kind=?"; params.append(kind)
            if not include_deleted:
                sql += " AND deleted=0"
            sql += " ORDER BY updated_at DESC LIMIT ?"; params.append(max(1, min(int(limit), 500)))
            return [self._row(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def search(self, query: str, *, kinds: list[str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
        terms = [t.casefold() for t in re.findall(r"[\w./-]{2,}", str(query or ""))][:12]
        rows = self.list(include_deleted=False, limit=500)
        if kinds:
            allowed = set(kinds)
            rows = [r for r in rows if r["kind"] in allowed]
        if not terms:
            return rows[:max(1, min(int(limit), 50))]
        def score(row: dict[str, Any]) -> tuple[int, int, str]:
            hay = f'{row["title"]} {row["content"]} {row["status"]}'.casefold()
            s = sum(hay.count(term) for term in terms)
            kind_bonus = 2 if row["kind"] in {"decision", "architecture", "bug", "todo"} else 0
            return (s + kind_bonus, int(row["server_seq"]), row["updated_at"])
        rows = [r for r in rows if score(r)[0] > 0]
        rows.sort(key=score, reverse=True)
        return rows[:max(1, min(int(limit), 50))]

    def dirty_changes(self, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT entity_id,kind,title,content,status,base_version,content_hash,device_id,
                          source,updated_at,deleted
                   FROM project_memory WHERE project_key=? AND sync_state='dirty'
                   ORDER BY updated_at ASC LIMIT ?""",
                (self.project_key, max(1, min(int(limit), 100))),
            ).fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    def cursor(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT cursor FROM project_memory_sync_state WHERE project_key=?",
                (self.project_key,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def apply_sync(self, response: dict[str, Any]) -> None:
        accepted = list(response.get("accepted") or [])
        conflicts = list(response.get("conflicts") or [])
        remote = list(response.get("remote") or [])
        cursor = int(response.get("cursor") or 0)
        accepted_ids = {str(item.get("entity_id") or "") for item in accepted}
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            for item in accepted:
                self._upsert_server(conn, item, sync_state="clean")
            for item in remote:
                entity_id = str(item.get("entity_id") or "")
                local = conn.execute(
                    "SELECT sync_state,server_seq FROM project_memory WHERE project_key=? AND entity_id=?",
                    (self.project_key, entity_id),
                ).fetchone()
                if local and local["sync_state"] == "dirty" and entity_id not in accepted_ids:
                    continue
                if local and int(local["server_seq"] or 0) > int(item.get("server_seq") or 0):
                    continue
                self._upsert_server(conn, item, sync_state="clean")
            for conflict in conflicts:
                entity_id = str(conflict.get("entity_id") or "")
                local = conn.execute(
                    "SELECT * FROM project_memory WHERE project_key=? AND entity_id=?",
                    (self.project_key, entity_id),
                ).fetchone()
                remote_current = conflict.get("current") or {}
                conn.execute(
                    """INSERT INTO project_memory_conflicts
                       (project_key,entity_id,local_json,remote_json,attempted_revision_seq,created_at,resolved)
                       VALUES (?,?,?,?,?,?,0)""",
                    (
                        self.project_key, entity_id,
                        json.dumps(dict(local) if local else conflict.get("attempted") or {}, ensure_ascii=False),
                        json.dumps(remote_current, ensure_ascii=False),
                        int(conflict.get("attempted_revision_seq") or 0), _now(),
                    ),
                )
                if local:
                    conn.execute(
                        """UPDATE project_memory SET sync_state='conflict',
                           version=?,base_version=?,server_seq=?
                           WHERE project_key=? AND entity_id=?""",
                        (
                            int(remote_current.get("version") or local["version"] or 0),
                            int(remote_current.get("version") or local["base_version"] or 0),
                            int(remote_current.get("server_seq") or local["server_seq"] or 0),
                            self.project_key, entity_id,
                        ),
                    )
            conn.execute(
                """INSERT INTO project_memory_sync_state(project_key,cursor,updated_at) VALUES (?,?,?)
                   ON CONFLICT(project_key) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at""",
                (self.project_key, cursor, _now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _upsert_server(self, conn: sqlite3.Connection, item: dict[str, Any], *, sync_state: str) -> None:
        entity_id = str(item.get("entity_id") or "")
        kind = str(item.get("kind") or "")
        title = str(item.get("title") or "")
        content = str(item.get("content") or "")
        status = str(item.get("status") or "active")
        deleted = bool(item.get("deleted"))
        _validate(kind, title, content, status)
        digest = str(item.get("content_hash") or _hash(kind, title, content, status, deleted))
        version = int(item.get("version") or 0)
        conn.execute(
            """INSERT INTO project_memory
               (project_key,entity_id,kind,title,content,status,version,base_version,server_seq,
                content_hash,device_id,source,updated_at,deleted,sync_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(project_key,entity_id) DO UPDATE SET
                 kind=excluded.kind,title=excluded.title,content=excluded.content,status=excluded.status,
                 version=excluded.version,base_version=excluded.version,server_seq=excluded.server_seq,
                 content_hash=excluded.content_hash,device_id=excluded.device_id,source=excluded.source,
                 updated_at=excluded.updated_at,deleted=excluded.deleted,sync_state=excluded.sync_state""",
            (
                self.project_key, entity_id, kind, title, content, status, version, version,
                int(item.get("server_seq") or 0), digest, str(item.get("device_id") or self.device_id),
                str(item.get("source") or "remote"), str(item.get("updated_at") or _now()),
                int(deleted), sync_state,
            ),
        )

    def conflicts(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT id,entity_id,local_json,remote_json,attempted_revision_seq,created_at,resolved
                   FROM project_memory_conflicts WHERE project_key=?
                   ORDER BY id DESC LIMIT ?""",
                (self.project_key, max(1, min(int(limit), 200))),
            ).fetchall()
            return [
                {
                    "id": int(r["id"]), "entity_id": r["entity_id"],
                    "local": json.loads(r["local_json"]), "remote": json.loads(r["remote_json"]),
                    "attempted_revision_seq": int(r["attempted_revision_seq"]),
                    "created_at": r["created_at"], "resolved": bool(r["resolved"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def sync(self, client: Any) -> dict[str, Any]:
        payload = {
            "project_key": self.project_key,
            "cursor": self.cursor(),
            "changes": self.dirty_changes(),
        }
        response = client.project_memory_sync(payload)
        self.apply_sync(response)
        return {
            "status": "ok",
            "project_key": self.project_key,
            "cursor": self.cursor(),
            "accepted": len(response.get("accepted") or []),
            "conflicts": len(response.get("conflicts") or []),
            "remote": len(response.get("remote") or []),
        }

    def context(self, query: str, limit: int = 6, max_chars: int = 5000) -> str:
        rows = self.search(query, limit=limit)
        if not rows:
            return ""
        lines = [
            "CURRENT PROJECT MEMORY (current state; code/runtime evidence still has priority):"
        ]
        for row in rows:
            line = json.dumps(
                {k: row[k] for k in ("entity_id", "kind", "title", "content", "status", "version") if k in row},
                ensure_ascii=False, separators=(",", ":"),
            )
            if sum(len(x) + 1 for x in lines) + len(line) > max_chars:
                break
            lines.append(line)
        return "\n".join(lines)
