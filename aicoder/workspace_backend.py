"""Transactional execution workspaces for the experimental AICoder runtime.

DiskWorkspace preserves the historical direct-on-disk behaviour. RamWorkspace
creates an isolated working tree on a real RAM filesystem, keeps Git metadata
independent, checkpoints only changed files for resumability, and writes a
verified result back with per-file atomic replacement plus rollback.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Iterable
import uuid

from .config import CONFIG_DIR

WORKSPACE_MODES = frozenset({"auto", "ram", "disk"})
_MANIFEST_FILE = ".aicoder-checkpoint.json"
_INTERNAL_NAMES = frozenset({_MANIFEST_FILE, ".aicoder-team"})
_TRANSIENT_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox"})
_MIN_RAM_RESERVE = 512 * 1024 * 1024
_RAM_OVERHEAD = 64 * 1024 * 1024


class WorkspaceError(RuntimeError):
    pass


class WorkspaceConflict(WorkspaceError):
    pass


@dataclass(frozen=True)
class WorkspaceInfo:
    mode: str
    source_root: Path
    execution_root: Path
    volatile: bool = False
    transactional: bool = False
    requested_mode: str = "disk"
    fallback_reason: str = ""
    estimated_bytes: int = 0
    safe_budget_bytes: int = 0
    restored_checkpoint: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "requested_mode": self.requested_mode,
            "source_root": str(self.source_root),
            "execution_root": str(self.execution_root),
            "volatile": self.volatile,
            "transactional": self.transactional,
            "fallback_reason": self.fallback_reason,
            "estimated_bytes": self.estimated_bytes,
            "safe_budget_bytes": self.safe_budget_bytes,
            "restored_checkpoint": self.restored_checkpoint,
        }


@dataclass(frozen=True)
class _Entry:
    kind: str
    digest: str = ""
    mode: int = 0
    size: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "digest": self.digest, "mode": self.mode, "size": self.size}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "_Entry":
        return cls(
            kind=str(data.get("kind") or ""), digest=str(data.get("digest") or ""),
            mode=int(data.get("mode") or 0), size=int(data.get("size") or 0),
        )


class WorkspaceBackend(ABC):
    @property
    @abstractmethod
    def info(self) -> WorkspaceInfo:
        raise NotImplementedError

    @abstractmethod
    def prepare(self) -> Path:
        raise NotImplementedError

    @abstractmethod
    def finalize(self, *, verified: bool) -> None:
        raise NotImplementedError

    def checkpoint(self, plan_id: str) -> Path | None:
        return None

    def clear_checkpoint(self, plan_id: str) -> None:
        return None

    @abstractmethod
    def abort(self) -> None:
        raise NotImplementedError


class DiskWorkspace(WorkspaceBackend):
    """Compatibility backend: tools operate directly on the user's workspace."""

    def __init__(self, root: str | Path, *, requested_mode: str = "disk", fallback_reason: str = ""):
        resolved = Path(root).expanduser().resolve(strict=False)
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError(f"workspace is not an existing directory: {resolved}")
        self._info = WorkspaceInfo(
            mode="disk", requested_mode=requested_mode, source_root=resolved,
            execution_root=resolved, volatile=False, transactional=False,
            fallback_reason=fallback_reason,
        )

    @property
    def info(self) -> WorkspaceInfo:
        return self._info

    def prepare(self) -> Path:
        return self._info.execution_root

    def finalize(self, *, verified: bool) -> None:
        return None

    def abort(self) -> None:
        return None


def _mem_available_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _ram_candidates() -> list[Path]:
    rows: list[Path] = []
    env = os.environ.get("AICODER_RAM_ROOT")
    if env:
        rows.append(Path(env).expanduser())
    rows.append(Path("/dev/shm"))
    if hasattr(os, "getuid"):
        rows.append(Path(f"/run/user/{os.getuid()}"))
    unique: list[Path] = []
    seen: set[str] = set()
    for row in rows:
        key = str(row)
        if key not in seen:
            seen.add(key); unique.append(row)
    return unique


def _select_ram_root() -> tuple[Path | None, int]:
    best: tuple[Path | None, int] = (None, 0)
    for root in _ram_candidates():
        try:
            if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
                continue
            free = int(shutil.disk_usage(root).free)
        except OSError:
            continue
        if free > best[1]:
            best = (root, free)
    return best


