from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .config import CONFIG_DIR, atomic_write_private
from .session_state import (
    SETTINGS,
    apply_settings_patch,
    describe_setting,
    list_settings,
    plan_settings_patch,
    resolve_setting_key,
)

PLUGIN_API_VERSION = "1"
PLUGIN_MANIFEST_NAME = "plugin.toml"
PLUGIN_STATE_FILE = CONFIG_DIR / "plugins.json"
_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SCOPE_RANK = {"builtin": 0, "user": 1, "workspace": 2}
_ALLOWED_TOP_LEVEL = {"plugin", "capabilities", "security", "contributions"}
_ALLOWED_PLUGIN_FIELDS = {"id", "name", "version", "api_version", "description"}
_ALLOWED_CAPABILITY_FIELDS = {"groups"}
_ALLOWED_SECURITY_FIELDS = {"trusted_builtin"}
_ALLOWED_CONTRIBUTION_FIELDS = {
    "tool_provider", "skills_dir", "agents_dir", "commands_dir", "hooks",
    "settings_schema", "mcp_servers",
}


class PluginError(RuntimeError):
    pass


class PluginManifestError(PluginError):
    pass


@dataclass(frozen=True)
class ToolSecurity:
    read_only: bool = False
    mutating: bool = True
    destructive: bool = False
    requires_elevation: bool = False
    network_access: bool = False
    external_side_effect: bool = True
    user_data_sensitive: bool = False
    security_boundary: bool = False

    def as_annotations(self) -> dict[str, bool]:
        return {
            "readOnlyHint": self.read_only,
            "mutating": self.mutating,
            "destructiveHint": self.destructive,
            "requiresElevation": self.requires_elevation,
            "networkAccess": self.network_access,
            "externalSideEffect": self.external_side_effect,
            "userDataSensitive": self.user_data_sensitive,
            "securityBoundary": self.security_boundary,
        }


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    security: ToolSecurity = field(default_factory=ToolSecurity)
    capability_groups: tuple[str, ...] = ()

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.security.as_annotations(),
            "x-aicoder": {"capabilities": list(self.capability_groups)},
        }


@runtime_checkable
class ToolProvider(Protocol):
    provider_id: str

    def tools(self) -> list[ToolDefinition]: ...

    def execute(self, name: str, args: dict[str, Any], *, confirmed: bool = False) -> tuple[str, bool]: ...

    def security_for(self, name: str, args: dict[str, Any]) -> ToolSecurity: ...


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    name: str
    version: str
    api_version: str
    description: str
    capability_groups: tuple[str, ...] = ()
    trusted_builtin: bool = False
    tool_provider: str | None = None
    skills_dir: str | None = None
    agents_dir: str | None = None
    commands_dir: str | None = None
    hooks: tuple[str, ...] = ()
    settings_schema: str | None = None
    mcp_servers: tuple[str, ...] = ()


@dataclass
class PluginRecord:
    manifest: PluginManifest
    scope: str
    path: Path | None
    enabled: bool = True
    executable: bool = False
    provider: ToolProvider | None = None
    conflicts: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id


