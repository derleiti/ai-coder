from __future__ import annotations
import hashlib, os, re, subprocess, time
from pathlib import Path
from typing import Any, Dict

IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"}

ACTIVE_WORKSPACE_ENV = "AICODER_ACTIVE_WORKSPACE"
DEFAULT_PROJECTS_ROOT = Path.home() / "workspace"


def projects_root(configured: str | Path | None = None) -> Path:
    """Return the project-container directory; it is not itself an active project."""
    raw = configured or DEFAULT_PROJECTS_ROOT
    return Path(raw).expanduser().resolve(strict=False)


def active_workspace(configured: str | None = None) -> Path:
    """Return the active project, preferring the process-local selected workspace."""
    raw = os.environ.get(ACTIVE_WORKSPACE_ENV) or configured or os.getcwd()
    return Path(raw).expanduser().resolve(strict=False)


def validate_project_workspace(path: str | Path, configured_projects_root: str | Path | None = None) -> Path:
    """Reject the projects container when a concrete project workspace is required."""
    root = Path(path).expanduser().resolve(strict=False)
    container = projects_root(configured_projects_root)
    if root == container:
        raise ValueError(
            f"active workspace points to projects container {container}; "
            "select or create a concrete project before starting a coding team run"
        )
    return root


def _task_project_path(task: str, container: Path) -> Path | None:
    """Find an explicit path in the task that stays below projects_root."""
    for raw in re.findall(r"(?:~|/)[^\s`\"'<>|]+", str(task or "")):
        candidate = Path(raw.rstrip(".,;:!?)]}")).expanduser().resolve(strict=False)
        try:
            relative = candidate.relative_to(container)
        except ValueError:
            continue
        if candidate != container and relative.parts:
            return candidate
    return None


def resolve_or_create_project_workspace(
    path: str | Path, task: str, configured_projects_root: str | Path | None = None,
) -> tuple[Path, bool, str]:
    """Use a concrete project; create one automatically when only projects_root is active."""
    current = Path(path).expanduser().resolve(strict=False)
    container = projects_root(configured_projects_root)
    if current != container:
        return validate_project_workspace(current, container), False, "selected-project"

    container.mkdir(parents=True, exist_ok=True)
    target = _task_project_path(task, container)
    reason = "task-project-path"
    if target is None:
        digest = hashlib.sha256(str(task or "").encode("utf-8", errors="ignore")).hexdigest()[:8]
        base = f"aicoder-project-{time.strftime('%Y%m%d-%H%M%S')}-{digest}"
        target = container / base
        reason = "generated-project-root"
        suffix = 2
        while target.exists() and any(target.iterdir()):
            target = container / f"{base}-{suffix}"
            suffix += 1

    target = target.expanduser().resolve(strict=False)
    try:
        target.relative_to(container)
    except ValueError as exc:
        raise ValueError(f"resolved project workspace escapes projects root {container}: {target}") from exc
    target.mkdir(parents=True, exist_ok=True)
    return target, True, reason


def sync_active_workspace(path: str | Path | None) -> Path:
    """Synchronize the process-local workspace cache with a persisted selection."""
    if path is None or not str(path).strip():
        os.environ.pop(ACTIVE_WORKSPACE_ENV, None)
        return active_workspace()
    return activate_workspace(path)


def activate_workspace(path: str | Path | None = None) -> Path:
    """Set the process-local workspace without requiring a Git repository."""
    root = Path(path or os.getcwd()).expanduser().resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"workspace is not an existing directory: {root}")
    os.environ[ACTIVE_WORKSPACE_ENV] = str(root)
    return root


def path_within_workspace(value: str | Path, root: str | Path | None = None) -> tuple[Path, bool]:
    workspace = (Path(root).expanduser().resolve(strict=False) if root is not None else active_workspace())
    raw = Path(str(value or ".")).expanduser()
    candidate = raw if raw.is_absolute() else workspace / raw
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace)
        return resolved, True
    except ValueError:
        return resolved, False

def detect_git_root(start: Path) -> Path | None:
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return None

def safe_git(cmd: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=5)
        out = proc.stdout.strip() or proc.stderr.strip()
        return out[:4000]
    except Exception as e:
        return f"git call failed: {e}"

def workspace_snapshot(path: str | None = None) -> Dict[str, Any]:
    root = Path(path or os.getcwd()).resolve()
    git_root = detect_git_root(root)
    files = 0
    dirs = 0
    sample = []
    try:
        for entry in root.iterdir():
            if entry.name in IGNORE_DIRS:
                continue
            if entry.is_dir():
                dirs += 1
            else:
                files += 1
            sample.append(entry.name)
            if len(sample) >= 20:
                break
    except Exception as e:
        sample = [f"scan failed: {e}"]
    result = {
        "cwd": str(root),
        "git_root": str(git_root) if git_root else None,
        "is_git_repo": bool(git_root),
        "top_level_dirs": dirs,
        "top_level_files": files,
        "sample_entries": sample,
    }
    if git_root:
        result["git_status_short"] = safe_git(["git", "status", "--short"], git_root)
        result["git_branch"] = safe_git(["git", "branch", "--show-current"], git_root)
        result["git_last_commit"] = safe_git(["git", "log", "-1", "--oneline"], git_root)
    return result