def _is_git_workspace(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _iter_tree(root: Path, *, include_git: bool = False) -> Iterable[tuple[Path, str]]:
    """Yield path + relative POSIX path without following directory symlinks."""
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            rel = Path(entry.path).relative_to(root).as_posix()
            top = rel.split("/", 1)[0]
            if (not include_git and top == ".git") or top in _INTERNAL_NAMES or entry.name in _TRANSIENT_DIRS:
                continue
            path = Path(entry.path)
            yield path, rel
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
            except OSError:
                continue


def _estimate_tree_bytes(root: Path, *, include_git: bool = False) -> int:
    total = 0
    for path, _ in _iter_tree(root, include_git=include_git):
        try:
            st = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            total += int(st.st_size)
    return total


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> _Entry | None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkspaceError(f"cannot inspect {path}: {exc}") from exc
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISLNK(st.st_mode):
        return _Entry("symlink", hashlib.sha256(os.readlink(path).encode()).hexdigest(), mode, 0)
    if stat.S_ISDIR(st.st_mode):
        return _Entry("dir", "", mode, 0)
    if stat.S_ISREG(st.st_mode):
        return _Entry("file", _sha256(path), mode, int(st.st_size))
    return _Entry("other", "", mode, int(st.st_size))


def _manifest(root: Path) -> dict[str, _Entry]:
    rows: dict[str, _Entry] = {}
    for path, rel in _iter_tree(root, include_git=False):
        item = _fingerprint(path)
        if item is not None:
            rows[rel] = item
    return rows


def _manifest_signature(rows: dict[str, _Entry]) -> str:
    payload = json.dumps(
        {key: value.as_dict() for key, value in sorted(rows.items())},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _copy_working_tree(source: Path, destination: Path, *, git_workspace: bool) -> None:
    """Create an independent execution tree.

    Git repositories get private metadata. A shared local clone reuses immutable
    object data but all refs/index/worktree metadata and new objects live in RAM.
    This is especially important for Git worktrees where `.git` is a pointer to
    metadata outside the working directory.
    """
    if git_workspace:
        proc = subprocess.run(
            ["git", "clone", "--shared", "--no-checkout", "--quiet", str(source), str(destination)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            raise WorkspaceError(f"RAM Git isolation failed: {proc.stderr.strip() or proc.stdout.strip()}")
        shutil.copytree(
            source, destination, dirs_exist_ok=True, symlinks=True,
            ignore=shutil.ignore_patterns(".git"),
        )
        # Populate the private index from HEAD without touching copied files.
        subprocess.run(
            ["git", "-C", str(destination), "reset", "--mixed", "--quiet", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return
    shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)


def _safe_plan_id(value: str) -> str:
    text = str(value or "").strip()
    if not text or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in text):
        raise ValueError("invalid plan id")
    return text


def _checkpoint_dir(source: Path) -> Path:
    key = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    path = CONFIG_DIR / "ram-checkpoints" / key
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700); os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _checkpoint_path(source: Path, plan_id: str) -> Path:
    return _checkpoint_dir(source) / f"{_safe_plan_id(plan_id)}.tar.gz"


def _safe_tar_member(name: str) -> bool:
    pure = PurePosixPath(name)
    return bool(name) and not pure.is_absolute() and ".." not in pure.parts


class RamWorkspace(WorkspaceBackend):
    """Isolated RAM-backed working tree with checkpoint and verified commit."""

    def __init__(
        self,
        root: str | Path,
        *,
        ram_root: str | Path | None = None,
        requested_mode: str = "ram",
        estimated_bytes: int = 0,
        safe_budget_bytes: int = 0,
        checkpoint_id: str | None = None,
    ):
        source = Path(root).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ValueError(f"workspace is not an existing directory: {source}")
        chosen = Path(ram_root).expanduser() if ram_root else _select_ram_root()[0]
        if chosen is None or not chosen.is_dir():
            raise WorkspaceError("no writable RAM filesystem is available")
        base = chosen / f"aicoder-{os.getuid() if hasattr(os, 'getuid') else 'user'}"
        base.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
        execution = Path(tempfile.mkdtemp(prefix="workspace-", dir=base))
        self._source = source
        self._execution = execution
        self._git_workspace = _is_git_workspace(source)
        self._checkpoint_id = checkpoint_id
        self._baseline: dict[str, _Entry] = {}
        self._prepared = False
        self._closed = False
        self._restored_checkpoint = False
        self._info = WorkspaceInfo(
            mode="ram", requested_mode=requested_mode,
            source_root=source, execution_root=execution,
            volatile=True, transactional=True,
            estimated_bytes=int(estimated_bytes), safe_budget_bytes=int(safe_budget_bytes),
        )

    @property
    def info(self) -> WorkspaceInfo:
        return self._info

    def prepare(self) -> Path:
        if self._prepared:
            return self._execution
        try:
            _copy_working_tree(self._source, self._execution, git_workspace=self._git_workspace)
            self._baseline = _manifest(self._execution)
            if self._checkpoint_id:
                self._restore_checkpoint(self._checkpoint_id)
            self._prepared = True
            self._info = replace(self._info, restored_checkpoint=self._restored_checkpoint)
            return self._execution
        except Exception:
            self.abort()
            raise

    def delta_summary(self) -> dict[str, Any]:
        current, changed, deleted = self._delta()
        return {
            "changed": sorted(changed),
            "deleted": sorted(deleted),
            "changed_count": len(changed),
            "deleted_count": len(deleted),
            "current_entries": len(current),
        }

    def seed_from(self, other_root: str | Path) -> None:
        """Replace RAM working-tree content from another candidate, keeping private .git metadata."""
        if not self._prepared:
            raise WorkspaceError("RAM workspace was not prepared")
        source = Path(other_root).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise WorkspaceError(f"candidate seed is not a directory: {source}")
        for path, rel in sorted(
            list(_iter_tree(self._execution, include_git=False)),
            key=lambda item: (item[1].count("/"), item[1]), reverse=True,
        ):
            self._remove_path(path)
        shutil.copytree(
            source, self._execution, dirs_exist_ok=True, symlinks=True,
            ignore=shutil.ignore_patterns(".git", *_INTERNAL_NAMES),
        )

    def write_candidate_artifact(self, relative_path: str, content: str) -> Path:
        """Write orchestration evidence inside RAM without exposing the persistent source workspace."""
        if not self._prepared:
            raise WorkspaceError("RAM workspace was not prepared")
        rel = PurePosixPath(str(relative_path))
        if rel.is_absolute() or ".." in rel.parts:
            raise WorkspaceError("unsafe candidate artifact path")
        target = self._execution.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        return target

    def _delta(self) -> tuple[dict[str, _Entry], set[str], set[str]]:
        if not self._prepared:
            raise WorkspaceError("RAM workspace was not prepared")
        current = _manifest(self._execution)
        changed = {
            rel for rel, entry in current.items()
            if rel not in self._baseline or self._baseline[rel] != entry
        }
        deleted = set(self._baseline).difference(current)
        return current, changed, deleted

    def _assert_source_unchanged(self, affected: set[str]) -> None:
        conflicts: list[str] = []
        for rel in sorted(affected):
            expected = self._baseline.get(rel)
            actual = _fingerprint(self._source / rel)
            if expected is None:
                if actual is not None:
                    conflicts.append(rel)
            elif actual != expected:
                conflicts.append(rel)
            if len(conflicts) >= 8:
                break
        if conflicts:
            raise WorkspaceConflict(
                "source workspace changed outside the RAM transaction: " + ", ".join(conflicts)
            )

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)

    @staticmethod
    def _atomic_install(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        source_entry = _fingerprint(source)
        if source_entry is None:
            raise WorkspaceError(f"RAM source disappeared before commit: {source}")
        if source_entry.kind == "dir":
            if target.exists() and not target.is_dir():
                RamWorkspace._remove_path(target)
            target.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(target, source_entry.mode)
            except OSError:
                pass
            return
        if target.exists() and target.is_dir() and not target.is_symlink():
            RamWorkspace._remove_path(target)
        token = f".aicoder-{uuid.uuid4().hex}.tmp"
        temp = target.parent / token
        try:
            if source_entry.kind == "symlink":
                os.symlink(os.readlink(source), temp)
            elif source_entry.kind == "file":
                shutil.copy2(source, temp, follow_symlinks=False)
            else:
                raise WorkspaceError(f"unsupported workspace entry type: {source}")
            os.replace(temp, target)
        finally:
            if temp.exists() or temp.is_symlink():
                RamWorkspace._remove_path(temp)

    def _backup_affected(self, affected: set[str], backup: Path) -> None:
        for rel in sorted(affected, key=lambda item: (item.count("/"), item)):
            source = self._source / rel
            entry = _fingerprint(source)
            if entry is None:
                continue
            target = backup / rel
            if entry.kind == "dir":
                target.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(target, entry.mode)
                except OSError:
                    pass
            elif entry.kind == "symlink":
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(os.readlink(source), target)
            elif entry.kind == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, target, follow_symlinks=False)
                except (OSError, TypeError):
                    shutil.copy2(source, target, follow_symlinks=False)

    def _rollback(self, affected: set[str], backup: Path) -> None:
        # Remove partial committed state first, deepest paths first.
        for rel in sorted(affected, key=lambda item: (item.count("/"), item), reverse=True):
            self._remove_path(self._source / rel)
        # Restore directories first and files afterwards.
        if not backup.exists():
            return
        for path, rel in sorted(_iter_tree(backup, include_git=True), key=lambda item: item[1].count("/")):
            target = self._source / rel
            self._atomic_install(path, target)

    def finalize(self, *, verified: bool) -> None:
        if not verified:
            raise WorkspaceError("refusing to persist an unverified RAM workspace")
        current, changed, deleted = self._delta()
        affected = set(changed) | set(deleted)
        # Git metadata is excluded by manifest construction and can never leak back.
        if not affected:
            self.abort()
            return
        self._assert_source_unchanged(affected)

        txn = self._source.parent / f".aicoder-txn-{uuid.uuid4().hex}"
        backup = txn / "backup"
        backup.mkdir(parents=True, exist_ok=False)
        try:
            self._backup_affected(affected, backup)
            # Create/replace directories shallow-first, then files/symlinks.
            changed_dirs = [rel for rel in changed if current.get(rel) and current[rel].kind == "dir"]
            for rel in sorted(changed_dirs, key=lambda item: item.count("/")):
                self._atomic_install(self._execution / rel, self._source / rel)
            for rel in sorted(changed.difference(changed_dirs)):
                self._atomic_install(self._execution / rel, self._source / rel)
            # Delete absent entries deepest-first so directories become empty last.
            for rel in sorted(deleted, key=lambda item: (item.count("/"), item), reverse=True):
                self._remove_path(self._source / rel)
        except Exception as exc:
            try:
                self._rollback(affected, backup)
            except Exception as rollback_exc:
                raise WorkspaceError(
                    f"RAM commit failed ({exc}); rollback also failed ({rollback_exc}). Backup kept at {txn}"
                ) from exc
            raise WorkspaceError(f"RAM commit failed and was rolled back: {exc}") from exc
        else:
            shutil.rmtree(txn, ignore_errors=True)
            if self._checkpoint_id:
                self.clear_checkpoint(self._checkpoint_id)
            self.abort()

    def checkpoint(self, plan_id: str) -> Path | None:
        if not self._prepared:
            return None
        current, changed, deleted = self._delta()
        plan = _safe_plan_id(plan_id)
        target = _checkpoint_path(self._source, plan)
        temp = target.with_suffix(target.suffix + f".{uuid.uuid4().hex}.tmp")
        metadata = {
            "schema": 1,
            "source": str(self._source),
            "base_signature": _manifest_signature(self._baseline),
            "deleted": sorted(deleted),
            "changed": sorted(changed),
        }
        try:
            with tarfile.open(temp, "w:gz") as archive:
                payload = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
                info = tarfile.TarInfo(_MANIFEST_FILE)
                info.size = len(payload); info.mode = 0o600
                import io
                archive.addfile(info, io.BytesIO(payload))
                for rel in sorted(changed):
                    path = self._execution / rel
                    if path.exists() or path.is_symlink():
                        archive.add(path, arcname=rel, recursive=False)
            os.replace(temp, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            self._checkpoint_id = plan
            return target
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)

    def _restore_checkpoint(self, plan_id: str) -> None:
        target = _checkpoint_path(self._source, plan_id)
        if not target.is_file():
            return
        with tarfile.open(target, "r:gz") as archive:
            try:
                member = archive.getmember(_MANIFEST_FILE)
            except KeyError as exc:
                raise WorkspaceError("RAM checkpoint has no metadata") from exc
            handle = archive.extractfile(member)
            if handle is None:
                raise WorkspaceError("RAM checkpoint metadata is unreadable")
            metadata = json.loads(handle.read().decode("utf-8"))
            if metadata.get("source") != str(self._source):
                raise WorkspaceConflict("RAM checkpoint belongs to another workspace")
            if metadata.get("base_signature") != _manifest_signature(self._baseline):
                raise WorkspaceConflict("source changed since the RAM checkpoint was created")
            deleted = [str(item) for item in metadata.get("deleted", [])]
            for rel in deleted:
                if not _safe_tar_member(rel):
                    raise WorkspaceError("unsafe path in RAM checkpoint")
                self._remove_path(self._execution / rel)
            for member in archive.getmembers():
                if member.name == _MANIFEST_FILE:
                    continue
                if not _safe_tar_member(member.name):
                    raise WorkspaceError("unsafe path in RAM checkpoint")
                destination = self._execution / member.name
                self._remove_path(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif member.issym():
                    os.symlink(member.linkname, destination)
                elif member.isfile():
                    source = archive.extractfile(member)
                    if source is None:
                        raise WorkspaceError(f"checkpoint member unreadable: {member.name}")
                    with destination.open("wb") as out:
                        shutil.copyfileobj(source, out)
                    try:
                        os.chmod(destination, member.mode)
                    except OSError:
                        pass
                else:
                    raise WorkspaceError(f"unsupported checkpoint member: {member.name}")
        self._restored_checkpoint = True

    def clear_checkpoint(self, plan_id: str) -> None:
        try:
            _checkpoint_path(self._source, plan_id).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self._execution, ignore_errors=True)


def create_workspace_backend(
    root: str | Path,
    mode: str = "auto",
    *,
    checkpoint_id: str | None = None,
) -> WorkspaceBackend:
    source = Path(root).expanduser().resolve(strict=False)
    requested = str(mode or "auto").strip().lower()
    if requested not in WORKSPACE_MODES:
        raise ValueError(f"workspace mode must be one of: {', '.join(sorted(WORKSPACE_MODES))}")
    if requested == "disk":
        return DiskWorkspace(source, requested_mode=requested)

    ram_root, ram_free = _select_ram_root()
    mem_available = _mem_available_bytes()
    estimated = _estimate_tree_bytes(source, include_git=False)
    # Keep half of currently available memory untouched and reserve additional
    # headroom for model/tool subprocesses. The filesystem's own free capacity
    # is a second hard ceiling.
    memory_budget = max(0, int(mem_available * 0.50) - _MIN_RAM_RESERVE) if mem_available else 0
    fs_budget = max(0, int(ram_free * 0.80))
    safe_budget = min(value for value in (memory_budget, fs_budget) if value > 0) if (memory_budget > 0 and fs_budget > 0) else 0
    required = int(estimated * 1.35) + _RAM_OVERHEAD

    reason = ""
    if ram_root is None:
        reason = "no writable RAM filesystem available"
    elif safe_budget <= 0:
        reason = "RAM availability could not be established safely"
    elif required > safe_budget:
        reason = f"estimated RAM need {required} exceeds safe budget {safe_budget}"

    if reason:
        return DiskWorkspace(source, requested_mode=requested, fallback_reason=reason)

    try:
        return RamWorkspace(
            source, ram_root=ram_root, requested_mode=requested,
            estimated_bytes=required, safe_budget_bytes=safe_budget,
            checkpoint_id=checkpoint_id,
        )
    except (OSError, WorkspaceError) as exc:
        return DiskWorkspace(source, requested_mode=requested, fallback_reason=str(exc))


def resolve_resume_checkpoint(
    source_root: str | Path,
    *,
    resume: bool,
    resume_plan_id: str | None = None,
) -> str | None:
    """Resolve the persistent plan id whose RAM delta should be restored."""
    if not resume:
        return None
    if resume_plan_id and resume_plan_id != "current":
        return _safe_plan_id(resume_plan_id)
    try:
        from .agent_plan import PlanStore
        plan = PlanStore().load_current(str(Path(source_root).expanduser().resolve(strict=False)))
    except Exception:
        return None
    if plan is None or plan.status not in {"running", "paused", "failed"}:
        return None
    return plan.id


def open_workspace_for_run(
    source_root: str | Path,
    mode: str,
    *,
    resume: bool = False,
    resume_plan_id: str | None = None,
) -> WorkspaceBackend:
    """Create and prepare a backend, falling back to disk on RAM setup failure."""
    checkpoint_id = resolve_resume_checkpoint(
        source_root, resume=resume, resume_plan_id=resume_plan_id,
    )
    backend = create_workspace_backend(source_root, mode, checkpoint_id=checkpoint_id)
    try:
        backend.prepare()
        return backend
    except Exception as exc:
        backend.abort()
        disk = DiskWorkspace(
            source_root, requested_mode=str(mode or "auto"),
            fallback_reason=f"RAM preparation failed: {type(exc).__name__}: {exc}",
        )
        disk.prepare()
        return disk


def preserve_workspace_for_resume(backend: WorkspaceBackend, plan_id: str | None) -> Path | None:
    """Persist only the volatile delta of an unfinished RAM run, then release RAM."""
    checkpoint = None
    try:
        if backend.info.mode == "ram" and plan_id:
            checkpoint = backend.checkpoint(plan_id)
        return checkpoint
    finally:
        backend.abort()
