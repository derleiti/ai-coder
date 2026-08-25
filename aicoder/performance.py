"""Low-overhead runtime performance telemetry for AICoder.

The collector is deliberately dependency-free and monotonic-clock based.  It
measures what AICoder can prove locally without pretending to separate network
latency from provider/model inference when the transport cannot expose that
split.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

_FILESYSTEM_TOOLS = frozenset({
    "file_read", "file_edit", "file_tree", "code_search", "code_grep",
    "directory_create", "file_delete", "file_move", "file_copy",
})
_BUILD_TEST_TOOLS = frozenset({"test", "lint", "binary_exec"})


def _ms(seconds: float) -> int:
    return max(0, int(round(float(seconds) * 1000.0)))


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def model_usage_metrics(result: Any, elapsed_ms: int | float = 0) -> dict[str, Any]:
    """Normalize provider token-usage metadata without inspecting response text."""
    data = result if isinstance(result, dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    gemini = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
    input_tokens = _int_value(usage.get("prompt_tokens") or usage.get("input_tokens") or gemini.get("promptTokenCount") or gemini.get("inputTokenCount"))
    output_tokens = _int_value(usage.get("completion_tokens") or usage.get("output_tokens") or gemini.get("candidatesTokenCount") or gemini.get("outputTokenCount"))
    total_tokens = _int_value(usage.get("total_tokens") or gemini.get("totalTokenCount"))
    if not total_tokens and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens
    elapsed = max(0, _int_value(elapsed_ms))
    tokens_per_second = round(output_tokens / (elapsed / 1000.0), 2) if output_tokens and elapsed > 0 else 0.0
    return {
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": total_tokens, "tokens_per_second": tokens_per_second,
        "usage_available": bool(input_tokens or output_tokens or total_tokens),
    }


@dataclass
class RuntimePerformance:
    """Aggregate timing for one agent run.

    Model request time includes provider/network/model time unless the selected
    transport exposes a more precise split.  Tool categories are subsets of
    total tool time and therefore must not be added to tool_ms again.
    """

    started_at: float = field(default_factory=time.monotonic)
    model_ms: int = 0
    tool_ms: int = 0
    filesystem_ms: int = 0
    build_test_ms: int = 0
    model_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tokenized_model_requests: int = 0
    tokenized_model_ms: int = 0
    tool_calls: int = 0
    filesystem_calls: int = 0
    tool_errors: int = 0
    slowest_model_ms: int = 0
    slowest_tool_ms: int = 0
    slowest_tool: str = ""

    def record_model(
        self, elapsed_ms: int | float, *, input_tokens: int = 0,
        output_tokens: int = 0, total_tokens: int = 0,
    ) -> None:
        elapsed = max(0, int(round(float(elapsed_ms or 0))))
        self.model_ms += elapsed
        self.model_requests += 1
        self.slowest_model_ms = max(self.slowest_model_ms, elapsed)
        self.input_tokens += max(0, int(input_tokens or 0))
        self.output_tokens += max(0, int(output_tokens or 0))
        self.total_tokens += max(0, int(total_tokens or 0))
        if input_tokens or output_tokens or total_tokens:
            self.tokenized_model_requests += 1
            self.tokenized_model_ms += elapsed

    def record_tool(self, name: str, elapsed_s: float, *, is_error: bool = False) -> None:
        elapsed = _ms(elapsed_s)
        normalized = str(name or "?")
        self.tool_ms += elapsed
        self.tool_calls += 1
        if is_error:
            self.tool_errors += 1
        if normalized in _FILESYSTEM_TOOLS:
            self.filesystem_ms += elapsed
            self.filesystem_calls += 1
        if normalized in _BUILD_TEST_TOOLS:
            self.build_test_ms += elapsed
        if elapsed > self.slowest_tool_ms:
            self.slowest_tool_ms = elapsed
            self.slowest_tool = normalized

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        wall_ms = _ms((time.monotonic() if now is None else now) - self.started_at)
        accounted_ms = min(wall_ms, self.model_ms + self.tool_ms)
        orchestration_ms = max(0, wall_ms - accounted_ms)
        model_share = (self.model_ms / wall_ms) if wall_ms else 0.0
        tool_share = (self.tool_ms / wall_ms) if wall_ms else 0.0
        io_share = (self.filesystem_ms / wall_ms) if wall_ms else 0.0
        average_model_ms = int(self.model_ms / self.model_requests) if self.model_requests else 0
        output_tokens_per_second = round(self.output_tokens / (self.tokenized_model_ms / 1000.0), 2) if self.output_tokens and self.tokenized_model_ms else 0.0

        bottleneck = "orchestration"
        dominant_ms = orchestration_ms
        if self.model_ms >= dominant_ms:
            bottleneck, dominant_ms = "model", self.model_ms
        if self.tool_ms > dominant_ms:
            bottleneck, dominant_ms = "tools", self.tool_ms

        warnings: list[dict[str, Any]] = []
        if self.model_requests and average_model_ms >= 10_000:
            warnings.append({
                "kind": "model_latency",
                "message": "Model/API responses are currently slow.",
                "average_ms": average_model_ms,
                "max_ms": self.slowest_model_ms,
            })
        if self.filesystem_ms >= 2_000 and io_share >= 0.20:
            warnings.append({
                "kind": "filesystem_latency",
                "message": "Filesystem I/O is a meaningful part of this run.",
                "total_ms": self.filesystem_ms,
                "share": round(io_share, 4),
            })
        if self.tool_errors >= 3:
            warnings.append({
                "kind": "tool_errors",
                "message": "Repeated tool failures are slowing this run.",
                "count": self.tool_errors,
            })

        return {
            "wall_ms": wall_ms,
            "model_ms": self.model_ms,
            "tool_ms": self.tool_ms,
            "filesystem_ms": self.filesystem_ms,
            "build_test_ms": self.build_test_ms,
            "orchestration_ms": orchestration_ms,
            "model_requests": self.model_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "tokenized_model_requests": self.tokenized_model_requests,
            "tokenized_model_ms": self.tokenized_model_ms,
            "output_tokens_per_second": output_tokens_per_second,
            "tool_calls": self.tool_calls,
            "filesystem_calls": self.filesystem_calls,
            "tool_errors": self.tool_errors,
            "average_model_ms": average_model_ms,
            "slowest_model_ms": self.slowest_model_ms,
            "slowest_tool_ms": self.slowest_tool_ms,
            "slowest_tool": self.slowest_tool,
            "model_share": round(model_share, 4),
            "tool_share": round(tool_share, 4),
            "filesystem_share": round(io_share, 4),
            "bottleneck": bottleneck,
            "warnings": warnings,
        }
