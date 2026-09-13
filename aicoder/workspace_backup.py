"""Shared AILinux workspace recovery backup convention.

All participating local apps use ~/workspace/.workspacebackup by default. The
location can be overridden process-wide for tests or custom deployments with
AILINUX_WORKSPACE_ROOT / AILINUX_WORKSPACE_BACKUP_ROOT.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import shlex
import subprocess
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

WORKSPACE_ROOT_ENV = "AILINUX_WORKSPACE_ROOT"
BACKUP_ROOT_ENV = "AILINUX_WORKSPACE_BACKUP_ROOT"


def shared_workspace_root() -> Path:
    return Path(os.environ.get(WORKSPACE_ROOT_ENV, str(Path.home() / "workspace"))).expanduser().resolve(strict=False)


def shared_backup_root() -> Path:
    configured = os.environ.get(BACKUP_ROOT_ENV)
    root = Path(configured).expanduser() if configured else shared_workspace_root() / ".workspacebackup"
    root = root.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def ensure_workspace_layout() -> tuple[Path, Path]:
    workspace = shared_workspace_root()
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace, shared_backup_root()


def _action_dir(workspace: Path, source: str) -> Path:
    key = hashlib.sha256(str(workspace.resolve(strict=False)).encode("utf-8")).hexdigest()[:16]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    safe_source = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in str(source or "mutation"))[:64]
    path = shared_backup_root() / "aicoder" / key / f"{stamp}-{uuid.uuid4().hex[:8]}-{safe_source}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _write_meta(action: Path, *, workspace: Path, source: str, kind: str, target: str = "") -> dict[str, str | int]:
    payload: dict[str, str | int] = {
        "schema": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(workspace.resolve(strict=False)),
        "source": source,
        "kind": kind,
        "target": target,
    }
    (action / "metadata.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _write_backup_doc(action: Path, payload: dict[str, str | int], recovery: list[str]) -> None:
    lines = [
        "# Workspace backup", "",
        f"- Created: {payload['created_at']}",
        f"- Source workspace: `{payload['workspace']}`",
        f"- Trigger: `{payload['source']}`",
        f"- Backup kind: `{payload['kind']}`",
        f"- Original target: `{payload.get('target') or '(workspace)'}`",
        f"- Backup directory: `{action}`", "",
        "## Recovery", "",
        "Compare current state before recovery. Restore only the intended file or workspace state.", "",
    ]
    lines.extend(f"- `{cmd}`" for cmd in recovery)
    (action / "backup.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_index(action: Path, payload: dict[str, str | int]) -> None:
    root = shared_backup_root()
    index = root / "INDEX.md"
    lock_path = root / ".index.lock"
    with lock_path.open("a+") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not index.exists():
            index.write_text(
                "# Workspace Backup Index\n\n"
                "All AILinux/TriForce/AICoder fallback backups share this index. "
                "Every backup directory contains its own `backup.md`.\n\n",
                encoding="utf-8",
            )
        with index.open("a", encoding="utf-8") as handle:
            handle.write(
                f"- {payload['created_at']} — `aicoder:{payload['source']}` — {payload['kind']} — "
                f"`{payload.get('target') or payload['workspace']}` — `{action}`\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _document(action: Path, payload: dict[str, str | int], recovery: list[str]) -> None:
    _write_backup_doc(action, payload, recovery)
    _append_index(action, payload)


def snapshot_absence(workspace: Path, target: Path, *, source: str, kind: str) -> Path:
    workspace = workspace.expanduser().resolve(strict=False)
    target = target.expanduser().resolve(strict=False)
    if target.exists() or target.is_symlink():
        raise ValueError(f"absence snapshot requires a missing target: {target}")
    action = _action_dir(workspace, source)
    try:
        rel = target.relative_to(workspace).as_posix()
    except ValueError:
        rel = "external/" + hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:16] + "/" + target.name
    payload = _write_meta(action, workspace=workspace, source=source, kind=kind, target=rel)
    (action / "target-was-absent").write_text(str(target) + "\n", encoding="utf-8")
    recovery = (
        f"rmdir -- {shlex.quote(str(target))}  # remove only if still empty"
        if kind == "directory-absent"
        else f"rm -f -- {shlex.quote(str(target))}  # restore original absent state"
    )
    _document(action, payload, [recovery])
    return action


def snapshot_file(workspace: Path, target: Path, *, source: str) -> Path:
    """Persist pre-change state for one file, including an explicit absent marker."""
    workspace = workspace.expanduser().resolve(strict=False)
    target = target.expanduser().resolve(strict=False)
    if not target.exists():
        return snapshot_absence(workspace, target, source=source, kind="file-absent")
    if not target.is_file() or target.is_symlink():
        raise ValueError(f"file recovery snapshot requires a regular file: {target}")
    action = _action_dir(workspace, source)
    try:
        rel = target.resolve(strict=False).relative_to(workspace.resolve(strict=False)).as_posix()
    except ValueError:
        rel = "external/" + hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:16] + "/" + target.name
    payload = _write_meta(action, workspace=workspace, source=source, kind="file", target=rel)
    backup = action / "files" / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, backup, follow_symlinks=False)
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    _document(action, payload, [f"cp -a -- {shlex.quote(str(backup))} {shlex.quote(str(target))}"])
    return backup



_LOCAL_CONFIG_SUFFIXES = {".env", ".ini", ".conf", ".cfg", ".toml", ".yaml", ".yml", ".json", ".key", ".crt", ".pem"}
_LOCAL_CONFIG_BASENAMES = {"wp-config.php"}
_LOCAL_CONFIG_MAX_BYTES = 2 * 1024 * 1024
_LOCAL_CONFIG_MAX_DEPTH = 4


def _is_local_config_candidate(path: Path, workspace: Path) -> bool:
    try:
        rel = path.relative_to(workspace)
        stat = path.stat()
    except (OSError, ValueError):
        return False
    if len(rel.parts) > _LOCAL_CONFIG_MAX_DEPTH or stat.st_size > _LOCAL_CONFIG_MAX_BYTES:
        return False
    if not os.access(path, os.R_OK):
        return False
    name = path.name.lower()
    return (
        name.startswith(".env")
        or name.endswith(".env")
        or path.suffix.lower() in _LOCAL_CONFIG_SUFFIXES
        or name in _LOCAL_CONFIG_BASENAMES
    )


def _git_snapshot_paths(workspace: Path) -> list[Path] | None:
    """Return source files plus bounded ignored local configuration for a Git root.

    Tracked and non-ignored untracked files define the recoverable project source.
    Small configuration-like files are additionally retained even when ignored so
    local runtime credentials/settings survive rollback. Heavy ignored runtime data,
    caches, package trees and generated artefacts are intentionally excluded.
    """
    try:
        top = subprocess.check_output(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
        if Path(top).resolve(strict=False) != workspace.resolve(strict=False):
            return None
        raw = subprocess.check_output(
            ["git", "-C", str(workspace), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            stderr=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    selected: dict[str, Path] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        rel = item.decode("utf-8", errors="surrogateescape")
        candidate = workspace / rel
        if candidate.exists() or candidate.is_symlink():
            selected[rel] = candidate

    workspace_dev = os.lstat(workspace).st_dev
    # Recover ignored local configuration without walking into other filesystems.
    for base, dirnames, filenames in os.walk(workspace, topdown=True, followlinks=False):
        base_path = Path(base)
        rel_base = base_path.relative_to(workspace)
        if len(rel_base.parts) >= _LOCAL_CONFIG_MAX_DEPTH:
            dirnames[:] = []
        else:
            kept = []
            for name in dirnames:
                child = base_path / name
                if name in {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", "dist", "build", "logs", ".workspacebackup"} or name.startswith(".venv-"):
                    continue
                try:
                    if os.lstat(child).st_dev != workspace_dev or child.is_symlink():
                        continue
                except OSError:
                    continue
                kept.append(name)
            dirnames[:] = kept
        for name in filenames:
            candidate = base_path / name
            if _is_local_config_candidate(candidate, workspace):
                selected[candidate.relative_to(workspace).as_posix()] = candidate
    return list(selected.values())


def snapshot_workspace(workspace: Path, *, source: str) -> Path:
    """Create a persistent pre-change archive for commands with unknown write scope."""
    workspace = workspace.expanduser().resolve(strict=True)
    action = _action_dir(workspace, source)
    payload = _write_meta(action, workspace=workspace, source=source, kind="workspace")
    archive = action / "workspace.tar.gz"
    backup_root = shared_backup_root().resolve(strict=False)
    git_paths = _git_snapshot_paths(workspace)
    with tarfile.open(archive, "w:gz") as tar:
        if git_paths is not None:
            for child in git_paths:
                try:
                    resolved = child.resolve(strict=False)
                except OSError:
                    resolved = child.absolute()
                if resolved == backup_root or backup_root in resolved.parents:
                    continue
                tar.add(child, arcname=child.relative_to(workspace).as_posix(), recursive=False)
        else:
            workspace_dev = os.lstat(workspace).st_dev
            for base, dirnames, filenames in os.walk(workspace, topdown=True, followlinks=False):
                base_path = Path(base)
                kept_dirs: list[str] = []
                for name in dirnames:
                    child = base_path / name
                    try:
                        resolved = child.resolve(strict=False)
                    except OSError:
                        resolved = child.absolute()
                    if resolved == backup_root or backup_root in resolved.parents:
                        continue
                    try:
                        if os.lstat(child).st_dev != workspace_dev:
                            continue
                    except OSError:
                        continue
                    if child.is_symlink():
                        tar.add(child, arcname=child.relative_to(workspace).as_posix(), recursive=False)
                        continue
                    kept_dirs.append(name)
                dirnames[:] = kept_dirs
                if base_path != workspace:
                    tar.add(base_path, arcname=base_path.relative_to(workspace).as_posix(), recursive=False)
                for name in filenames:
                    child = base_path / name
                    try:
                        resolved = child.resolve(strict=False)
                    except OSError:
                        resolved = child.absolute()
                    if resolved == backup_root or backup_root in resolved.parents:
                        continue
                    try:
                        if os.lstat(child).st_dev != workspace_dev:
                            continue
                    except OSError:
                        continue
                    tar.add(child, arcname=child.relative_to(workspace).as_posix(), recursive=False)
    preview = action / "recovery-preview"
    _document(action, payload, [
        f"mkdir -p -- {shlex.quote(str(preview))}",
        f"tar -xzf {shlex.quote(str(archive))} -C {shlex.quote(str(preview))}",
        f"diff -ruN -- {shlex.quote(str(workspace))} {shlex.quote(str(preview))}  # inspect before restore",
    ])
    return archive
