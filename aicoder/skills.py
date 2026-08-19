"""Native, read-only skill discovery for AICoder.

Skills are instruction bundles, not executable plugins. A skill lives in
``<root>/<skill-name>/SKILL.md`` and is loaded on demand through the local
``skill_read`` tool. Only a bounded catalog is added to the system prompt.

Precedence, from lowest to highest:
1. ``~/.config/ai-coder/skills``
2. ``<workspace>/.agents/skills``
3. ``<workspace>/.aicoder/skills``

A workspace skill may intentionally shadow a global skill with the same name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import CONFIG_DIR

SKILL_FILE = "SKILL.md"
MAX_SKILLS = 64
MAX_SKILL_BYTES = 64 * 1024
MAX_SKILL_DESCRIPTION = 240
_MAX_DIRS_PER_ROOT = 128
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    scope: str
    path: Path

    @property
    def key(self) -> str:
        return self.name.casefold()


def _skill_roots(workspace: str | Path, config_dir: Path | None = None) -> list[tuple[str, Path]]:
    ws = Path(workspace or ".").expanduser().resolve(strict=False)
    config = Path(config_dir) if config_dir is not None else CONFIG_DIR
    return [
        ("global", config / "skills"),
        ("workspace-agents", ws / ".agents" / "skills"),
        ("workspace-aicoder", ws / ".aicoder" / "skills"),
    ]


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_skill_file(root: Path, skill_dir: Path) -> Path | None:
    try:
        root_resolved = root.resolve(strict=True)
        dir_resolved = skill_dir.resolve(strict=True)
    except OSError:
        return None
    if not _within(dir_resolved, root_resolved):
        return None
    candidate = dir_resolved / SKILL_FILE
    try:
        resolved = candidate.resolve(strict=True)
        stat = resolved.stat()
    except OSError:
        return None
    if not resolved.is_file() or not _within(resolved, root_resolved):
        return None
    if stat.st_size > MAX_SKILL_BYTES:
        return None
    return resolved


def _frontmatter_and_body(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    header = text[4:end]
    body = text[end + 5 :]
    data: dict[str, str] = {}
    for raw in header.splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip().lower()
        if key not in {"name", "description"}:
            continue
        value = value.strip().strip('"\'')
        if value:
            data[key] = value
    return data, body


def _fallback_description(body: str) -> str:
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^#{1,6}\s*", "", line).strip()
        if line:
            return line[:MAX_SKILL_DESCRIPTION]
    return "No description provided"


def _load_spec(scope: str, root: Path, skill_dir: Path) -> SkillSpec | None:
    folder_name = skill_dir.name
    if not _SKILL_NAME_RE.fullmatch(folder_name):
        return None
    skill_file = _safe_skill_file(root, skill_dir)
    if skill_file is None:
        return None
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    metadata, body = _frontmatter_and_body(text)
    declared_name = metadata.get("name", folder_name).strip()
    if declared_name.casefold() != folder_name.casefold():
        # The directory name is the stable lookup key. Reject mismatched
        # declarations instead of creating aliases with surprising precedence.
        return None
    description = metadata.get("description") or _fallback_description(body)
    description = " ".join(description.split())[:MAX_SKILL_DESCRIPTION]
    return SkillSpec(
        name=folder_name,
        description=description or "No description provided",
        scope=scope,
        path=skill_file,
    )


def _iter_skill_dirs(root: Path) -> Iterable[Path]:
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return []
    return [entry for entry in entries[:_MAX_DIRS_PER_ROOT] if entry.is_dir()]


def discover_skills(
    workspace: str | Path,
    *,
    config_dir: Path | None = None,
    limit: int = MAX_SKILLS,
) -> list[SkillSpec]:
    """Discover the bounded effective skill catalog for a workspace."""
    effective: dict[str, SkillSpec] = {}
    for scope, root in _skill_roots(workspace, config_dir=config_dir):
        for skill_dir in _iter_skill_dirs(root):
            spec = _load_spec(scope, root, skill_dir)
            if spec is not None:
                effective[spec.key] = spec
    bounded = max(1, min(MAX_SKILLS, int(limit)))
    return sorted(effective.values(), key=lambda item: item.name.casefold())[:bounded]


def render_skill_catalog(workspace: str | Path, *, config_dir: Path | None = None) -> str:
    skills = discover_skills(workspace, config_dir=config_dir)
    if not skills:
        return ""
    lines = ["## Available Skills", "Load a relevant skill with skill_read(name) before applying it."]
    for skill in skills:
        lines.append(f"- {skill.name} [{skill.scope}]: {skill.description}")
    return "\n".join(lines)[:6000]


def read_skill(
    workspace: str | Path,
    name: str,
    *,
    config_dir: Path | None = None,
) -> tuple[str, bool]:
    """Read one discovered skill by stable name; arbitrary paths are impossible."""
    requested = str(name or "").strip()
    if not _SKILL_NAME_RE.fullmatch(requested):
        return "skill_read: invalid skill name", True
    catalog = {skill.key: skill for skill in discover_skills(workspace, config_dir=config_dir)}
    skill = catalog.get(requested.casefold())
    if skill is None:
        return f"skill_read: unknown skill '{requested}'", True
    try:
        text = skill.path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return f"skill_read: could not read '{skill.name}': {exc}", True
    if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
        return f"skill_read: skill '{skill.name}' exceeds {MAX_SKILL_BYTES} bytes", True
    metadata, body = _frontmatter_and_body(text)
    description = metadata.get("description") or skill.description
    result = (
        f"Skill: {skill.name}\n"
        f"Scope: {skill.scope}\n"
        f"Description: {' '.join(description.split())[:MAX_SKILL_DESCRIPTION]}\n\n"
        f"{body.strip()}"
    )
    return result[:MAX_SKILL_BYTES], False
