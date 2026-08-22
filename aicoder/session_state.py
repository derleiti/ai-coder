from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import CONFIG_DIR, atomic_write_private

STATE_FILE = CONFIG_DIR / "state.json"
STATE_SCHEMA_VERSION = 1

SWARM_MODES = {"off", "auto", "on", "review"}
TOOL_MODES = {"off", "on_demand", "always"}
APPROVAL_MODES = {"ask", "autopilot", "all"}
RUNTIME_MODES = {"classic", "native-light"}
DEFAULT_RUNTIME_MODE = "native-light"
DEFAULT_FALLBACK_MODEL = "ollama/llama3.2:latest"


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: str
    default: Any
    description: str
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    aliases: tuple[str, ...] = ()
    group: str = "general"
    sensitive: bool = False
    mutable: bool = True
    restart_required: bool = False
    security_impact: str = "none"
    cli_parser: str = "auto"

    def to_schema(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "type": self.kind,
            "default": self.default,
            "choices": list(self.choices),
            "min": self.minimum,
            "max": self.maximum,
            "description": self.description,
            "group": self.group,
            "aliases": list(self.aliases),
            "sensitive": self.sensitive,
            "mutable": self.mutable,
            "restart_required": self.restart_required,
            "security_impact": self.security_impact,
            "cli_parser": self.cli_parser,
        }


SETTINGS: Dict[str, SettingSpec] = {
    "selected_model": SettingSpec(
        "selected_model", "string", None,
        "Primary model as provider/model. Empty uses the backend default.",
        aliases=("model",), group="models",
    ),
    "fallback_model": SettingSpec(
        "fallback_model", "string", DEFAULT_FALLBACK_MODEL,
        "Fallback model. Empty disables fallback.",
        aliases=("fallback",), group="models",
    ),
    "swarm_mode": SettingSpec(
        "swarm_mode", "enum", "off", "Multi-model swarm mode.",
        choices=tuple(sorted(SWARM_MODES)), aliases=("swarm",), group="models",
    ),
    "workspace_root": SettingSpec(
        "workspace_root", "path", None,
        "Active workspace root. This defines the default local filesystem boundary.",
        aliases=("workspace",), group="workspace", security_impact="boundary",
    ),
    "tool_mode": SettingSpec(
        "tool_mode", "enum", "on_demand", "Tool discovery mode.",
        choices=tuple(sorted(TOOL_MODES)), aliases=("tool-mode",), group="tools",
        security_impact="capability",
    ),
    "enabled_tools": SettingSpec(
        "enabled_tools", "list", None,
        "Enabled tool allow-list. 'all' means every discovered tool; 'none' disables every tool.",
        aliases=("tools",), group="tools", security_impact="capability",
        cli_parser="comma_list",
    ),
    "request_timeout": SettingSpec(
        "request_timeout", "int", 300, "LLM request timeout in seconds.",
        minimum=10, maximum=300, aliases=("timeout",), group="runtime",
    ),
    "approval_mode": SettingSpec(
        "approval_mode", "enum", "ask", "Approval policy for mutating actions.",
        choices=tuple(sorted(APPROVAL_MODES)), aliases=("approval", "permissions"),
        group="security", security_impact="boundary",
    ),
    "runtime_mode": SettingSpec(
        "runtime_mode", "enum", DEFAULT_RUNTIME_MODE, "Agent runtime implementation.",
        choices=tuple(sorted(RUNTIME_MODES)), aliases=("runtime",), group="runtime",
    ),
}

_DEFAULTS: Dict[str, Any] = {key: spec.default for key, spec in SETTINGS.items()}
_SETTING_ALIASES = {alias: key for key, spec in SETTINGS.items() for alias in spec.aliases}

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


def resolve_setting_key(key: str) -> str:
    normalized = str(key or "").strip()
    canonical = _SETTING_ALIASES.get(normalized, normalized)
    if canonical not in SETTINGS:
        raise ValueError(f"Unbekannte Einstellung '{key}'.")
    return canonical


