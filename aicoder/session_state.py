"""Backwards-compatible facade over the canonical settings registry.

Every definition, default and validation rule now lives in ``aicoder.settings``.
This module keeps the historical import surface working (13 call sites across
CLI, GUI, REPL, executor and tests) so the migration to the registry does not
have to happen in one commit.

Prefer ``aicoder.settings`` in new code.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from . import settings as _settings
from .config import CONFIG_DIR, atomic_write_private  # noqa: F401 - patched by tests
from .settings import (  # noqa: F401 - re-exported for existing importers
    APPROVAL_MODES,
    DEFAULT_RUNTIME_MODE,
    RUNTIME_MODES,
    SWARM_MODES,
    TOOL_MODES,
    WORKSPACE_MODES,
    migrate_enabled_tools,
)

STATE_FILE = CONFIG_DIR / "state.json"

_DEFAULTS: Dict[str, Any] = dict(_settings.DEFAULTS)

# Resolved lazily through the module global so ``patch.object(session_state,
# "STATE_FILE", ...)`` keeps working: the store must never capture the path at
# import time.
_STORE = _settings.SettingsStore(lambda: STATE_FILE)

# Legacy cache mirrors. Tests set these to None to force a reload; the bridge in
# _load_raw() turns that into a real store invalidation.
_cache: Dict[str, Any] | None = None
_cache_stamp: tuple[str, int, int] | None = None
_lock = threading.Lock()


def _load_raw() -> Dict[str, Any]:
    global _cache, _cache_stamp
    if _cache is None or _cache_stamp is None:
        _STORE.invalidate()
    data = _STORE.load()
    data.pop("_schema_version", None)
    data.pop("_recovered_from", None)
    _cache = dict(data)
    _cache_stamp = _STORE._stamp(_STORE.path)
    return dict(data)


def _save_raw(data: Dict[str, Any]) -> None:
    global _cache, _cache_stamp
    saved = _STORE.save(data)
    saved.pop("_schema_version", None)
    _cache = dict(saved)
    _cache_stamp = _STORE._stamp(_STORE.path)


def get_state() -> Dict[str, Any]:
    return _load_raw()


def _apply(**changes: Any) -> None:
    global _cache, _cache_stamp
    if _cache is None or _cache_stamp is None:
        _STORE.invalidate()
    saved = _STORE.update(**changes)
    saved.pop("_schema_version", None)
    _cache = dict(saved)
    _cache_stamp = _STORE._stamp(_STORE.path)


def set_model(model: str) -> None:
    # Clearing an identical fallback is an invariant of the registry, not of
    # this call site — see settings.apply_invariants().
    _apply(selected_model=model)


def set_fallback(model: str) -> None:
    """Deprecated compatibility no-op: automatic fallback routing was removed."""
    return None


def set_tool_mode(mode: str) -> None:
    _apply(tool_mode=mode)


def set_enabled_tools(names: Optional[list[str]]) -> None:
    """Persist selected tool names. None means all discovered tools."""
    _apply(enabled_tools=names)


def set_native_openrouter_tool_calling(enabled: bool) -> None:
    _apply(native_openrouter_tool_calling=bool(enabled))


def set_approval_mode(mode: str) -> None:
    _apply(approval_mode=mode)


def set_request_timeout(seconds: int) -> None:
    # Historical behaviour clamps instead of raising, because the GUI spin box
    # and older configs rely on out-of-range values being silently corrected.
    spec = _settings.REGISTRY["request_timeout"]
    value = max(spec.minimum, min(spec.maximum, int(seconds)))
    _apply(request_timeout=value)


def set_runtime_mode(mode: str) -> None:
    _apply(runtime_mode=mode)


def set_swarm(mode: str) -> None:
    _apply(swarm_mode=mode)


def set_workspace(path: Optional[str]) -> None:
    _apply(workspace_root=path)


def set_workspace_mode(mode: str) -> None:
    _apply(workspace_mode=mode)
