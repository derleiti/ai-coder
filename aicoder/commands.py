"""Native prompt-command discovery and expansion for AICoder.

Commands are inert markdown templates. They never execute shell or tools by
being loaded; expansion produces a normal user prompt for the native agent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import CONFIG_DIR

COMMAND_FILE_SUFFIX = ".md"
MAX_COMMANDS = 64
MAX_COMMAND_BYTES = 32 * 1024
MAX_COMMAND_DESCRIPTION = 240
MAX_COMMAND_ARGS = 4000
_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    scope: str
    path: Path

    @property
    def key(self) -> str:
        return self.name.casefold()


def _roots(workspace: str | Path, config_dir: Path | None = None) -> list[tuple[str, Path]]:
    ws = Path(workspace or ".").expanduser().resolve(strict=False)
    config = Path(config_dir) if config_dir is not None else CONFIG_DIR
    return [
        ("global", config / "commands"),
        ("workspace-agents", ws / ".agents" / "commands"),
        ("workspace-aicoder", ws / ".aicoder" / "commands"),
    ]


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    data: dict[str, str] = {}
    for raw in text[4:end].splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        if key.strip().lower() == "description":
            value = value.strip().strip('"\'')
            if value:
                data["description"] = value
    return data, text[end + 5 :]


def _description(body: str) -> str:
    for raw in body.splitlines():
        line = re.sub(r"^#{1,6}\s*", "", raw.strip()).strip()
        if line:
            return " ".join(line.split())[:MAX_COMMAND_DESCRIPTION]
    return "No description provided"


def _iter_files(root: Path) -> Iterable[Path]:
    if root.is_symlink():
        return []
    try:
        return sorted(
            (path for path in root.iterdir() if path.is_file() and path.suffix.lower() == COMMAND_FILE_SUFFIX),
            key=lambda path: path.name.casefold(),
        )[:128]
    except OSError:
        return []


def _load_spec(scope: str, root: Path, path: Path) -> CommandSpec | None:
    name = path.stem
    if not _COMMAND_NAME_RE.fullmatch(name):
        return None
    try:
        root_resolved = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root_resolved)
        stat = resolved.stat()
    except (OSError, ValueError):
        return None
    if stat.st_size > MAX_COMMAND_BYTES:
        return None
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    meta, body = _frontmatter(text)
    return CommandSpec(
        name=name,
        description=(meta.get("description") or _description(body))[:MAX_COMMAND_DESCRIPTION],
        scope=scope,
        path=resolved,
    )


def discover_commands(
    workspace: str | Path,
    *,
    config_dir: Path | None = None,
    limit: int = MAX_COMMANDS,
) -> list[CommandSpec]:
    effective: dict[str, CommandSpec] = {}
    for scope, root in _roots(workspace, config_dir=config_dir):
        for path in _iter_files(root):
            spec = _load_spec(scope, root, path)
            if spec is not None:
                effective[spec.key] = spec
    bounded = max(1, min(MAX_COMMANDS, int(limit)))
    return sorted(effective.values(), key=lambda item: item.name.casefold())[:bounded]


def read_command(
    workspace: str | Path,
    name: str,
    *,
    config_dir: Path | None = None,
) -> tuple[str, bool]:
    requested = str(name or "").strip()
    if not _COMMAND_NAME_RE.fullmatch(requested):
        return "command: invalid command name", True
    catalog = {item.key: item for item in discover_commands(workspace, config_dir=config_dir)}
    spec = catalog.get(requested.casefold())
    if spec is None:
        return f"command: unknown command '{requested}'", True
    try:
        text = spec.path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return f"command: could not read '{spec.name}': {exc}", True
    _, body = _frontmatter(text)
    return body.strip()[:MAX_COMMAND_BYTES], False


def expand_command(
    workspace: str | Path,
    name: str,
    arguments: str = "",
    *,
    config_dir: Path | None = None,
) -> tuple[str, bool]:
    body, is_error = read_command(workspace, name, config_dir=config_dir)
    if is_error:
        return body, True
    args = str(arguments or "").strip()[:MAX_COMMAND_ARGS]
    expanded = body.replace("$ARGUMENTS", args).replace("{{args}}", args)
    if "$ARGUMENTS" not in body and "{{args}}" not in body and args:
        expanded = expanded.rstrip() + "\n\nArguments:\n" + args
    return expanded[:MAX_COMMAND_BYTES], False
