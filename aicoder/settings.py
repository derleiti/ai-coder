"""Canonical settings registry and store for AICoder.

This module is the single source of truth for *what* a setting is: its type,
default, allowed values, description and security classification.  CLI, GUI,
REPL and the LLM-facing settings tools all read this registry instead of
carrying their own copies of defaults and choice lists.

Design notes
------------
* The state path is resolved *per call*, never captured at import time.  The
  existing test-suite patches ``session_state.STATE_FILE`` at runtime, and the
  GUI may be started with a different config dir than the CLI.
* Writes go through ``config.atomic_write_private`` (mkstemp -> fsync ->
  chmod 0600 -> os.replace), guarded by an advisory *file* lock so that a
  concurrent CLI and GUI process cannot lose each other's update.  A
  ``threading.Lock`` alone only protects threads inside one process.
* A corrupted ``state.json`` is preserved as ``state.json.corrupt-<stamp>``
  instead of being silently overwritten with defaults, so the cause stays
  diagnosable.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .config import CONFIG_DIR, atomic_write_private

try:  # POSIX advisory locking
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows advisory locking
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
_SCHEMA_KEY = "_schema_version"

SWARM_MODES = {"off", "auto", "on", "review"}
TOOL_MODES = {"off", "on_demand", "always"}
APPROVAL_MODES = {"ask", "autopilot", "all"}
RUNTIME_MODES = {"classic", "native-light"}
WORKSPACE_MODES = {"auto", "ram", "disk"}
DEFAULT_RUNTIME_MODE = "native-light"
DEFAULT_FALLBACK_MODEL = "ollama/llama3.2:latest"


class SettingsError(ValueError):
    """Raised when a value does not satisfy its schema entry."""


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SettingSpec:
    """Metadata for exactly one setting.

    ``security_impact`` marks settings whose change can widen what the agent is
    allowed to do without asking.  The policy layer requires explicit user
    confirmation for those, even when the current approval mode is permissive —
    an LLM must never be able to grant itself more privilege by writing a
    setting.
    """

    key: str
    type: str                      # str | int | bool | enum | path | list | model
    default: Any
    description: str
    group: str
    choices: Optional[frozenset] = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    aliases: Tuple[str, ...] = ()
    sensitive: bool = False        # never printed / never exposed to the model
    mutable: bool = True           # False = read-only, diagnostic value
    restart_required: bool = False
    security_impact: bool = False
    nullable: bool = False

    def choice_list(self) -> List[str]:
        return sorted(self.choices) if self.choices else []


REGISTRY: Dict[str, SettingSpec] = {}


def _register(spec: SettingSpec) -> SettingSpec:
    REGISTRY[spec.key] = spec
    return spec


_register(SettingSpec(
    key="selected_model", type="model", default=None, nullable=True,
    group="model", aliases=("model",),
    description="Primary coding model, as 'provider/model'. Unset means the backend default.",
))
_register(SettingSpec(
    key="fallback_model", type="model", default=DEFAULT_FALLBACK_MODEL,
    group="model", aliases=("fallback",),
    description=(
        "Model used when the primary fails. An empty string disables fallback. "
        "It can never equal the primary — an identical fallback cannot recover a failed request."
    ),
))
_register(SettingSpec(
    key="swarm_mode", type="enum", default="off", choices=frozenset(SWARM_MODES),
    group="agent", aliases=("swarm",),
    description="Multi-model swarm behaviour: off, auto (on demand), on (always), review (second opinion only).",
))
_register(SettingSpec(
    key="workspace_root", type="path", default=None, nullable=True,
    group="workspace", aliases=("workspace",),
    description="Active workspace directory. All file tools are scoped to this root.",
))
_register(SettingSpec(
    key="workspace_mode", type="enum", default="auto", choices=frozenset(WORKSPACE_MODES),
    group="workspace", aliases=("execution_workspace", "workspace_execution"),
    description=(
        "Execution workspace: auto prefers an isolated transactional RAM workspace when safe, "
        "ram requests RAM with automatic disk fallback, disk uses the source tree directly."
    ),
))
_register(SettingSpec(
    key="tool_mode", type="enum", default="on_demand", choices=frozenset(TOOL_MODES),
    group="tools", aliases=("tool-mode",),
    description=(
        "Tool discovery: off (never), on_demand (load tools only for tool-relevant turns), "
        "always (expose the catalogue every turn)."
    ),
))
_register(SettingSpec(
    key="enabled_tools", type="list", default=None, nullable=True,
    group="tools", aliases=("tools",),
    description=(
        "Allow-list of tool names. Unset means every discovered tool. "
        "Nothing — not a plugin, not the model — can re-enable a tool excluded here."
    ),
))
_register(SettingSpec(
    key="native_openrouter_tool_calling", type="bool", default=False,
    group="tools", aliases=("openrouter_native_tools", "native_openrouter_tools"),
    description=(
        "Experimental compatibility switch. AICoder uses its provider-independent "
        "text tool protocol by default for every model. Enable this only to send "
        "provider-native tools/tool_choice to OpenRouter models."
    ),
))
_register(SettingSpec(
    key="request_timeout", type="int", default=300, minimum=10, maximum=300,
    group="runtime", aliases=("timeout",),
    description=("Seconds of provider/network inactivity allowed while waiting for an LLM request. "
        "Streaming keepalive activity resets this idle timer; it is not a hard total turn deadline "
        "and is unrelated to shell/subprocess timeouts."),
))
_register(SettingSpec(
    key="max_output_tokens", type="int", default=16384, minimum=256, maximum=200000,
    group="runtime", aliases=("max_tokens", "output_tokens"),
    description=(
        "Upper bound on tokens the model may write per request. This is the reply "
        "budget, not the context window. The old hard-coded 4096 truncated any "
        "generated file past roughly 370 lines mid-line."
    ),
))
_register(SettingSpec(
    key="approval_mode", type="enum", default="ask", choices=frozenset(APPROVAL_MODES),
    group="security", aliases=("approval",), security_impact=True,
    description=(
        "When mutations need confirmation: ask (every mutation), autopilot (safe writes "
        "without asking), all (all mutations without asking). Lowering this widens what runs unattended."
    ),
))
_register(SettingSpec(
    key="runtime_mode", type="enum", default=DEFAULT_RUNTIME_MODE, choices=frozenset(RUNTIME_MODES),
    group="runtime", aliases=("runtime",), restart_required=True,
    description="Agent engine: native-light (default agentic loop) or classic (compatibility mode).",
))


DEFAULTS: Dict[str, Any] = {key: spec.default for key, spec in REGISTRY.items()}

_ALIAS_MAP: Dict[str, str] = {}
for _key, _spec in REGISTRY.items():
    _ALIAS_MAP[_key] = _key
    for _alias in _spec.aliases:
        _ALIAS_MAP[_alias] = _key


def resolve_key(name: str) -> str:
    """Map a user-facing name or alias to its canonical setting key."""
    canonical = _ALIAS_MAP.get(str(name).strip().replace("-", "_").lower()) \
        or _ALIAS_MAP.get(str(name).strip().lower())
    if canonical is None:
        raise SettingsError(
            f"Unknown setting '{name}'. Known: {', '.join(sorted(REGISTRY))}"
        )
    return canonical


def spec_for(name: str) -> SettingSpec:
    return REGISTRY[resolve_key(name)]


# --------------------------------------------------------------------------
# Legacy migration
# --------------------------------------------------------------------------

# Before the operator tool policy was centralized, the Settings UI persisted
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


def migrate_enabled_tools(value: Any) -> Optional[List[str]]:
    """Convert the obsolete explicit 'all tools' snapshot back to its meaning."""
    if value is None:
        return None
    if not isinstance(value, list):
        return []
    normalized = [str(name) for name in value if isinstance(name, str) and name]
    if frozenset(normalized) == _LEGACY_ALL_TOOLS:
        return None
    return normalized


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def coerce(name: str, value: Any) -> Any:
    """Validate and normalize a single value against its schema entry."""
    spec = spec_for(name)

    if value is None:
        if spec.nullable or spec.default is None:
            return None
        raise SettingsError(f"'{spec.key}' cannot be null.")

    if spec.type == "enum":
        text = str(value).strip()
        if text not in (spec.choices or frozenset()):
            raise SettingsError(
                f"Invalid value '{text}' for '{spec.key}'. Allowed: {', '.join(spec.choice_list())}"
            )
        return text

    if spec.type == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise SettingsError(f"'{spec.key}' expects a whole number, got '{value}'.") from None
        if spec.minimum is not None and number < spec.minimum:
            raise SettingsError(f"'{spec.key}' must be >= {spec.minimum} (got {number}).")
        if spec.maximum is not None and number > spec.maximum:
            raise SettingsError(f"'{spec.key}' must be <= {spec.maximum} (got {number}).")
        return number

    if spec.type == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise SettingsError(f"'{spec.key}' expects a boolean, got '{value}'.")

    if spec.type == "list":
        if isinstance(value, str):
            text = value.strip()
            if text.lower() in {"all", "*"}:
                return None
            if text.lower() in {"none", "-"}:
                return []
            items = [part.strip() for part in text.split(",")]
        elif isinstance(value, (list, tuple, set, frozenset)):
            items = [str(part).strip() for part in value]
        else:
            raise SettingsError(f"'{spec.key}' expects a list or comma-separated string.")
        return sorted({item for item in items if item})

    if spec.type == "path":
        text = str(value).strip()
        if not text:
            return None
        return str(Path(text).expanduser())

    return str(value)


def apply_invariants(data: Dict[str, Any]) -> Dict[str, Any]:
    """Enforce cross-setting rules centrally, so no UI can bypass them."""
    # A fallback identical to the primary can never recover a failed request.
    if data.get("fallback_model") and data.get("fallback_model") == data.get("selected_model"):
        data["fallback_model"] = ""
    return data


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def default_state_path() -> Path:
    """Resolved per call — the config dir may be patched or differ per process."""
    return CONFIG_DIR / "state.json"


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Advisory cross-process lock on a sidecar file.

    A ``threading.Lock`` only serialises threads inside one interpreter; a GUI
    and a CLI process editing the same state.json need a lock the kernel knows
    about.  Failure to lock is never fatal: on platforms without flock support
    we degrade to the previous behaviour rather than refusing to save.
    """
    lock_path = path.with_name(path.name + ".lock")
    handle = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    except OSError:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            try:
                handle.close()
            except OSError:
                pass