class SettingsToolProvider:
    provider_id = "settings"

    _TOOLS = (
        ToolDefinition(
            "settings_list", "List AICoder runtime settings and non-secret metadata.",
            {"type": "object", "properties": {}},
            ToolSecurity(read_only=True, mutating=False, external_side_effect=False),
            ("settings", "settings.read"),
        ),
        ToolDefinition(
            "settings_describe", "Describe one AICoder setting.",
            {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
            ToolSecurity(read_only=True, mutating=False, external_side_effect=False),
            ("settings", "settings.read"),
        ),
        ToolDefinition(
            "settings_get", "Read one AICoder setting.",
            {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
            ToolSecurity(read_only=True, mutating=False, external_side_effect=False),
            ("settings", "settings.read"),
        ),
        ToolDefinition(
            "settings_plan_patch", "Validate a proposed settings patch without changing state.",
            {"type": "object", "properties": {"changes": {"type": "object"}}, "required": ["changes"]},
            ToolSecurity(read_only=True, mutating=False, external_side_effect=False),
            ("settings", "settings.write"),
        ),
        ToolDefinition(
            "settings_apply_patch", "Apply a validated AICoder settings patch through host approval policy.",
            {"type": "object", "properties": {"changes": {"type": "object"}, "reason": {"type": "string"}}, "required": ["changes"]},
            ToolSecurity(read_only=False, mutating=True, external_side_effect=True),
            ("settings", "settings.write"),
        ),
        ToolDefinition(
            "settings_reset", "Reset one AICoder setting, or all settings, to canonical defaults.",
            {"type": "object", "properties": {"key": {"type": "string"}, "reason": {"type": "string"}}, "required": ["key"]},
            ToolSecurity(read_only=False, mutating=True, external_side_effect=True),
            ("settings", "settings.write"),
        ),
    )

    def tools(self) -> list[ToolDefinition]:
        return list(self._TOOLS)

    @staticmethod
    def _changes(args: dict[str, Any]) -> dict[str, Any]:
        changes = args.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("changes must be a non-empty object")
        return changes

    @staticmethod
    def _reset_values(args: dict[str, Any]) -> dict[str, Any]:
        raw_key = str(args.get("key") or "").strip()
        if not raw_key:
            raise ValueError("key is required")
        if raw_key.lower() == "all":
            return {key: spec.default for key, spec in SETTINGS.items()}
        key = resolve_setting_key(raw_key)
        return {key: SETTINGS[key].default}

    def security_for(self, name: str, args: dict[str, Any]) -> ToolSecurity:
        definition = next((tool for tool in self._TOOLS if tool.name == name), None)
        if definition is None:
            return ToolSecurity()
        security = definition.security
        if name not in {"settings_apply_patch", "settings_reset"}:
            return security
        try:
            proposed = self._changes(args) if name == "settings_apply_patch" else self._reset_values(args)
            plan = plan_settings_patch(proposed)
        except ValueError:
            return security
        if not plan.get("requires_confirmation"):
            return security
        return ToolSecurity(
            read_only=False,
            mutating=True,
            destructive=security.destructive,
            requires_elevation=security.requires_elevation,
            network_access=security.network_access,
            external_side_effect=True,
            user_data_sensitive=security.user_data_sensitive,
            security_boundary=True,
        )

    def execute(self, name: str, args: dict[str, Any], *, confirmed: bool = False) -> tuple[str, bool]:
        try:
            if name == "settings_list":
                payload: Any = list_settings()
            elif name == "settings_describe":
                payload = describe_setting(str(args.get("key") or ""))
            elif name == "settings_get":
                row = describe_setting(str(args.get("key") or ""))
                payload = {"key": row["key"], "value": row["value"]}
            elif name == "settings_plan_patch":
                payload = plan_settings_patch(self._changes(args))
            elif name == "settings_apply_patch":
                payload = apply_settings_patch(self._changes(args), security_confirmed=confirmed)
            elif name == "settings_reset":
                payload = apply_settings_patch(self._reset_values(args), security_confirmed=confirmed)
            else:
                return f"{name}: unknown settings tool", True
            return json.dumps(payload, ensure_ascii=False, sort_keys=True), False
        except (ValueError, PermissionError) as exc:
            return f"{name} error: {exc}", True


def _builtin_settings_manifest() -> PluginManifest:
    return PluginManifest(
        plugin_id="settings",
        name="AICoder Settings",
        version="1.0.0",
        api_version=PLUGIN_API_VERSION,
        description="Canonical SettingsStore tools and schema access.",
        capability_groups=("settings", "settings.read", "settings.write"),
        trusted_builtin=True,
        tool_provider="builtin:settings",
    )


def _toml_load(path: Path) -> dict[str, Any]:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 only
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError as exc:
            raise PluginManifestError("TOML plugin manifests require tomli on Python 3.10") from exc
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as exc:
        raise PluginManifestError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginManifestError(f"manifest root must be a table: {path}")
    return data


def _strict_fields(table: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise PluginManifestError(f"unknown {label} field(s): {', '.join(sorted(unknown))}")


def _safe_relative_ref(value: str, label: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise PluginManifestError(f"{label} must stay inside the plugin directory")
    if not candidate.parts or any(part in {"", "."} for part in candidate.parts):
        raise PluginManifestError(f"{label} must be a non-empty relative path")
    return value


def load_manifest(path: Path, *, scope: str) -> PluginManifest:
    data = _toml_load(path)
    unknown_sections = set(data) - _ALLOWED_TOP_LEVEL
    if unknown_sections:
        raise PluginManifestError(f"unknown manifest section(s): {', '.join(sorted(unknown_sections))}")
    plugin = data.get("plugin")
    if not isinstance(plugin, dict):
        raise PluginManifestError("missing [plugin] table")
    _strict_fields(plugin, _ALLOWED_PLUGIN_FIELDS, "plugin")
    required = ("id", "name", "version", "api_version", "description")
    missing = [key for key in required if not isinstance(plugin.get(key), str) or not plugin.get(key).strip()]
    if missing:
        raise PluginManifestError(f"missing plugin field(s): {', '.join(missing)}")
    plugin_id = plugin["id"].strip()
    if not _PLUGIN_ID_RE.fullmatch(plugin_id):
        raise PluginManifestError(f"invalid plugin id: {plugin_id}")
    if plugin["api_version"].strip() != PLUGIN_API_VERSION:
        raise PluginManifestError(
            f"unsupported plugin api_version {plugin['api_version']!r}; expected {PLUGIN_API_VERSION!r}"
        )

    capabilities = data.get("capabilities") or {}
    security = data.get("security") or {}
    contributions = data.get("contributions") or {}
    if not all(isinstance(item, dict) for item in (capabilities, security, contributions)):
        raise PluginManifestError("capabilities/security/contributions must be TOML tables")
    _strict_fields(capabilities, _ALLOWED_CAPABILITY_FIELDS, "capabilities")
    _strict_fields(security, _ALLOWED_SECURITY_FIELDS, "security")
    _strict_fields(contributions, _ALLOWED_CONTRIBUTION_FIELDS, "contributions")

    groups = capabilities.get("groups") or []
    hooks = contributions.get("hooks") or []
    mcp_servers = contributions.get("mcp_servers") or []
    for label, value in (("capabilities.groups", groups), ("contributions.hooks", hooks), ("contributions.mcp_servers", mcp_servers)):
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise PluginManifestError(f"{label} must be an array of non-empty strings")

    trusted_declared = security.get("trusted_builtin", False)
    if not isinstance(trusted_declared, bool):
        raise PluginManifestError("security.trusted_builtin must be boolean")
    # External manifests cannot self-assert built-in trust.
    trusted_builtin = bool(trusted_declared and scope == "builtin")

    scalar_refs: dict[str, str | None] = {}
    for key in ("tool_provider", "skills_dir", "agents_dir", "commands_dir", "settings_schema"):
        value = contributions.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise PluginManifestError(f"contributions.{key} must be a non-empty string")
        normalized = value.strip() if isinstance(value, str) else None
        if normalized is not None and key in {"skills_dir", "agents_dir", "commands_dir", "settings_schema"}:
            normalized = _safe_relative_ref(normalized, f"contributions.{key}")
        scalar_refs[key] = normalized

    return PluginManifest(
        plugin_id=plugin_id,
        name=plugin["name"].strip(),
        version=plugin["version"].strip(),
        api_version=plugin["api_version"].strip(),
        description=plugin["description"].strip(),
        capability_groups=tuple(dict.fromkeys(item.strip() for item in groups)),
        trusted_builtin=trusted_builtin,
        tool_provider=scalar_refs["tool_provider"],
        skills_dir=scalar_refs["skills_dir"],
        agents_dir=scalar_refs["agents_dir"],
        commands_dir=scalar_refs["commands_dir"],
        hooks=tuple(item.strip() for item in hooks),
        settings_schema=scalar_refs["settings_schema"],
        mcp_servers=tuple(item.strip() for item in mcp_servers),
    )


def plugin_paths(workspace: str | Path, *, config_dir: Path | None = None) -> list[tuple[str, Path | None]]:
    config = Path(config_dir or CONFIG_DIR)
    ws = Path(workspace).resolve()
    return [
        ("builtin", None),
        ("user", config / "plugins"),
        ("workspace", ws / ".aicoder" / "plugins"),
    ]


def _load_plugin_state(config_dir: Path) -> dict[str, bool]:
    path = config_dir / "plugins.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    enabled = raw.get("enabled") if isinstance(raw, dict) else None
    if not isinstance(enabled, dict):
        return {}
    return {str(key): bool(value) for key, value in enabled.items() if isinstance(value, bool)}


def set_plugin_enabled(plugin_id: str, enabled: bool, *, config_dir: Path | None = None) -> None:
    if not _PLUGIN_ID_RE.fullmatch(str(plugin_id or "")):
        raise PluginError(f"invalid plugin id: {plugin_id}")
    config = Path(config_dir or CONFIG_DIR)
    config.mkdir(parents=True, exist_ok=True)
    state = _load_plugin_state(config)
    state[plugin_id] = bool(enabled)
    path = config / "plugins.json"
    # atomic_write_private uses the canonical config directory helper; ensure the
    # custom test/config path exists and use an equivalent private atomic write.
    if config == CONFIG_DIR:
        atomic_write_private(path, json.dumps({"enabled": state}, indent=2, sort_keys=True) + "\n")
        return
    import tempfile
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(config))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"enabled": state}, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


class PluginRegistry:
    def __init__(self, workspace: str | Path, *, config_dir: Path | None = None):
        self.workspace = Path(workspace).resolve()
        self.config_dir = Path(config_dir or CONFIG_DIR)
        self.records: dict[str, PluginRecord] = {}
        self.shadowed: list[PluginRecord] = []
        self.errors: list[str] = []
        self._tool_index: dict[str, tuple[PluginRecord, ToolProvider, ToolDefinition]] = {}

    def discover(self) -> "PluginRegistry":
        self.records.clear()
        self.shadowed.clear()
        self.errors.clear()
        state = _load_plugin_state(self.config_dir)

        builtin_provider = SettingsToolProvider()
        builtin = PluginRecord(
            manifest=_builtin_settings_manifest(), scope="builtin", path=None,
            enabled=state.get("settings", True), executable=True, provider=builtin_provider,
        )
        self._merge(builtin)

        for scope, root in plugin_paths(self.workspace, config_dir=self.config_dir):
            if scope == "builtin" or root is None or not root.is_dir():
                continue
            try:
                entries = sorted(root.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                self.errors.append(f"{scope} plugin directory unreadable: {exc}")
                continue
            for entry in entries:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                manifest_path = entry / PLUGIN_MANIFEST_NAME
                if manifest_path.is_symlink() or not manifest_path.is_file():
                    continue
                try:
                    manifest = load_manifest(manifest_path, scope=scope)
                    if manifest.plugin_id != entry.name:
                        raise PluginManifestError(
                            f"plugin id {manifest.plugin_id!r} must match directory name {entry.name!r}"
                        )
                except PluginManifestError as exc:
                    self.errors.append(f"{manifest_path}: {exc}")
                    continue
                record = PluginRecord(
                    manifest=manifest,
                    scope=scope,
                    path=entry.resolve(),
                    enabled=state.get(manifest.plugin_id, True),
                    executable=False,
                )
                if manifest.tool_provider:
                    record.diagnostics.append(
                        "external tool_provider discovered but not loaded: executable plugin loading is disabled until trust/PrivilegeBroker hardening is complete"
                    )
                self._merge(record)

        self._rebuild_tool_index()
        return self

    def _merge(self, candidate: PluginRecord) -> None:
        current = self.records.get(candidate.plugin_id)
        if current is None:
            self.records[candidate.plugin_id] = candidate
            return
        winner, loser = (candidate, current) if _SCOPE_RANK[candidate.scope] > _SCOPE_RANK[current.scope] else (current, candidate)
        msg = f"{candidate.plugin_id}: {winner.scope} overrides {loser.scope}"
        winner.conflicts.append(msg)
        loser.conflicts.append(msg)
        self.records[candidate.plugin_id] = winner
        self.shadowed.append(loser)

    def _rebuild_tool_index(self) -> None:
        self._tool_index.clear()
        for record in self.records.values():
            if not record.enabled or not record.executable or record.provider is None:
                continue
            for definition in record.provider.tools():
                if definition.name in self._tool_index:
                    record.diagnostics.append(f"duplicate tool ignored: {definition.name}")
                    continue
                self._tool_index[definition.name] = (record, record.provider, definition)

    def list(self) -> list[PluginRecord]:
        return sorted(self.records.values(), key=lambda record: record.plugin_id)

    def get(self, plugin_id: str) -> PluginRecord | None:
        return self.records.get(plugin_id)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [definition.schema() for _, _, definition in self._tool_index.values()]

    def provider_for_tool(self, name: str) -> ToolProvider | None:
        item = self._tool_index.get(name)
        return item[1] if item else None

    def security_for_tool(self, name: str, args: dict[str, Any]) -> ToolSecurity | None:
        item = self._tool_index.get(name)
        if item is None:
            return None
        return item[1].security_for(name, args)

    def execute_tool(self, name: str, args: dict[str, Any], *, confirmed: bool = False) -> tuple[str, bool]:
        provider = self.provider_for_tool(name)
        if provider is None:
            return f"{name}: no enabled ToolProvider", True
        return provider.execute(name, args, confirmed=confirmed)

    def doctor(self, plugin_id: str | None = None) -> dict[str, Any]:
        records = [self.records[plugin_id]] if plugin_id and plugin_id in self.records else self.list()
        if plugin_id and plugin_id not in self.records:
            return {"ok": False, "error": f"plugin not found: {plugin_id}", "plugins": []}
        rows = []
        for record in records:
            rows.append({
                "id": record.plugin_id,
                "scope": record.scope,
                "enabled": record.enabled,
                "executable": record.executable,
                "trusted_builtin": record.manifest.trusted_builtin,
                "api_version": record.manifest.api_version,
                "path": str(record.path) if record.path else "<builtin>",
                "conflicts": list(record.conflicts),
                "diagnostics": list(record.diagnostics),
            })
        return {"ok": not self.errors, "errors": list(self.errors), "plugins": rows}


def plugin_catalog_stamp(workspace: str | Path, *, config_dir: Path | None = None) -> str:
    """Content stamp used to invalidate the runtime tool catalogue on plugin changes."""
    import hashlib
    digest = hashlib.sha256()
    config = Path(config_dir or CONFIG_DIR)
    state_path = config / "plugins.json"
    for label, path in [("state", state_path)]:
        digest.update(label.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    for scope, root in plugin_paths(workspace, config_dir=config):
        digest.update(scope.encode("utf-8"))
        if root is None or not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                continue
            manifest = entry / PLUGIN_MANIFEST_NAME
            if manifest.is_symlink() or not manifest.is_file():
                continue
            digest.update(entry.name.encode("utf-8"))
            try:
                digest.update(manifest.read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
    return digest.hexdigest()[:20]


def discover_plugins(workspace: str | Path, *, config_dir: Path | None = None) -> PluginRegistry:
    return PluginRegistry(workspace, config_dir=config_dir).discover()
