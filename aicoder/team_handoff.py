"""Deterministic compact handoffs between AICoder team stages.

The full stage result remains available to the orchestrator and RAM candidate
snapshots keep complete repository state. Model-facing handoffs carry a stable
content id plus a bounded evidence-oriented projection, avoiding another LLM
summarization/routing call merely to shrink context.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

_OMISSION_TEMPLATE = "\n...[handoff compacted; {omitted} chars omitted]"


@dataclass(frozen=True)
class HandoffEnvelope:
    kind: str
    handoff_id: str
    raw: str
    compact: str

    @property
    def original_chars(self) -> int:
        return len(self.raw)

    @property
    def compact_chars(self) -> int:
        return len(self.compact)

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - self.compact_chars)

    def render(self) -> str:
        return (
            f"[HANDOFF id={self.handoff_id} kind={self.kind} "
            f"chars={self.compact_chars}/{self.original_chars}]\n{self.compact}"
        )

    def metrics(self) -> dict[str, int | str]:
        return {
            "id": self.handoff_id,
            "kind": self.kind,
            "original_chars": self.original_chars,
            "compact_chars": self.compact_chars,
            "saved_chars": self.saved_chars,
        }


def _clean(text: str) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value


def _bounded(text: str, max_chars: int) -> str:
    text = _clean(text)
    max_chars = max(256, int(max_chars))
    if len(text) <= max_chars:
        return text
    suffix = _OMISSION_TEMPLATE.format(omitted=max(0, len(text) - max_chars))
    limit = max(64, max_chars - len(suffix))
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = text.rfind(" ", 0, limit)
    if cut < limit // 2:
        cut = limit
    return text[:cut].rstrip() + suffix


def _structured_projection(text: str, labels: tuple[str, ...], max_chars: int) -> str | None:
    if not labels:
        return None
    cleaned = _clean(text)
    escaped = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(rf"(?im)^\s*(?:#+\s*)?({escaped})\s*:\s*")
    matches = list(pattern.finditer(cleaned))
    if not matches:
        return None
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        body = cleaned[start:end].strip()
        sections.append((match.group(1).upper(), body))
    if not sections:
        return None
    overhead = sum(len(label) + 3 for label, _ in sections) + max(0, len(sections) - 1) * 2
    available = max(256, max_chars - overhead)
    weights = {
        "FINDINGS": 3,
        "SOURCES": 3,
        "APPLICABILITY": 2,
        "RECOMMENDATIONS": 2,
        "RISKS": 1,
        "UNRESOLVED RISKS": 1,
        "REQUIREMENTS": 3,
        "ROADMAP": 3,
        "ACCEPTANCE TESTS": 2,
        "VERIFICATION": 2,
        "MERGE CRITERIA": 1,
    }
    total_weight = sum(weights.get(label, 1) for label, _ in sections) or len(sections)
    rendered: list[str] = []
    for label, body in sections:
        share = max(180, int(available * weights.get(label, 1) / total_weight))
        rendered.append(f"{label}:\n{_bounded(body, share)}")
    return _bounded("\n\n".join(rendered), max_chars)


def make_handoff(
    kind: str,
    text: str,
    *,
    max_chars: int,
    section_labels: tuple[str, ...] = (),
) -> HandoffEnvelope:
    raw = _clean(text)
    digest = hashlib.sha256((str(kind) + "\0" + raw).encode("utf-8")).hexdigest()[:12]
    compact = _structured_projection(raw, section_labels, max_chars) or _bounded(raw, max_chars)
    return HandoffEnvelope(str(kind), f"ho-{digest}", raw, compact)


BRAINSTORM_SECTIONS = (
    "DIRECTIONS", "IDEAS", "TRADEOFFS", "RISKS", "OPEN QUESTIONS", "RECOMMENDATIONS",
)
RESEARCH_SECTIONS = ("FINDINGS", "SOURCES", "APPLICABILITY", "RISKS", "RECOMMENDATIONS")
CODE_PLAN_SECTIONS = (
    "OBJECTIVE", "REQUIREMENTS", "NON-GOALS", "ARCHITECTURE", "ARCHITECTURE BOUNDARIES",
    "AFFECTED AREAS", "ROADMAP", "ACCEPTANCE TESTS", "VERIFICATION", "VERIFICATION COMMANDS",
    "MERGE CRITERIA", "RISKS", "UNRESOLVED RISKS",
)
MERGE_PLAN_SECTIONS = (
    "BASE CANDIDATE", "IMPROVEMENTS", "CONFLICTS", "INVARIANTS", "VERIFICATION", "RISKS",
)
