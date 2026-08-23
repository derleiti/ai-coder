"""Project-scoped evidence metadata with a RAM hot cache and SQLite durability.

The store deliberately does not persist raw source/tool output. It remembers
what was inspected and whether it changed, plus privacy-safe failure fingerprints.
Higher-level summaries can be layered on later without making raw transcripts the
memory primitive.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG_DIR, ensure_config_dir

DB_PATH = CONFIG_DIR / "evidence.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_key(workspace: str) -> str:
    resolved = str(Path(workspace or ".").expanduser().resolve(strict=False))
    return hashlib.sha256(resolved.encode("utf-8", errors="replace")).hexdigest()[:20]


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FileEvidence:
    path: str
    content_hash: str
    size: int
    mtime_ns: int
    start_line: int
    end_line: int
    updated_at: str


class ProjectEvidenceStore:
    """Fast per-run cache backed by a private project-scoped SQLite store."""

    def __init__(self, workspace: str, db_path: Path | None = None):
        self.workspace = str(Path(workspace or ".").expanduser().resolve(strict=False))
        self.workspace_key = _workspace_key(self.workspace)
        self.db_path = Path(db_path) if db_path is not None else DB_PATH
        self._files: dict[tuple[str, int, int], FileEvidence] = {}
        self._ensure_schema()
        self._warm_recent_files(limit=128)

    def _connect(self) -> sqlite3.Connection:
        ensure_config_dir()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=2.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_evidence (
                    workspace_key TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_key, path, start_line, end_line)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS failure_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_key TEXT NOT NULL,
                    category TEXT NOT NULL,
                    signature_hash TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (workspace_key, signature_hash)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_file_evidence_recent
                ON file_evidence(workspace_key, updated_at DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_failure_evidence_recent
                ON failure_evidence(workspace_key, updated_at DESC)
            """)
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _warm_recent_files(self, limit: int) -> None:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT path, content_hash, size, mtime_ns, start_line, end_line, updated_at "
                "FROM file_evidence WHERE workspace_key=? ORDER BY updated_at DESC LIMIT ?",
                (self.workspace_key, max(1, int(limit))),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            item = FileEvidence(*row)
            self._files[(item.path, item.start_line, item.end_line)] = item

    def _resolve_file(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path(self.workspace) / path
        return path.resolve(strict=True)

    def inspect_file(
        self, path: str, start_line: int = 1, end_line: int = 0, *, force_hash: bool = False
    ) -> tuple[FileEvidence, bool]:
        """Record file identity/range and return (record, unchanged_from_previous).

        ``force_hash`` bypasses the size+mtime fast path for explicit rechecks.
        """
        resolved = self._resolve_file(path)
        if not resolved.is_file():
            raise ValueError(f"not a regular file: {resolved}")
        stat = resolved.stat()
        start = max(1, int(start_line or 1))
        end = max(0, int(end_line or 0))
        key = (str(resolved), start, end)
        previous = self._files.get(key)

        # Avoid hashing an unchanged file repeatedly inside one run. mtime+size is
        # only the fast path; a changed stat always gets a content digest.
        if (
            not force_hash
            and previous
            and previous.size == stat.st_size
            and previous.mtime_ns == stat.st_mtime_ns
        ):
            return previous, True

        content_hash = _file_digest(resolved)
        unchanged = bool(previous and previous.content_hash == content_hash)
        item = FileEvidence(
            path=str(resolved), content_hash=content_hash, size=stat.st_size,
            mtime_ns=stat.st_mtime_ns, start_line=start, end_line=end, updated_at=_now(),
        )
        self._files[key] = item
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO file_evidence
                   (workspace_key,path,content_hash,size,mtime_ns,start_line,end_line,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(workspace_key,path,start_line,end_line) DO UPDATE SET
                     content_hash=excluded.content_hash,size=excluded.size,mtime_ns=excluded.mtime_ns,
                     updated_at=excluded.updated_at""",
                (self.workspace_key, item.path, item.content_hash, item.size, item.mtime_ns,
                 item.start_line, item.end_line, item.updated_at),
            )
            conn.commit()
        finally:
            conn.close()
        return item, unchanged

    def remember_failure(self, category: str, signature: str, count: int) -> None:
        """Persist only a digest of a normalized failure signature, never raw stderr."""
        signature_hash = hashlib.sha256(str(signature).encode("utf-8", errors="replace")).hexdigest()
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO failure_evidence
                   (workspace_key,category,signature_hash,occurrence_count,updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(workspace_key,signature_hash) DO UPDATE SET
                     category=excluded.category,
                     occurrence_count=MAX(failure_evidence.occurrence_count, excluded.occurrence_count),
                     updated_at=excluded.updated_at""",
                (self.workspace_key, str(category)[:40], signature_hash, max(1, int(count)), _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def recent_files(self, limit: int = 20) -> list[FileEvidence]:
        return sorted(self._files.values(), key=lambda item: item.updated_at, reverse=True)[:max(0, int(limit))]

    def health(self) -> dict[str, int | str]:
        conn = self._connect()
        try:
            files = conn.execute(
                "SELECT COUNT(*) FROM file_evidence WHERE workspace_key=?",
                (self.workspace_key,),
            ).fetchone()[0]
            failures = conn.execute(
                "SELECT COUNT(*) FROM failure_evidence WHERE workspace_key=?",
                (self.workspace_key,),
            ).fetchone()[0]
        finally:
            conn.close()
        return {
            "status": "ready",
            "hot_files": len(self._files),
            "persisted_files": int(files),
            "persisted_failures": int(failures),
        }

    def close(self) -> None:
        """Compatibility hook for future backends; SQLite connections are short-lived."""
        return None
