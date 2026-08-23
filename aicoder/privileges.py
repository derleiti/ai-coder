"""Risk classification for local and MCP-backed agent operations.

The operator may perform coding, DevOps, system and infrastructure work. Mutation approval controls
state changes; elevated requests always require explicit interactive approval and local authentication.
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
    "git_ops", "lint", "test", "devops",
    "config_set", "prompt_set", "vault_add", "settings_apply_patch", "settings_reset",
    "memory_store", "memory_clear", "clipboard_write",
    "crawl", "crawl_url",
    "service_control", "container_control", "remote_task", "mesh_task",
    "agent_start", "agent_stop", "agent_broadcast", "restart",
    "ollama_pull", "ollama_delete", "package_manager",
    "wp_create_draft", "wp_create_page", "wp_publish_post",
    "wp_update_post", "wp_delete_post", "mail_send", "notify_send",
}
_DESTRUCTIVE_TOOL_NAMES = {"memory_clear", "ollama_delete", "wp_delete_post"}

_COMMAND_RUNNER_TOOLS = {"shell", "task_runner", "custom_exec", "binary_exec", "local_exec"}
_READ_ONLY_PROGRAMS = {
    "cat", "cut", "df", "du", "env", "false", "find", "getprop", "grep", "head",
    "hostname", "id", "ip", "ls", "lsblk", "printf", "ps", "pwd", "readlink", "realpath",
    "sed", "ss", "stat", "tail", "termux-info", "true", "uname", "uptime", "wc", "which",
}

def _binary_exec_command(args: dict[str, Any]) -> str:
    program = str(args.get("program") or "").strip()
    argv = " ".join(str(item) for item in (args.get("arguments") or []))
    return f"{program} {argv}".strip()

def _known_read_only_command_runner(canonical_tool: str, args: dict[str, Any], command: str) -> bool:
    if canonical_tool == "binary_exec":
        program = str(args.get("program") or "").strip().lower().rsplit("/", 1)[-1]
        argv = [str(item) for item in (args.get("arguments") or [])]
        if program in _READ_ONLY_PROGRAMS:
            # find is read-only only without execution/deletion actions. sed is read-only without -i.
            joined = " ".join(argv)
            if program == "find" and re.search(r"(?:^|\s)-(?:delete|exec|execdir|ok|okdir)\b", joined):
                return False
            if program == "sed" and re.search(r"(?:^|\s)-[^\s]*i(?:\s|$)", joined):
                return False
            return True
        if program == "git":
            return bool(argv) and argv[0] in {"status", "log", "show", "diff", "grep", "rev-parse", "ls-files", "branch"} or argv[:1] == ["--version"]
        if program in {"python", "python3"} and argv[:1] in [["--version"], ["-V"]]:
            return True
        if program == "ssh" and argv[:1] == ["-V"]:
            return True
        if program == "curl" and argv[:1] == ["--version"]:
            return True
        if program == "pkg" and (argv[:1] in [["list-installed"], ["--version"], ["help"]]):
            return True
        return False
    # For free-form shells, remain conservative unless every command token is from a small
    # diagnostic vocabulary and no mutation syntax was detected elsewhere.
    if canonical_tool in {"shell", "task_runner", "custom_exec", "local_exec"}:
        if not command:
            return False
        if re.search(r"[><]", command):
            return False
        segments = re.split(r"(?:&&|\|\||;|\|)", command)
        for segment in segments:
            text = segment.strip()
            if not text:
                continue
            # Allow simple env assignment/echo separators used in diagnostic probes.
            token = text.split()[0].lower().rsplit("/", 1)[-1]
            if token in {"echo"}:
                continue
            if token == "git":
                parts = text.split()
                if len(parts) >= 2 and (parts[1] in {"status","log","show","diff","grep","rev-parse","ls-files","branch"} or parts[1] == "--version"):
                    continue
                return False
            if token not in _READ_ONLY_PROGRAMS:
                return False
        return True
    return False


@dataclass(frozen=True)
class ExecutionRisk:
    needs_approval: bool
    elevation: bool
    sudo: bool
    mutation: bool
    deletion: bool
    protected_path: bool
    destructive: bool
    security_change: bool
    reasons: tuple[str, ...]
    command: str
    cwd: str
    user_reason: str

    @property
    def level(self) -> str:
        if self.elevation or self.destructive or self.deletion or self.security_change:
            return "high"
        if self.mutation:
            return "write"
        return "read"


def assess_execution(tool_name: str, args: dict[str, Any], *, destructive: bool = False) -> ExecutionRisk:
    """Classify a local tool call without executing or modifying it."""
    command = str(args.get("command") or "").strip()
    normalized_tool = str(tool_name or "").strip().lower()
    canonical_tool = re.split(r"[./:]", normalized_tool)[-1]
    if not command and canonical_tool == "binary_exec":
        command = _binary_exec_command(args)
    if not command and args.get("path"):
        operation = str(args.get("operation") or "access").strip()
        command = f"{operation} {args.get('path')}"
    cwd = str(args.get("cwd") or "").strip()
    # Providers and MCP gateways may namespace tool names. Security classification
    # uses the canonical leaf while execution keeps the original name.
    metadata_mutating = args.get("_mutating")
    metadata_destructive = args.get("_destructive")
    security_change = args.get("_security_change") is True
    sudo_requested = bool(args.get("sudo")) or bool(SUDO_PREFIX_RE.match(command))
    explicit_elevation = sudo_requested or bool(_ELEVATION_PREFIX_RE.match(command))
    deletion = (
        bool(_DELETE_RE.search(command))
        or bool(re.search(r"(?:^|\s)find\b[^\n]*(?:\s-delete\b|\s-exec(?:dir)?\b|\s-ok(?:dir)?\b)", command, re.IGNORECASE))
        or canonical_tool in _DESTRUCTIVE_TOOL_NAMES
        or metadata_destructive is True
    )
    protected_path = bool(_PROTECTED_PATH_RE.search(command))
    runner_known_read_only = _known_read_only_command_runner(canonical_tool, args, command)
    runner_default_mutation = canonical_tool in _COMMAND_RUNNER_TOOLS and not runner_known_read_only
    mutation = (
        metadata_mutating is True
        or canonical_tool in _MUTATING_TOOL_NAMES
        or runner_default_mutation
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
    if security_change:
        reasons.append("Sicherheitsgrenze oder unbeaufsichtigte Berechtigung wird geändert")

    return ExecutionRisk(
        needs_approval=bool(explicit_elevation or mutation or destructive or security_change),
        elevation=explicit_elevation,
        sudo=sudo_requested,
        mutation=mutation,
        deletion=deletion,
        protected_path=protected_path,
        destructive=destructive,
        security_change=security_change,
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
    if risk.security_change:
        details.append("  Status  : Sicherheitsänderungen werden niemals automatisch freigegeben")
    return "\n".join(details)


def approval_is_automatic(mode: str, risk: ExecutionRisk) -> bool:
    """Return whether a persisted permission mode may approve this risk.

    Destructive/deletion and elevation requests are never auto-approved.
    Elevation is never auto-approved; interactive brokers may grant it once.
    """
    mode = str(mode or "ask").strip().lower()
    if not risk.needs_approval:
        return True
    if risk.elevation or risk.security_change:
        return False
    if mode == "all":
        return bool(risk.mutation and not risk.deletion and not risk.destructive)
    if mode == "autopilot":
        return bool(risk.mutation and not risk.elevation and not risk.deletion and not risk.destructive)
    return False


@dataclass(frozen=True)
class ApprovalDecision:
    automatic: bool
    requires_confirmation: bool
    denied: bool
    reason: str


class PrivilegeBroker:
    """Central host policy for CLI, GUI and headless approval decisions."""

    @staticmethod
    def authenticate_terminal() -> tuple[bool, str]:
        import shutil, subprocess, sys
        if not getattr(sys.stdin, "isatty", lambda: False)():
            return False, "no interactive TTY available for elevation"
        if shutil.which("sudo") is None:
            return False, "sudo is not installed"
        try:
            completed = subprocess.run(["sudo", "-v"], timeout=120, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"sudo authentication failed: {exc}"
        if completed.returncode != 0:
            return False, f"sudo authentication failed with exit code {completed.returncode}"
        return True, "sudo credentials validated"

    @staticmethod
    def gui_elevation_available() -> tuple[bool, str]:
        import os, shutil
        if shutil.which("pkexec") is None:
            return False, "pkexec is not installed"
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False, "no graphical session is available for Polkit authentication"
        return True, "pkexec available"

    @staticmethod
    def evaluate(mode: str, risk: ExecutionRisk, *, workspace_escape: bool = False, headless: bool = False) -> ApprovalDecision:
        if not risk.needs_approval and not workspace_escape:
            return ApprovalDecision(True, False, False, "read-only/safe operation")
        automatic = approval_is_automatic(mode, risk) and not workspace_escape
        if automatic:
            return ApprovalDecision(True, False, False, f"allowed by approval mode {mode}")
        if workspace_escape:
            reason = "workspace boundary crossing requires explicit approval"
        elif risk.elevation:
            reason = "elevation always requires explicit interactive approval/authentication"
        elif risk.security_change:
            reason = "security-boundary changes always require explicit approval"
        elif risk.deletion or risk.destructive:
            reason = "destructive operations always require explicit approval"
        else:
            reason = "state-changing operation requires explicit approval"
        if headless:
            return ApprovalDecision(False, False, True, reason + "; headless mode fails closed")
        return ApprovalDecision(False, True, False, reason)