@dataclass
class _CacheEntry:
    stamp: Tuple[str, int, int]
    data: Dict[str, Any]


class SettingsStore:
    """Reads and writes state.json against the canonical registry."""

    def __init__(self, path_resolver: Callable[[], Path] = default_state_path) -> None:
        self._resolve = path_resolver
        self._cache: Optional[_CacheEntry] = None
        self._lock = threading.Lock()

    # -- path / cache -----------------------------------------------------
    @property
    def path(self) -> Path:
        return Path(self._resolve())

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def _stamp(self, path: Path) -> Tuple[str, int, int]:
        try:
            stat = path.stat()
            return (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return (str(path), -1, -1)

    # -- corruption -------------------------------------------------------
    def _quarantine(self, path: Path) -> Optional[Path]:
        """Preserve an unreadable state file instead of destroying evidence."""
        target = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(path, target)
            return target
        except OSError:
            return None

    # -- read -------------------------------------------------------------
    def load(self) -> Dict[str, Any]:
        path = self.path
        stamp = self._stamp(path)
        with self._lock:
            if self._cache is not None and self._cache.stamp == stamp:
                return dict(self._cache.data)

        if not path.exists():
            data = dict(DEFAULTS)
        else:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("state.json must contain an object")
                data = self._normalize(raw)
            except Exception:
                quarantined = self._quarantine(path)
                data = dict(DEFAULTS)
                data["_recovered_from"] = str(quarantined) if quarantined else None

        with self._lock:
            self._cache = _CacheEntry(stamp=self._stamp(path), data=dict(data))
        return dict(data)

    def _normalize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Coerce persisted values, repairing invalid entries to their default."""
        data = dict(DEFAULTS)
        for key, spec in REGISTRY.items():
            if key not in raw:
                continue
            value = raw[key]
            if key == "enabled_tools":
                data[key] = migrate_enabled_tools(value)
                continue
            if key == "fallback_model" and value is None:
                # Historical "unset" null. An explicit empty string still means
                # the user intentionally disabled fallback.
                data[key] = DEFAULT_FALLBACK_MODEL
                continue
            try:
                data[key] = coerce(key, value)
            except SettingsError:
                data[key] = spec.default
        data[_SCHEMA_KEY] = SCHEMA_VERSION
        return apply_invariants(data)

    # -- write ------------------------------------------------------------
    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        path = self.path
        payload = {key: value for key, value in data.items() if not key.startswith("_")}
        payload = apply_invariants(payload)
        payload[_SCHEMA_KEY] = SCHEMA_VERSION
        with _file_lock(path):
            atomic_write_private(path, json.dumps(payload, indent=2))
        with self._lock:
            self._cache = _CacheEntry(stamp=self._stamp(path), data=dict(payload))
        return dict(payload)

    def update(self, **changes: Any) -> Dict[str, Any]:
        """Read-modify-write one or more settings under a single lock."""
        path = self.path
        with _file_lock(path):
            data = self.load()
            for name, value in changes.items():
                key = resolve_key(name)
                spec = REGISTRY[key]
                if not spec.mutable:
                    raise SettingsError(f"'{key}' is read-only.")
                data[key] = coerce(key, value)
            payload = {k: v for k, v in data.items() if not k.startswith("_")}
            payload = apply_invariants(payload)
            payload[_SCHEMA_KEY] = SCHEMA_VERSION
            atomic_write_private(path, json.dumps(payload, indent=2))
        with self._lock:
            self._cache = _CacheEntry(stamp=self._stamp(path), data=dict(payload))
        return dict(payload)

    # -- typed API --------------------------------------------------------
    def get(self, name: str) -> Any:
        return self.load().get(resolve_key(name))

    def set(self, name: str, value: Any) -> Dict[str, Any]:
        return self.update(**{resolve_key(name): value})

    def reset(self, name: str) -> Dict[str, Any]:
        key = resolve_key(name)
        return self.update(**{key: REGISTRY[key].default})

    def reset_all(self) -> Dict[str, Any]:
        return self.save(dict(DEFAULTS))


STORE = SettingsStore()


def describe(name: str) -> Dict[str, Any]:
    """Schema entry plus effective value — the payload behind `settings explain`."""
    spec = spec_for(name)
    return {
        "key": spec.key,
        "type": spec.type,
        "group": spec.group,
        "default": spec.default,
        "value": "***" if spec.sensitive else STORE.get(spec.key),
        "choices": spec.choice_list(),
        "minimum": spec.minimum,
        "maximum": spec.maximum,
        "aliases": list(spec.aliases),
        "description": spec.description,
        "mutable": spec.mutable,
        "restart_required": spec.restart_required,
        "security_impact": spec.security_impact,
        "sensitive": spec.sensitive,
    }


def schema() -> List[Dict[str, Any]]:
    """Full machine-readable schema, ordered deterministically by group then key."""
    return [describe(key) for key in sorted(REGISTRY, key=lambda k: (REGISTRY[k].group, k))]
