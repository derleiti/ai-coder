"""Private structured records and safe typed rollback for AICoder changes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, atomic_write_private

_SECRET = re.compile(r"(?:^|[_-])(?:password|passwd|token|secret|api[_-]?key|authorization|cookie)(?:$|[_-])", re.I)
_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_INLINE = re.compile(r"(?i)\b(password|passwd|token|bearer|secret|api[_-]?key|authorization)\b(\s*[:=]\s*|\s+)([^\s,;]+)")
_ROLLBACK_KINDS = {"restore_file", "remove_created_file", "remove_created_dir", "settings_patch"}


def _text(value: Any, limit: int = 1000) -> str:
    raw = str(value or "")
    return _INLINE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", raw)[:limit]


def _clean(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, list):
        return [_clean(item, depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        return {
            str(key)[:100]: ("[REDACTED]" if _SECRET.search(str(key)) else _clean(item, depth + 1))
            for key, item in list(value.items())[:60]
        }
    return str(value)[:1000]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ChangeJournal:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else CONFIG_DIR / "changes"

    @property
    def snapshot_root(self) -> Path:
        return self.root / "snapshots"

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def _ensure_snapshots(self) -> None:
        self._ensure()
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.snapshot_root, 0o700)
        except OSError:
            pass

    def _path(self, ident: str) -> Path:
        if not _ID.fullmatch(ident):
            raise ValueError("invalid change id")
        self._ensure()
        return self.root / f"change-{ident}.json"

    def _save(self, data: dict[str, Any]) -> None:
        atomic_write_private(self._path(str(data["id"])), json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    def prepare_file_change(self, target: str | Path) -> dict[str, Any]:
        """Snapshot an existing file, or describe safe removal of a newly created file."""
        path = Path(target).expanduser().resolve(strict=False)
        if not path.exists():
            return {"kind": "remove_created_file", "target": str(path)}
        if not path.is_file():
            raise ValueError(f"rollback snapshot requires a regular file: {path}")
        self._ensure_snapshots()
        backup = self.snapshot_root / f"file-{uuid.uuid4().hex}.bak"
        shutil.copy2(path, backup)
        mode = path.stat().st_mode & 0o777
        os.chmod(backup, 0o600)
        return {
            "kind": "restore_file",
            "target": str(path),
            "backup_path": str(backup),
            "pre_sha256": _sha256(backup),
            "mode": mode,
        }

    def prepare_directory_create(self, target: str | Path) -> dict[str, Any] | None:
        path = Path(target).expanduser().resolve(strict=False)
        if path.exists():
            return None
        return {"kind": "remove_created_dir", "target": str(path)}

    def finalize_restore_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any] | None:
        if not metadata:
            return None
        result = dict(metadata)
        kind = str(result.get("kind") or "")
        target = Path(str(result.get("target") or "")).expanduser().resolve(strict=False)
        if kind in {"restore_file", "remove_created_file"} and target.is_file():
            result["post_sha256"] = _sha256(target)
        return result

    def discard_restore_metadata(self, metadata: dict[str, Any] | None) -> None:
        if not metadata or metadata.get("kind") != "restore_file":
            return
        try:
            backup = Path(str(metadata.get("backup_path") or "")).resolve(strict=False)
            if backup.is_file() and backup.is_relative_to(self.snapshot_root.resolve(strict=False)):
                backup.unlink()
        except (OSError, ValueError):
            pass

    def record(
        self, *, tool: str, arguments: dict[str, Any], risk: str, approved: bool,
        result: str, is_error: bool, reason: str = "", reversible: dict[str, Any] | None = None,
        session_id: str = "", task_id: str = "",
    ) -> dict[str, Any]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ident = f"{stamp}-{uuid.uuid4().hex[:8]}"
        restore = dict(reversible or {}) if not is_error else {}
        data = {
            "id": ident,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": _text(session_id, 200),
            "task_id": _text(task_id, 200),
            "tool": str(tool),
            "arguments": _clean(arguments),
            "risk": str(risk),
            "approved": bool(approved),
            "result_summary": _text(result, 2000),
            "is_error": bool(is_error),
            "verification": "failed" if is_error else "pending",
            "reversible": bool(restore),
            "restore_metadata": _clean(restore),
            "rollback_status": "available" if restore else "unavailable",
            "rollback_timestamp": "",
            "rollback_result": "",
            "reason": _text(reason, 1000),
        }
        self._save(data)
        return data

    def get(self, ident: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path(ident).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure()
        out: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("change-*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    out.append(data)
            except (OSError, ValueError, TypeError):
                pass
            if len(out) >= max(1, min(200, int(limit))):
                break
        return out

    def mark_verified(self, ident: str, status: str) -> bool:
        data = self.get(ident)
        if data is None:
            return False
        data["verification"] = str(status)[:80]
        self._save(data)
        return True

    def _validated_snapshot(self, raw: str) -> Path:
        backup = Path(raw).expanduser().resolve(strict=False)
        root = self.snapshot_root.resolve(strict=False)
        if not backup.is_file() or not backup.is_relative_to(root):
            raise ValueError("rollback backup is missing or outside the private snapshot store")
        return backup

    @staticmethod
    def _check_current_file(target: Path, expected: str) -> None:
        if not target.is_file():
            raise ValueError("target no longer exists as a regular file")
        if expected and _sha256(target) != expected:
            raise ValueError("target changed after the journaled action; refusing to overwrite newer work")

    def rollback(self, ident: str, *, approved: bool = False) -> dict[str, Any]:
        """Apply one typed rollback. No arbitrary command stored in a journal is executable."""
        data = self.get(ident)
        if data is None:
            raise ValueError("change not found")
        if not approved:
            raise PermissionError("rollback requires explicit host approval")
        if not data.get("reversible"):
            raise ValueError("change is marked irreversible")
        if data.get("rollback_status") == "completed":
            raise ValueError("change has already been rolled back")
        metadata = data.get("restore_metadata")
        if not isinstance(metadata, dict):
            raise ValueError("rollback metadata is invalid")
        kind = str(metadata.get("kind") or "")
        if kind not in _ROLLBACK_KINDS:
            raise ValueError(f"unsupported rollback kind: {kind or '?'}")

        try:
            if kind == "restore_file":
                target = Path(str(metadata.get("target") or "")).expanduser().resolve(strict=False)
                self._check_current_file(target, str(metadata.get("post_sha256") or ""))
                backup = self._validated_snapshot(str(metadata.get("backup_path") or ""))
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.rollback.", dir=str(target.parent))
                try:
                    with os.fdopen(fd, "wb") as handle, backup.open("rb") as source:
                        shutil.copyfileobj(source, handle)
                        handle.flush(); os.fsync(handle.fileno())
                    os.chmod(tmp_name, int(metadata.get("mode") or 0o600))
                    os.replace(tmp_name, target)
                finally:
                    if os.path.exists(tmp_name):
                        os.unlink(tmp_name)
                if str(metadata.get("pre_sha256") or "") and _sha256(target) != str(metadata["pre_sha256"]):
                    raise RuntimeError("restored file hash does not match the private snapshot")
            elif kind == "remove_created_file":
                target = Path(str(metadata.get("target") or "")).expanduser().resolve(strict=False)
                self._check_current_file(target, str(metadata.get("post_sha256") or ""))
                target.unlink()
            elif kind == "remove_created_dir":
                target = Path(str(metadata.get("target") or "")).expanduser().resolve(strict=False)
                if not target.is_dir():
                    raise ValueError("created directory no longer exists")
                target.rmdir()  # deliberately fails if later work made it non-empty
            elif kind == "settings_patch":
                from . import settings
                previous = metadata.get("previous")
                post = metadata.get("post")
                if not isinstance(previous, dict) or not isinstance(post, dict) or not previous:
                    raise ValueError("settings rollback metadata is incomplete")
                current = settings.STORE.load()
                for key, expected in post.items():
                    if current.get(key, settings.REGISTRY[key].default) != expected:
                        raise ValueError(f"setting '{key}' changed after the journaled action")
                settings.STORE.update(**previous)

            data["rollback_status"] = "completed"
            data["rollback_timestamp"] = datetime.now(timezone.utc).isoformat()
            data["rollback_result"] = f"rolled back via {kind}"
            data["verification"] = "rolled_back"
            self._save(data)
            return {"ok": True, "id": ident, "kind": kind, "status": "completed"}
        except Exception as exc:
            data["rollback_status"] = "failed"
            data["rollback_timestamp"] = datetime.now(timezone.utc).isoformat()
            data["rollback_result"] = _text(f"{type(exc).__name__}: {exc}", 1000)
            self._save(data)
            raise