def coerce_setting_value(key: str, value: Any) -> Any:
    key = resolve_setting_key(key)
    spec = SETTINGS[key]
    if not spec.mutable:
        raise ValueError(f"{key} ist schreibgeschuetzt.")
    if spec.kind == "int":
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} erwartet eine Ganzzahl.") from exc
        if spec.minimum is not None and parsed < spec.minimum:
            raise ValueError(f"{key} muss >= {spec.minimum} sein.")
        if spec.maximum is not None and parsed > spec.maximum:
            raise ValueError(f"{key} muss <= {spec.maximum} sein.")
        return parsed
    if spec.kind == "enum":
        parsed = str(value).strip()
        if parsed not in spec.choices:
            raise ValueError(f"Ungueltiger Wert '{parsed}' fuer {key}. Erlaubt: {', '.join(spec.choices)}")
        return parsed
    if spec.kind == "list":
        if value is None:
            return None
        if isinstance(value, str):
            raw = value.strip()
            if raw.lower() == "all":
                return None
            if raw.lower() in {"none", ""}:
                return []
            return sorted({part.strip() for part in raw.split(",") if part.strip()})
        if isinstance(value, (list, tuple, set)):
            return sorted({str(part).strip() for part in value if str(part).strip()})
        raise ValueError(f"{key} erwartet 'all', 'none' oder eine kommagetrennte Liste.")
    if spec.kind in {"string", "path"}:
        parsed = "" if value is None else str(value).strip()
        if key in {"selected_model", "workspace_root"}:
            return parsed or None
        return parsed
    return value


def migrate_enabled_tools(value: Any) -> Optional[list[str]]:
    """Convert the obsolete explicit 'all tools' snapshot back to its meaning."""
    if value is None:
        return None
    if not isinstance(value, list):
        return []
    normalized = [str(name) for name in value if isinstance(name, str) and name]
    if frozenset(normalized) == _LEGACY_ALL_TOOLS:
        return None
    return sorted(set(normalized))


def _normalize_loaded_state(data: dict[str, Any]) -> dict[str, Any]:
    state = dict(_DEFAULTS)
    if data.get("fallback_model") is None and "fallback_model" in data:
        data = {**data, "fallback_model": DEFAULT_FALLBACK_MODEL}
    for key, spec in SETTINGS.items():
        if key not in data:
            continue
        value = data[key]
        if key == "enabled_tools":
            state[key] = migrate_enabled_tools(value)
            continue
        try:
            state[key] = coerce_setting_value(key, value)
        except ValueError:
            state[key] = spec.default
    if state.get("fallback_model") == state.get("selected_model"):
        state["fallback_model"] = ""
    return state


# Compatibility cache globals. Existing tests and callers clear these when they
# temporarily redirect STATE_FILE. The SettingsStore owns all actual I/O.
_cache: Dict[str, Any] | None = None
_cache_stamp: tuple[Any, ...] | None = None
_lock = threading.Lock()


@contextmanager
def _process_state_lock(path: Path | None = None):
    """Serialize read-modify-write cycles across GUI/CLI processes."""
    target = Path(path or STATE_FILE)
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _state_stamp(path: Path | None = None) -> tuple[Any, ...]:
    target = Path(path or STATE_FILE)
    try:
        stat = target.stat()
        raw = target.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        return str(target), stat.st_mtime_ns, stat.st_size, digest
    except OSError:
        return str(target), -1, -1, ""


