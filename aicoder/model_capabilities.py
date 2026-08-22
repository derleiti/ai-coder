"""Model capability lookup.

Sending a tool schema to a model that cannot call tools is not a soft failure:
several providers answer with an empty completion, which reaches the user as an
agent that silently refuses to work.  The backend already publishes per-model
capabilities in its catalogue, so ask before sending tools instead of guessing
from the model name.

Unknown models are treated as tool-capable.  A missing catalogue entry must
never disable a setup that works today; the cost of being wrong here is one
failed request, while the cost of the opposite default is silently degrading
every model the backend has not annotated yet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .client import model_identifier

# Capability strings that different backends use for the same thing.
_TOOL_CAPABILITIES = frozenset({
    "function_calling", "functions", "tool_calling", "tools", "tool_use",
})

_CACHE_TTL_SECONDS = 300

_catalogue: dict[str, frozenset[str]] | None = None
_catalogue_ts: float = 0.0


def _capabilities_of(entry: Any) -> frozenset[str]:
    if not isinstance(entry, dict):
        return frozenset()
    raw: Iterable[Any] = entry.get("capabilities") or []
    if isinstance(raw, str):
        raw = [raw]
    caps = {str(item).strip().lower() for item in raw if item}
    for key in ("tools", "tool_calling", "supports_tools", "function_calling"):
        if entry.get(key) is True:
            caps.add("function_calling")
    return frozenset(caps)




@dataclass(frozen=True)
class ModelInfo:
    id: str
    provider: str
    capabilities: frozenset[str]
    context_window: int | None = None
    available: bool = True
    raw: dict[str, Any] | None = None


def normalize_model_info(entry: Any) -> ModelInfo | None:
    model_id = model_identifier(entry)
    if not model_id:
        return None
    data = entry if isinstance(entry, dict) else {}
    provider = str(data.get("provider") or data.get("provider_id") or "").strip().lower()
    if not provider and "/" in model_id:
        provider = model_id.split("/", 1)[0].lower()
    raw_window = data.get("context_window") or data.get("context_length") or data.get("max_context_tokens")
    try:
        context_window = int(raw_window) if raw_window is not None else None
    except (TypeError, ValueError):
        context_window = None
    available_raw = data.get("available", data.get("enabled", True))
    return ModelInfo(
        id=model_id,
        provider=provider or "unknown",
        capabilities=_capabilities_of(data),
        context_window=context_window if context_window and context_window > 0 else None,
        available=bool(available_raw),
        raw=dict(data) if isinstance(data, dict) else None,
    )


def load_model_info(client: Any) -> dict[str, ModelInfo]:
    result: dict[str, ModelInfo] = {}
    try:
        entries = client.list_models() or []
    except Exception:
        return result
    for entry in entries:
        info = normalize_model_info(entry)
        if info is not None:
            result[info.id] = info
    return result

def load_catalogue(client: Any, *, force: bool = False) -> dict[str, frozenset[str]]:
    """Model id -> capability set, cached briefly to keep the agent loop cheap."""
    global _catalogue, _catalogue_ts
    fresh = _catalogue is not None and (time.monotonic() - _catalogue_ts) < _CACHE_TTL_SECONDS
    if fresh and not force:
        return _catalogue  # type: ignore[return-value]
    catalogue: dict[str, frozenset[str]] = {}
    try:
        for entry in client.list_models() or []:
            model_id = model_identifier(entry)
            if model_id:
                catalogue[model_id] = _capabilities_of(entry)
    except Exception:
        # A catalogue lookup must never break the run it was meant to protect.
        return _catalogue or {}
    _catalogue = catalogue
    _catalogue_ts = time.monotonic()
    return catalogue


def reset_cache() -> None:
    global _catalogue, _catalogue_ts
    _catalogue = None
    _catalogue_ts = 0.0


def capabilities(client: Any, model: str | None) -> Optional[frozenset[str]]:
    """Capabilities of one model, or None when the model is not in the catalogue."""
    if not model:
        return None
    return load_catalogue(client).get(str(model))


def supports_tools(client: Any, model: str | None) -> bool:
    """True unless the catalogue positively states the model cannot call tools."""
    caps = capabilities(client, model)
    if caps is None or not caps:
        return True
    return bool(caps & _TOOL_CAPABILITIES)
