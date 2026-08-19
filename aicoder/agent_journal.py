"""Private bounded continuation journals for persistent native-light plans.

The journal is deliberately protocol-neutral. It persists enough sanitized
conversation context to continue after a process restart, while raw tool output,
system prompts, credentials, and provider auth material are never stored.
Provider-native tool-call identity is retained as metadata for diagnostics and
future adapters, but incomplete native tool sequences are never blindly replayed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, atomic_write_private

JOURNAL_SCHEMA_VERSION = 1
MAX_JOURNAL_MESSAGES = 20
MAX_MESSAGE_CHARS = 4000
MAX_TOTAL_MESSAGE_CHARS = 32000
MAX_TOOL_BATCHES = 20
MAX_TOOL_CALLS_PER_BATCH = 12
MAX_SCALAR_CHARS = 1000

_SECRET_KEY_RE = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|cookie)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+\-/=]+", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_KEY_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret)\b\s*[:=]\s*([^\s,;]+)"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_key(workspace: str) -> str:
    resolved = str(Path(workspace or ".").expanduser().resolve(strict=False))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def _redact_text(value: Any, limit: int = MAX_SCALAR_CHARS) -> str:
    text = str(value or "")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED_JWT]", text)
    text = _KEY_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return text[: max(0, int(limit))]


def _sanitize_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_sanitize_value(item, depth=depth + 1) for item in value[:24]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:32]:
            key = str(raw_key)[:120]
            if _SECRET_KEY_RE.search(key):
                result[key] = "[REDACTED]"
            else:
                result[key] = _sanitize_value(raw_value, depth=depth + 1)
        return result
    return _redact_text(value)


def _stable_message(message: dict[str, Any]) -> dict[str, str] | None:
    role = str(message.get("role") or "")
    if role not in {"user", "assistant"}:
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    stripped = content.lstrip()
    # Raw tool evidence is intentionally not persisted. The tool metadata below
    # is sufficient to reconstruct a safe checkpoint and forces fresh inspection.
    if stripped.startswith("UNTRUSTED_TOOL_OUTPUT_BEGIN_") or stripped.startswith("Tool "):
        return None
    # Assistant messages containing executable/tool markup represent an
    # incomplete protocol boundary and must not be replayed as ordinary prose.
    if role == "assistant" and "<tool_call>" in content:
        return None
    return {"role": role, "content": _redact_text(content, MAX_MESSAGE_CHARS)}


@dataclass
class ContinuationJournal:
    plan_id: str
    workspace: str
    messages: list[dict[str, str]] = field(default_factory=list)
    tool_batches: list[dict[str, Any]] = field(default_factory=list)
    pending_input: str = ""
    updated_at: str = field(default_factory=_now)
    schema_version: int = JOURNAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "workspace": self.workspace,
            "messages": list(self.messages),
            "tool_batches": list(self.tool_batches),
            "pending_input": self.pending_input,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContinuationJournal":
        if int(data.get("schema_version") or 0) != JOURNAL_SCHEMA_VERSION:
            raise ValueError("unsupported journal schema")
        raw_messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        messages: list[dict[str, str]] = []
        total = 0
        for item in raw_messages[-MAX_JOURNAL_MESSAGES:]:
            if not isinstance(item, dict):
                continue
            stable = _stable_message(item)
            if stable is None:
                continue
            remaining = MAX_TOTAL_MESSAGE_CHARS - total
            if remaining <= 0:
                break
            stable["content"] = stable["content"][:remaining]
            total += len(stable["content"])
            messages.append(stable)
        raw_batches = data.get("tool_batches") if isinstance(data.get("tool_batches"), list) else []
        batches = [
            _sanitize_value(item)
            for item in raw_batches[-MAX_TOOL_BATCHES:]
            if isinstance(item, dict)
        ]
        return cls(
            plan_id=str(data.get("plan_id") or ""),
            workspace=str(data.get("workspace") or ""),
            messages=messages,
            tool_batches=batches,
            pending_input=_redact_text(data.get("pending_input"), MAX_MESSAGE_CHARS),
            updated_at=str(data.get("updated_at") or _now()),
        )

    def resume_messages(self) -> list[dict[str, str]]:
        rows = [dict(item) for item in self.messages]
        if self.tool_batches:
            summaries: list[str] = []
            for batch in self.tool_batches[-6:]:
                calls = batch.get("calls") if isinstance(batch.get("calls"), list) else []
                labels = []
                for call in calls[:MAX_TOOL_CALLS_PER_BATCH]:
                    if not isinstance(call, dict):
                        continue
                    name = str(call.get("name") or "?")
                    state = "error" if call.get("is_error") else "ok"
                    labels.append(f"{name}({state})")
                if labels:
                    summaries.append(", ".join(labels))
            if summaries:
                rows.append({
                    "role": "user",
                    "content": (
                        "Persistent continuation checkpoint: the prior process used these tools: "
                        + " | ".join(summaries)
                        + ". Raw tool outputs were intentionally not persisted. Re-inspect current "
                        "workspace state before relying on prior tool evidence or making a new mutation."
                    )[:MAX_MESSAGE_CHARS],
                })
        return rows[-MAX_JOURNAL_MESSAGES:]


class ContinuationJournalStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else CONFIG_DIR / "journals"

    def _workspace_dir(self, workspace: str) -> Path:
        path = self.root / _workspace_key(workspace)
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
            os.chmod(path, 0o700)
        except OSError:
            pass
        return path

    def _path(self, workspace: str, plan_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(plan_id or "")):
            raise ValueError("invalid plan id")
        return self._workspace_dir(workspace) / f"journal-{plan_id}.json"

    def save_checkpoint(
        self,
        *,
        plan_id: str,
        workspace: str,
        messages: list[dict[str, Any]],
        pending_input: str = "",
        tool_batches: list[dict[str, Any]] | None = None,
    ) -> Path:
        stable: list[dict[str, str]] = []
        total = 0
        for message in messages[-(MAX_JOURNAL_MESSAGES * 2):]:
            if not isinstance(message, dict):
                continue
            item = _stable_message(message)
            if item is None:
                continue
            remaining = MAX_TOTAL_MESSAGE_CHARS - total
            if remaining <= 0:
                break
            item["content"] = item["content"][:remaining]
            total += len(item["content"])
            stable.append(item)
        batches = [
            _sanitize_value(item)
            for item in (tool_batches or [])[-MAX_TOOL_BATCHES:]
            if isinstance(item, dict)
        ]
        pending = str(pending_input or "")
        if pending.lstrip().startswith("UNTRUSTED_TOOL_OUTPUT_BEGIN_"):
            pending = ""
        journal = ContinuationJournal(
            plan_id=str(plan_id),
            workspace=str(Path(workspace).expanduser().resolve(strict=False)),
            messages=stable[-MAX_JOURNAL_MESSAGES:],
            tool_batches=batches,
            pending_input=_redact_text(pending, MAX_MESSAGE_CHARS),
        )
        path = self._path(workspace, plan_id)
        atomic_write_private(path, json.dumps(journal.to_dict(), ensure_ascii=False, indent=2))
        return path

    def load(self, workspace: str, plan_id: str) -> ContinuationJournal | None:
        path = self._path(workspace, plan_id)
        if not path.exists() or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            journal = ContinuationJournal.from_dict(data)
            expected = str(Path(workspace).expanduser().resolve(strict=False))
            if journal.plan_id != plan_id or str(Path(journal.workspace).resolve(strict=False)) != expected:
                return None
            return journal
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def clear(self, workspace: str, plan_id: str) -> bool:
        path = self._path(workspace, plan_id)
        if not path.exists():
            return False
        path.unlink()
        return True
