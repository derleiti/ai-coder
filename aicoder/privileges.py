"""Risk classification for local and MCP-backed agent operations.

The coding-only client never grants elevation. Mutation approval controls
ordinary workspace changes; elevated requests always require explicit interactive approval.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


SUDO_PREFIX_RE = re.compile(r"^\s*sudo(?:\s+--)?\s+", re.IGNORECASE)
_ELEVATION_PREFIX_RE = re.compile(r"^\s*(?:sudo|doas|pkexec)(?:\s+--)?\s+", re.IGNORECASE)
_DELETE_RE = re.compile(r"(?:^|[;&|]\s*|\s)(?:sudo\s+)?(?:rm|rmdir|unlink|shred)\b", re.IGNORECASE)
_CREATE_OR_WRITE_RE = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:sudo\s+)?(?:touch|mkdir|mktemp|install|cp|mv|tee|truncate|chmod|chown|ln)\b"
    r"|(?:^|\s)sed\s+[^\n]*\s-i(?:\s|$)|(?:^|\s)>+\s*\S+",
    re.IGNORECASE,
)
_PACKAGE_OR_SERVICE_RE = re.compile(
    r"\b(?:apt(?:-get)?|dnf|yum|pacman|zypper)\s+(?:install|remove|purge|upgrade|update)\b"
    r"|\bsystemctl\s+(?:start|stop|restart|reload|enable|disable|mask|unmask)\b",
    re.IGNORECASE,
)
_GIT_MUTATION_RE = re.compile(
    r"\bgit\s+(?:add|commit|push|pull|merge|rebase|checkout|switch|reset|clean|stash|tag|branch\s+-[dD])\b",
    re.IGNORECASE,
)
_PROTECTED_PATH_RE = re.compile(
    r"(?<![\w.-])/(?:etc|boot|usr|bin|sbin|lib(?:32|64)?|root|var|opt|dev)(?:/|\b)",
    re.IGNORECASE,
)

# Tool-level mutation classification is transport-independent. The GUI may
# execute both local tools and MCP tools, so approval must not depend on where
# a tool happens to run. Exact names avoid false positives for read helpers.
_MUTATING_TOOL_NAMES = {
    "file_edit", "directory_create", "file_write", "file_ops", "code_edit", "code_patch",
    "shell", "task_runner", "custom_exec", "binary_exec", "local_exec",
    "git_ops", "lint", "test", "devops",
    "config_set", "prompt_set", "vault_add",
    "memory_store", "memory_clear", "clipboard_write",
    "crawl", "crawl_url",
    "service_control", "container_control", "remote_task", "mesh_task",
    "agent_start", "agent_stop", "agent_broadcast", "restart",
    "ollama_pull", "ollama_delete", "package_manager",
    "wp_create_draft", "wp_create_page", "wp_publish_post",
    "wp_update_post", "wp_delete_post", "mail_send", "notify_send",
}
_DESTRUCTIVE_TOOL_NAMES = {"memory_clear", "ollama_delete", "wp_delete_post"}


@dataclass(frozen=True)
class ExecutionRisk:
    needs_approval: bool
    elevation: bool
    sudo: bool
    mutation: bool
    deletion: bool
    protected_path: bool
    destructive: bool
    reasons: tuple[str, ...]
    command: str
    cwd: str
    user_reason: str

    @property
    def level(self) -> str:
        if self.elevation or self.destructive or self.deletion:
            return "high"
        if self.mutation:
            return "write"
        return "read"


def assess_execution(tool_name: str, args: dict[str, Any], *, destructive: bool = False) -> ExecutionRisk:
    """Classify a local tool call without executing or modifying it."""
    command = str(args.get("command") or "").strip()
    if not command and args.get("path"):
        operation = str(args.get("operation") or "access").strip()
        command = f"{operation} {args.get('path')}"
    cwd = str(args.get("cwd") or "").strip()
    normalized_tool = str(tool_name or "").strip().lower()
    # Providers and MCP gateways may namespace tool names (for example
    # ``mcp.code_edit`` or ``server/code_edit``). Security classification must
    # use the canonical leaf name while execution keeps the original name.
    canonical_tool = re.split(r"[./:]", normalized_tool)[-1]
    metadata_mutating = args.get("_mutating")
    metadata_destructive = args.get("_destructive")
    sudo_requested = bool(args.get("sudo")) or bool(SUDO_PREFIX_RE.match(command))
    explicit_elevation = sudo_requested or bool(_ELEVATION_PREFIX_RE.match(command))
    deletion = (
        bool(_DELETE_RE.search(command))
        or bool(re.search(r"(?:^|\s)find\b[^\n]*(?:\s-delete\b|\s-exec(?:dir)?\b|\s-ok(?:dir)?\b)", command, re.IGNORECASE))
        or canonical_tool in _DESTRUCTIVE_TOOL_NAMES
        or metadata_destructive is True
    )
    protected_path = bool(_PROTECTED_PATH_RE.search(command))
    mutation = (
        metadata_mutating is True
        or canonical_tool in _MUTATING_TOOL_NAMES
        or deletion
        or bool(_CREATE_OR_WRITE_RE.search(command))
        or bool(_PACKAGE_OR_SERVICE_RE.search(command))
        or bool(_GIT_MUTATION_RE.search(command))
        or bool(re.search(r"\bgit\s+(?:restore|rm|config|worktree|submodule\s+(?:add|deinit|update))\b", command, re.IGNORECASE))
        or bool(re.search(r"\b(?:npm|pnpm|yarn|pip|pipx)\s+(?:install|uninstall|publish|update|upgrade)\b", command, re.IGNORECASE))
    )

    reasons: list[str] = []
    if explicit_elevation:
        reasons.append("erhöhte lokale Rechte angefordert")
    if deletion:
        reasons.append("Dateien oder Verzeichnisse werden gelöscht")
    elif mutation:
        reasons.append("lokaler Zustand oder Dateien werden verändert")
    if protected_path:
        reasons.append("geschützter Systempfad betroffen")
    if destructive:
        reasons.append("potenziell destruktives Befehlsmuster")

    return ExecutionRisk(
        needs_approval=bool(explicit_elevation or mutation or destructive),
        elevation=explicit_elevation,
        sudo=sudo_requested,
        mutation=mutation,
        deletion=deletion,
        protected_path=protected_path,
        destructive=destructive,
        reasons=tuple(dict.fromkeys(reasons)),
        command=command,
        cwd=cwd,
        user_reason=str(args.get("reason") or "").strip(),
    )


def format_request(risk: ExecutionRisk) -> str:
    """Human-readable approval card used by terminal and GUI."""
    title = "PRIVILEGIEN" if risk.elevation else ("LÖSCHEN" if risk.deletion else "SCHREIBZUGRIFF")
    details = [f"  Anfrage : {title}"]
    if risk.user_reason:
        details.append(f"  Grund   : {risk.user_reason}")
    details.append(f"  Befehl  : {risk.command[:500]}")
    details.append(f"  Ordner  : {risk.cwd or '(aktueller Workspace)'}")
    if risk.reasons:
        details.append(f"  Risiko  : {'; '.join(risk.reasons)}")
    if risk.elevation:
        details.append("  Status  : Root/sudo erfordert immer eine ausdrückliche Einmal-Freigabe")
    return "\n".join(details)


def approval_is_automatic(mode: str, risk: ExecutionRisk) -> bool:
    """Return whether a persisted permission mode may approve this risk.

    Destructive/deletion and elevation requests are never auto-approved.
    Elevation is never auto-approved; interactive brokers may grant it once.
    """
    mode = str(mode or "ask").strip().lower()
    if not risk.needs_approval:
        return True
    if risk.elevation:
        return False
    if mode == "all":
        return bool(risk.mutation and not risk.deletion and not risk.destructive)
    if mode == "autopilot":
        return bool(risk.mutation and not risk.elevation and not risk.deletion and not risk.destructive)
    return False
