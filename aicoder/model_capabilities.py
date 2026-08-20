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


def tool_capable_alternative(client: Any, model: str | None) -> Optional[str]:
    """Find a sibling of the same base model that does support tool calling.

    Backends expose the same weights under several routes — 'nvidia/<model>'
    and 'openrouter/nvidia/<model>:free' can differ only in whether tool calling
    is available.  Suggesting the working route is far more useful than telling
    the user their model is unsuitable.
    """
    if not model:
        return None
    catalogue = load_catalogue(client)
    base = str(model).split("/")[-1].split(":")[0]
    if not base:
        return None
    candidates = [
        model_id for model_id, caps in catalogue.items()
        if model_id != model
        and base in model_id
        and (caps & _TOOL_CAPABILITIES)
    ]
    if not candidates:
        return None
    # Prefer a free route, then the shortest id — the least surprising variant.
    candidates.sort(key=lambda mid: (0 if mid.endswith(":free") else 1, len(mid)))
    return candidates[0]
