"""Authoritative machine-readable task constraints for AICoder runs.

The contract is compiled once from the user's task and then projected into
runtime policy, tool policy, verification and model prompts. Prompts may explain
constraints, but runtime policy remains authoritative when a model ignores them.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

_EXTERNAL_RESEARCH_SIGNAL_RE = re.compile(
    r"(?i)(?:https?://|\b(?:latest|recent)\b|\bcurrent\s+(?:version|release|status)\b|"
    r"\brelease\s+notes?\b|\bdeprecat(?:ed|ion|ions)?\b|\bcompatib(?:ility|le)\b|"
    r"\bCVE-\d{4}-\d+\b|\bsecurity\s+advisory\b|\bAPI\b|\bSDK\b|"
    r"\bprotocol\b|\bspecification\b|\bupstream\b|\bofficial\s+documentation\b|"
    r"\bexternal\s+sources?\b|\bprovider\b|\bendpoint\b)"
)
_NEGATION_RE = re.compile(
    r"(?i)\b(?:do\s+not|don't|never|must\s+not|no|without|nicht|niemals|ohne|kein(?:e|en|er|es)?)\b"
)
_WEB_RE = re.compile(r"(?i)\b(?:web|internet|browse|browsing|online|websuche|internetsuche)\b")
_TRIFORCE_RE = re.compile(r"(?i)\btriforce\b")
_ACCEPTANCE_SECTION_RE = re.compile(
    r"^(?:acceptance(?: checks| tests)?|verification commands?)\b.*:\s*$", re.IGNORECASE
)
_ACCEPTANCE_LINE_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$")

_WEB_TOOL_NAMES = {
    "search", "crawl", "crawl_url", "web_fetch", "web_fetch_local", "browser", "browser_search",
}


def _segments(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\n.!?;]+", text) if part.strip()]


def _explicitly_forbids(text: str, subject_re: re.Pattern[str]) -> bool:
    for segment in _segments(text):
        if subject_re.search(segment) and _NEGATION_RE.search(segment):
            return True
    return False


def _extract_acceptance_commands(text: str) -> tuple[str, ...]:
    commands: list[str] = []
    in_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _ACCEPTANCE_SECTION_RE.match(line):
            in_section = True
            continue
        if in_section and line.startswith("#"):
            in_section = False
        if not in_section:
            continue
        match = _ACCEPTANCE_LINE_RE.match(raw)
        if not match:
            if line.endswith(":"):
                in_section = False
            continue
        commands.append(match.group(1).strip().strip("`"))
    return tuple(commands)


def _extract_constraints(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    requirements: list[str] = []
    prohibitions: list[str] = []
    for segment in _segments(text):
        low = segment.lower()
        if _NEGATION_RE.search(segment):
            prohibitions.append(segment)
        elif re.search(r"\b(?:must|required|shall|muss|müssen|erforderlich|soll)\b", low):
            requirements.append(segment)
    return tuple(requirements[:64]), tuple(prohibitions[:64])


@dataclass(frozen=True)
class TaskContract:
    """Immutable task truth shared by runtime, tools, verification and prompts."""

    task_sha256: str
    requirements: tuple[str, ...] = ()
    prohibitions: tuple[str, ...] = ()
    forbid_web: bool = False
    forbid_research_web: bool = False
    forbid_triforce_backend: bool = False
    external_research_required: bool = False
    acceptance_commands: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "aicoder-task-contract-v1",
            "task_sha256": self.task_sha256,
            "requirements": list(self.requirements),
            "prohibitions": list(self.prohibitions),
            "forbid_web": self.forbid_web,
            "forbid_research_web": self.forbid_research_web,
            "forbid_triforce_backend": self.forbid_triforce_backend,
            "external_research_required": self.external_research_required,
            "acceptance_commands": list(self.acceptance_commands),
        }

    def prompt_projection(self) -> str:
        lines = [
            "## AUTHORITATIVE TASK CONTRACT",
            "Runtime/tool gates enforce this contract even if model prose disagrees.",
            f"- Web/network use outside research forbidden: {'yes' if self.forbid_web else 'no'}",
            f"- Research-stage web access forbidden: {'yes' if self.forbid_research_web else 'no'}",
            f"- TriForce backend targeting forbidden: {'yes' if self.forbid_triforce_backend else 'no'}",
            f"- External research required by task: {'yes' if self.external_research_required else 'no'}",
        ]
        if self.requirements:
            lines.append("- Explicit requirements:")
            lines.extend(f"  - {item}" for item in self.requirements[:12])
        if self.acceptance_commands:
            lines.append("- Explicit acceptance commands:")
            lines.extend(f"  - {command}" for command in self.acceptance_commands)
        if self.prohibitions:
            lines.append("- Explicit prohibitions:")
            lines.extend(f"  - {item}" for item in self.prohibitions[:12])
        return "\n".join(lines)

    def tool_denial(self, tool_name: str, args: dict[str, Any] | None = None) -> str | None:
        """Return an authoritative denial reason before any tool transport executes."""
        name = str(tool_name or "").strip().lower()
        canonical = name.rsplit(".", 1)[-1].rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        if self.forbid_web and canonical in _WEB_TOOL_NAMES:
            return "web/network access is disabled by the authoritative task contract"
        if self.forbid_triforce_backend:
            if name.startswith("mcp.triforce"):
                return "TriForce backend MCP access is disabled by the authoritative task contract"
            # Built-in backend tools are recognized by the executor after local/provider routing.
        return None


def compile_task_contract(task: str) -> TaskContract:
    text = str(task or "")
    forbid_web = _explicitly_forbids(text, _WEB_RE)
    forbid_research_web = any(
        _WEB_RE.search(segment)
        and re.search(r"(?i)\b(?:research(?:er|ers|ing)?|rechercheur|rechercheure|recherche|research-stage)\b", segment)
        and _NEGATION_RE.search(segment)
        for segment in _segments(text)
    ) or bool(
        forbid_web
        and re.search(
            r"(?i)\b(?:use|using|nutze|verwende)\s+only\s+(?:local|repository)|"
            r"\bonly\s+local\s+(?:repository\s+)?evidence\b|\boffline[- ]only\b",
            text,
        )
    )
    forbid_triforce = _explicitly_forbids(text, _TRIFORCE_RE)
    requirements, prohibitions = _extract_constraints(text)
    return TaskContract(
        task_sha256=hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        requirements=requirements,
        prohibitions=prohibitions,
        forbid_web=forbid_web,
        forbid_research_web=forbid_research_web,
        forbid_triforce_backend=forbid_triforce,
        external_research_required=bool(_EXTERNAL_RESEARCH_SIGNAL_RE.search(text)) and not forbid_research_web,
        acceptance_commands=_extract_acceptance_commands(text),
    )
