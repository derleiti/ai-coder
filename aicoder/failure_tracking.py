"""Compact failure classification for the native agent loop.

The tracker identifies the same underlying failure across different tool calls,
so diagnostic stagnation is not limited to exact call/result loops.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_WS_RE = re.compile(r"\s+")
_LINE_RE = re.compile(r"\bline\s+\d+\b", re.IGNORECASE)
_HTTP_5XX_RE = re.compile(r"\bHTTP\s+5\d\d\b", re.IGNORECASE)
_EXCEPTION_RE = re.compile(
    r"(?m)^(ImportError|ModuleNotFoundError|SyntaxError|TypeError|ValueError|RuntimeError|"
    r"AttributeError|NameError|AssertionError|OSError|PermissionError):\s*(.+)$"
)


@dataclass(frozen=True)
class FailureObservation:
    category: str
    signature: str
    count: int
    retryable: bool


class FailureTracker:
    def __init__(self, transient_retry_budget: int = 2) -> None:
        self._counts: dict[str, int] = {}
        self.transient_retry_budget = max(0, int(transient_retry_budget))

    @staticmethod
    def _clean(text: str) -> str:
        text = _ANSI_RE.sub("", str(text or ""))
        text = _LINE_RE.sub("line #", text)
        return _WS_RE.sub(" ", text).strip()

    @classmethod
    def classify(cls, result: str) -> tuple[str, str, bool]:
        raw = _ANSI_RE.sub("", str(result or ""))
        text = cls._clean(raw)
        lower = text.lower()

        if any(token in lower for token in ("aborted by user", "rejected by user", "explicit approval")):
            category = "permission"
            retryable = False
        elif "429" in lower or _HTTP_5XX_RE.search(text) or any(
            token in lower for token in (
                "timed out", "timeout", "temporarily unavailable", "connection reset",
                "connection refused", "connection aborted", "remote end closed connection",
                "network is unreachable", "temporary failure in name resolution",
                "name or service not known", "transient incomplete chat response",
            )
        ):
            category = "transient"
            retryable = True
        elif any(token in lower for token in (
            "importerror", "modulenotfounderror", "abi", "partially initialized module",
            "unsupported python", "version mismatch",
        )):
            category = "environment"
            retryable = False
        elif any(token in lower for token in (
            "argument", "schema", "required", "not a file", "file does not exist",
            "path does not exist", "unknown tool", "unsupported role",
        )):
            category = "usage"
            retryable = False
        else:
            category = "code"
            retryable = False

        exception = _EXCEPTION_RE.findall(raw)
        if exception:
            kind, message = exception[-1]
            core = f"{kind}: {message}"
        else:
            core = text[-700:]
        core = cls._clean(core).lower()
        signature = f"{category}:{core}"[:900]
        return category, signature, retryable

    def observe(self, result: str, is_error: bool) -> FailureObservation | None:
        if not is_error:
            return None
        category, signature, retryable = self.classify(result)
        count = self._counts.get(signature, 0) + 1
        self._counts[signature] = count
        if category == "transient" and count > self.transient_retry_budget:
            # Preserve the underlying signature/count so the same dependency is
            # tracked as one failure family, but stop advertising it as retryable.
            category = "persistent_dependency"
            retryable = False
        return FailureObservation(category, signature, count, retryable)

    def reset(self) -> None:
        self._counts.clear()