class SettingsStore:
    """Canonical cross-process state store for CLI, REPL, GUI and agent tools."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or STATE_FILE)
        self._preserved_corrupt_hashes: set[str] = set()

    def _preserve_corrupt(self, raw: bytes) -> Path | None:
        digest = hashlib.sha256(raw).hexdigest()[:12]
        if digest in self._preserved_corrupt_hashes:
            return None
        self._preserved_corrupt_hashes.add(digest)
        existing = sorted(self.path.parent.glob(f"{self.path.name}.corrupt-*-{digest}"))
        if existing:
            return existing[-1]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{digest}")
        try:
            shutil.copy2(self.path, backup)
            try:
                os.chmod(backup, 0o600)
            except OSError:
                pass
            return backup
        except OSError:
            return None

    def _read_payload_uncached(self) -> tuple[dict[str, Any], int, int]:
        if not self.path.exists():
            return dict(_DEFAULTS), STATE_SCHEMA_VERSION, 0
        try:
            raw = self.path.read_bytes()
        except OSError:
            return dict(_DEFAULTS), STATE_SCHEMA_VERSION, 0
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("state root must be an object")
        except (UnicodeError, json.JSONDecodeError, ValueError):
            self._preserve_corrupt(raw)
            return dict(_DEFAULTS), STATE_SCHEMA_VERSION, 0

        schema_version = payload.get("_schema_version", 0)
        revision = payload.get("_revision", 0)
        try:
            schema_version = int(schema_version)
        except (TypeError, ValueError):
            schema_version = 0
        try:
            revision = max(0, int(revision))
        except (TypeError, ValueError):
            revision = 0

        state = _normalize_loaded_state(payload)
        return state, schema_version, revision

    def load(self) -> dict[str, Any]:
        global _cache, _cache_stamp
        with _lock:
            stamp = _state_stamp(self.path)
            if _cache is not None and _cache_stamp == stamp:
                return dict(_cache)
            state, _, _ = self._read_payload_uncached()
            _cache = dict(state)
            _cache_stamp = stamp
            return dict(state)

    def _write_locked(self, state: dict[str, Any], revision: int) -> dict[str, Any]:
        global _cache, _cache_stamp
        normalized = _normalize_loaded_state(state)
        payload = {
            "_schema_version": STATE_SCHEMA_VERSION,
            "_revision": max(1, int(revision)),
            **normalized,
        }
        atomic_write_private(self.path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        with _lock:
            _cache = dict(normalized)
            _cache_stamp = _state_stamp(self.path)
        return dict(normalized)

    def update(self, values: Dict[str, Any]) -> dict[str, Any]:
        parsed: Dict[str, Any] = {}
        for raw_key, raw_value in values.items():
            key = resolve_setting_key(raw_key)
            parsed[key] = coerce_setting_value(key, raw_value)

        with _process_state_lock(self.path):
            current, _, revision = self._read_payload_uncached()
            current.update(parsed)
            if current.get("fallback_model") == current.get("selected_model"):
                current["fallback_model"] = ""
            return self._write_locked(current, revision + 1)

    def replace(self, values: Dict[str, Any]) -> dict[str, Any]:
        with _process_state_lock(self.path):
            _, _, revision = self._read_payload_uncached()
            return self._write_locked(values, revision + 1)

    def reset_all(self) -> dict[str, Any]:
        return self.replace(dict(_DEFAULTS))

    def schema(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "settings": [spec.to_schema() for spec in SETTINGS.values()],
        }

    def doctor(self) -> dict[str, Any]:
        state, schema_version, revision = self._read_payload_uncached()
        mode = None
        if self.path.exists():
            try:
                mode = oct(self.path.stat().st_mode & 0o777)
            except OSError:
                mode = None
        return {
            "path": str(self.path),
            "exists": self.path.exists(),
            "schema_version": schema_version,
            "expected_schema_version": STATE_SCHEMA_VERSION,
            "revision": revision,
            "permissions": mode,
            "permissions_ok": mode in {None, "0o600"},
            "settings_count": len(state),
            "corrupt_backups": len(list(self.path.parent.glob(self.path.name + ".corrupt-*"))) if self.path.parent.exists() else 0,
        }


def _store() -> SettingsStore:
    return SettingsStore(STATE_FILE)


def _load_raw() -> Dict[str, Any]:
    return _store().load()


def _save_raw(data: Dict[str, Any]) -> None:
    _store().replace(data)


def get_state() -> Dict[str, Any]:
    return _store().load()


def set_settings(values: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and persist multiple settings in one cross-process transaction."""
    return _store().update(values)


def set_setting(key: str, value: Any) -> Dict[str, Any]:
    return set_settings({key: value})


def reset_setting(key: str) -> Dict[str, Any]:
    key = resolve_setting_key(key)
    return set_setting(key, SETTINGS[key].default)


def reset_all_settings() -> Dict[str, Any]:
    return _store().reset_all()


def settings_schema() -> dict[str, Any]:
    return _store().schema()


def settings_doctor() -> dict[str, Any]:
    return _store().doctor()


def list_settings(*, include_sensitive: bool = False) -> list[dict[str, Any]]:
    state = get_state()
    rows: list[dict[str, Any]] = []
    for key, spec in SETTINGS.items():
        if spec.sensitive and not include_sensitive:
            value: Any = "[REDACTED]"
        else:
            value = state.get(key, spec.default)
        rows.append({**spec.to_schema(), "value": value})
    return rows


