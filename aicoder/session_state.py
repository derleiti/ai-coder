from __future__ import annotations
import json, threading
from pathlib import Path
from typing import Any, Dict, Optional

from .config import CONFIG_DIR, atomic_write_private

STATE_FILE = CONFIG_DIR / "state.json"

SWARM_MODES = {"off", "auto", "on", "review"}
TOOL_MODES = {"off", "on_demand", "always"}
APPROVAL_MODES = {"ask", "autopilot", "all"}
RUNTIME_MODES = {"classic", "native-light"}
DEFAULT_RUNTIME_MODE = "native-light"
DEFAULT_FALLBACK_MODEL = "ollama/llama3.2:latest"

_DEFAULTS: Dict[str, Any] = {
    "selected_model": None,
    "fallback_model": DEFAULT_FALLBACK_MODEL,
    "swarm_mode": "off",
    "workspace_root": None,
    # on_demand skips tool discovery for greetings/small talk, but keeps the
    # full agent available for actual work.  None means "all discovered tools".
    "tool_mode": "on_demand",
    "enabled_tools": None,
    "request_timeout": 300,
    # ask: confirm every mutation; autopilot: safe writes only; all: all mutations.
    "approval_mode": "ask",
    # Native agent engine is the default; classic remains an explicit compatibility mode.
    "runtime_mode": DEFAULT_RUNTIME_MODE,
}

# Before the coding-only policy was centralized, the Settings UI persisted
# "Select all" as a concrete snapshot. That snapshot now contains removed
# admin/ops tools and omits newly introduced safe tools, so filtering it against
# the current catalogue produces the misleading 27/40 state. Migrate only this
# exact historical all-tools snapshot; real custom selections remain untouched.
_LEGACY_ALL_TOOLS = frozenset({
    "agents", "clipboard_read", "clipboard_write", "code_grep", "code_read",
    "code_search", "code_tree", "dev_analyze", "dev_debug", "dev_links",
    "dev_lint", "dev_refactor", "dev_summarize", "devops", "doc_read",
    "doc_search", "file_edit", "file_read", "file_tree", "git", "health",
    "lint", "local_exec", "logs", "logs_errors", "logs_stats",
    "memory_search", "memory_store", "models", "ollama_list", "ollama_status",
    "remote_hosts", "remote_status", "search", "status", "test", "vault_keys",
    "vault_status", "web_fetch_local", "web_search_local",
})


def migrate_enabled_tools(value: Any) -> Optional[list[str]]:
    """Convert the obsolete explicit 'all tools' snapshot back to its meaning."""
    if value is None:
        return None
    if not isinstance(value, list):
        return []
    normalized = [str(name) for name in value if isinstance(name, str) and name]
    if frozenset(normalized) == _LEGACY_ALL_TOOLS:
        return None
    return normalized

# In-memory cache — vermeidet wiederholte Disk-Reads im Agent-Loop
_cache: Dict[str, Any] | None = None
_cache_stamp: tuple[str, int, int] | None = None
_lock = threading.Lock()  # thread-safe cache access (GUI + Worker threads)


def _state_stamp() -> tuple[str, int, int]:
    try:
        stat = STATE_FILE.stat()
        return str(STATE_FILE), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(STATE_FILE), -1, -1


def _load_raw() -> Dict[str, Any]:
    global _cache, _cache_stamp
    with _lock:
        stamp = _state_stamp()
        if _cache is not None and _cache_stamp == stamp:
            return dict(_cache)
        if not STATE_FILE.exists():
            _cache = dict(_DEFAULTS)
            _cache_stamp = stamp
            return dict(_cache)
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            # Migrate the old "unset" null value. An explicit empty string
            # still means that the user intentionally disabled fallback.
            if data.get("fallback_model") is None:
                data["fallback_model"] = DEFAULT_FALLBACK_MODEL
            if data.get("approval_mode") not in APPROVAL_MODES:
                data["approval_mode"] = "ask"
            if data.get("runtime_mode") not in RUNTIME_MODES:
                data["runtime_mode"] = DEFAULT_RUNTIME_MODE
            data["enabled_tools"] = migrate_enabled_tools(data.get("enabled_tools"))
            _cache = {**_DEFAULTS, **data}
            _cache_stamp = stamp
            return dict(_cache)
        except Exception:
            _cache = dict(_DEFAULTS)
            _cache_stamp = stamp
            return dict(_cache)


def _save_raw(data: Dict[str, Any]) -> None:
    global _cache, _cache_stamp
    with _lock:
        atomic_write_private(STATE_FILE, json.dumps(data, indent=2))
        _cache = dict(data)
        _cache_stamp = _state_stamp()


def get_state() -> Dict[str, Any]:
    return _load_raw()


def set_model(model: str) -> None:
    d = _load_raw()
    d["selected_model"] = model
    # A fallback identical to the primary can never recover a failed request.
    # Clear it centrally so CLI, GUI and setup all share the same invariant.
    if d.get("fallback_model") == model:
        d["fallback_model"] = ""
    _save_raw(d)


def set_fallback(model: str) -> None:
    d = _load_raw()
    # Empty string explicitly disables fallback. Identical primary/fallback is
    # normalized to disabled instead of presenting a fake recovery path.
    d["fallback_model"] = "" if model == d.get("selected_model") else model
    _save_raw(d)


def set_tool_mode(mode: str) -> None:
    if mode not in TOOL_MODES:
        raise ValueError(f"Ungültiger Tool-Modus '{mode}'. Erlaubt: {', '.join(sorted(TOOL_MODES))}")
    d = _load_raw()
    d["tool_mode"] = mode
    _save_raw(d)


def set_enabled_tools(names: Optional[list[str]]) -> None:
    """Persist selected tool names. None means all discovered tools."""
    d = _load_raw()
    d["enabled_tools"] = None if names is None else sorted(set(names))
    _save_raw(d)



def set_approval_mode(mode: str) -> None:
    if mode not in APPROVAL_MODES:
        raise ValueError(
            f"Ungültiger Approval-Modus '{mode}'. Erlaubt: {', '.join(sorted(APPROVAL_MODES))}"
        )
    d = _load_raw()
    d["approval_mode"] = mode
    _save_raw(d)


def set_request_timeout(seconds: int) -> None:
    d = _load_raw()
    d["request_timeout"] = max(10, min(300, int(seconds)))
    _save_raw(d)

def set_runtime_mode(mode: str) -> None:
    if mode not in RUNTIME_MODES:
        raise ValueError(
            f"Ungültiger Runtime-Modus '{mode}'. Erlaubt: {', '.join(sorted(RUNTIME_MODES))}"
        )
    d = _load_raw()
    d["runtime_mode"] = mode
    _save_raw(d)


def set_swarm(mode: str) -> None:
    if mode not in SWARM_MODES:
        raise ValueError(f"Ungültiger Swarm-Modus '{mode}'. Erlaubt: {', '.join(sorted(SWARM_MODES))}")
    d = _load_raw()
    d["swarm_mode"] = mode
    _save_raw(d)


def set_workspace(path: Optional[str]) -> None:
    d = _load_raw()
    d["workspace_root"] = path
    _save_raw(d)
