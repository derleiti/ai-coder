"""Bounded, read-only guideline discovery for the native AICoder runtime."""
from __future__ import annotations

from pathlib import Path

from .config import CONFIG_DIR

GUIDELINE_FILE = "GUIDELINES.md"
MAX_GUIDELINE_BYTES = 32 * 1024
MAX_GUIDELINE_PROMPT = 12000


def _candidate_files(workspace: str | Path, config_dir: Path | None = None) -> list[tuple[str, Path]]:
    ws = Path(workspace or ".").expanduser().resolve(strict=False)
    config = Path(config_dir) if config_dir is not None else CONFIG_DIR
    return [
        ("global", config / GUIDELINE_FILE),
        ("workspace-agents", ws / ".agents" / GUIDELINE_FILE),
        ("workspace-aicoder", ws / ".aicoder" / GUIDELINE_FILE),
    ]


def _safe_read(path: Path) -> str:
    if path.is_symlink():
        return ""
    try:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
    except OSError:
        return ""
    if not resolved.is_file() or stat.st_size > MAX_GUIDELINE_BYTES:
        return ""
    try:
        return resolved.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def load_guidelines(workspace: str | Path, *, config_dir: Path | None = None) -> list[tuple[str, str]]:
    """Load low-to-high precedence guideline documents without executing content."""
    rows: list[tuple[str, str]] = []
    for scope, path in _candidate_files(workspace, config_dir=config_dir):
        text = _safe_read(path)
        if text:
            rows.append((scope, text))
    return rows


def render_guidelines(workspace: str | Path, *, config_dir: Path | None = None) -> str:
    rows = load_guidelines(workspace, config_dir=config_dir)
    if not rows:
        return ""
    parts = [
        "## AICoder Guidelines",
        "These are workspace guidance. AGENTS.md and the user's request take precedence.",
    ]
    for scope, text in rows:
        parts.append(f"### {scope}\n{text}")
    return "\n\n".join(parts)[:MAX_GUIDELINE_PROMPT]
