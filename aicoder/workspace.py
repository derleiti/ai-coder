from __future__ import annotations
import os, subprocess
from pathlib import Path
from typing import Any, Dict

IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"}

ACTIVE_WORKSPACE_ENV = "AICODER_ACTIVE_WORKSPACE"


def active_workspace(configured: str | None = None) -> Path:
    """Return the process-local workspace, preferring the directory AICoder started in."""
    raw = os.environ.get(ACTIVE_WORKSPACE_ENV) or configured or os.getcwd()
    return Path(raw).expanduser().resolve(strict=False)


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