def describe_setting(key: str) -> dict[str, Any]:
    canonical = resolve_setting_key(key)
    spec = SETTINGS[canonical]
    value = get_state().get(canonical, spec.default)
    return {**spec.to_schema(), "value": "[REDACTED]" if spec.sensitive else value}


def _security_change_reason(key: str, old: Any, new: Any) -> str | None:
    if old == new:
        return None
    if key == "approval_mode":
        order = {"ask": 0, "autopilot": 1, "all": 2}
        if order.get(str(new), 0) > order.get(str(old), 0):
            return "approval policy becomes more permissive"
        return None
    if key == "tool_mode":
        order = {"off": 0, "on_demand": 1, "always": 2}
        if order.get(str(new), 0) > order.get(str(old), 0):
            return "tool activation policy becomes broader"
        return None
    if key == "enabled_tools":
        if new is None and old is not None:
            return "tool allow-list expands to all discovered tools"
        old_set = set(old or []) if old is not None else None
        new_set = set(new or []) if new is not None else None
        if old_set is not None and new_set is not None and not new_set.issubset(old_set):
            return "tool allow-list enables additional tools"
        return None
    if key == "workspace_root":
        return "workspace filesystem boundary changes"
    return None


def plan_settings_patch(values: Dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict) or not values:
        raise ValueError("settings patch requires a non-empty object")
    current = get_state()
    parsed: dict[str, Any] = {}
    for raw_key, raw_value in values.items():
        key = resolve_setting_key(raw_key)
        spec = SETTINGS[key]
        if spec.sensitive:
            raise ValueError(f"{key} is a credential/secret and is not managed by SettingsStore")
        parsed[key] = coerce_setting_value(key, raw_value)

    proposed = dict(current)
    proposed.update(parsed)
    if proposed.get("fallback_model") == proposed.get("selected_model"):
        proposed["fallback_model"] = ""

    changes: dict[str, dict[str, Any]] = {}
    requires_confirmation = False
    affected = list(parsed)
    if proposed.get("fallback_model") != current.get("fallback_model") and "fallback_model" not in affected:
        affected.append("fallback_model")
    for key in affected:
        old = current.get(key, SETTINGS[key].default)
        new = proposed.get(key, SETTINGS[key].default)
        if old == new:
            continue
        reason = _security_change_reason(key, old, new)
        requires_confirmation = requires_confirmation or bool(reason)
        changes[key] = {
            "old": old,
            "new": new,
            "requested": key in parsed,
            "security_confirmation_required": bool(reason),
            "security_reason": reason,
        }
    return {
        "changes": changes,
        "requires_confirmation": requires_confirmation,
    }


def apply_settings_patch(values: Dict[str, Any], *, security_confirmed: bool = False) -> dict[str, Any]:
    plan = plan_settings_patch(values)
    if plan["requires_confirmation"] and not security_confirmed:
        raise PermissionError("settings patch widens a security boundary and requires explicit user confirmation")
    target = {key: row["new"] for key, row in plan["changes"].items()}
    if target:
        set_settings(target)
    persisted = get_state()
    verified = all(persisted.get(key) == row["new"] for key, row in plan["changes"].items())
    return {
        "changes": {
            key: {"old": row["old"], "new": persisted.get(key), "requested": row["requested"]}
            for key, row in plan["changes"].items()
        },
        "verified": verified,
    }


def set_model(model: str) -> None:
    set_setting("selected_model", model)


def set_fallback(model: str) -> None:
    set_setting("fallback_model", model)


def set_tool_mode(mode: str) -> None:
    set_setting("tool_mode", mode)


def set_enabled_tools(names: Optional[list[str]]) -> None:
    """Persist selected tool names. None means all discovered tools."""
    set_setting("enabled_tools", names)


def set_approval_mode(mode: str) -> None:
    set_setting("approval_mode", mode)


def set_request_timeout(seconds: int) -> None:
    set_setting("request_timeout", seconds)


def set_runtime_mode(mode: str) -> None:
    set_setting("runtime_mode", mode)


def set_swarm(mode: str) -> None:
    set_setting("swarm_mode", mode)


def set_workspace(path: Optional[str]) -> None:
    set_setting("workspace_root", path)
