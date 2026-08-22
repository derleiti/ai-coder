"""Plugin discovery and trusted in-process ToolProvider foundation.

External manifests are declarative in v1.2: they are discovered and validated,
but Python providers are not imported until a later trust/privilege phase.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any, Protocol, runtime_checkable

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

PLUGIN_API_VERSION = "1"
_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class PluginManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ToolSecurity:
    read_only: bool = False
    mutating: bool = True
    destructive: bool = False
    requires_elevation: bool = False
    external_side_effect: bool = True
    security_boundary: bool = False


@dataclass(frozen=True)
class ToolDefinition:
    schema: dict[str, Any]
    capabilities: tuple[str, ...] = ()
    security: ToolSecurity = ToolSecurity()

    @property
    def name(self) -> str:
        return str(self.schema.get("name") or "")


@runtime_checkable
class ToolProvider(Protocol):
    provider_id: str
    def tools(self) -> tuple[ToolDefinition, ...]: ...
    def execute(self, name: str, args: dict[str, Any]) -> tuple[str, bool]: ...
    def security_for(self, name: str, args: dict[str, Any]) -> ToolSecurity: ...


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    name: str
    version: str
    api_version: str
    description: str
    scope: str
    path: Path | None
    capability_groups: tuple[str, ...] = ()
    tool_provider: str | None = None
    trusted_builtin: bool = False


@dataclass
class PluginRecord:
    manifest: PluginManifest
    enabled: bool = True
    provider: ToolProvider | None = None
    executable: bool = False
    conflicts: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def plugin_id(self) -> str: return self.manifest.plugin_id
    @property
    def scope(self) -> str: return self.manifest.scope


class SettingsToolProvider:
    provider_id = "settings"

    def tools(self) -> tuple[ToolDefinition, ...]:
        from .settings_tools import MUTATING_TOOLS, TOOL_SCHEMAS
        result = []
        for schema in TOOL_SCHEMAS:
            name = str(schema.get("name") or "")
            mutating = name in MUTATING_TOOLS
            result.append(ToolDefinition(
                schema=dict(schema), capabilities=("settings",),
                security=ToolSecurity(
                    read_only=not mutating, mutating=mutating,
                    external_side_effect=mutating, security_boundary=mutating,
                ),
            ))
        return tuple(result)

    def execute(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        from .settings_tools import run
        return run(name, args)

    def security_for(self, name: str, args: dict[str, Any]) -> ToolSecurity:
        from .settings_tools import MUTATING_TOOLS, security_change_requested
        known = {tool.name for tool in self.tools()}
        if name not in known:
            return ToolSecurity()  # fail closed for unknown future tools
        mutating = name in MUTATING_TOOLS
        return ToolSecurity(
            read_only=not mutating, mutating=mutating,
            external_side_effect=mutating,
            security_boundary=bool(mutating and security_change_requested(name, args)),
        )


def _config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    return (Path(root) if root else Path.home() / ".config") / "aicoder"


def _strict(table: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise PluginManifestError(f"unknown {label} field(s): {', '.join(sorted(unknown))}")


def _safe_ref(value: str, label: str) -> str:
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise PluginManifestError(f"{label} must stay inside the plugin directory")
    return value


def load_manifest(path: Path, *, scope: str) -> PluginManifest:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PluginManifestError(f"invalid TOML: {exc}") from exc
    _strict(data, {"plugin", "capabilities", "security", "contributions"}, "top-level")
    plugin = data.get("plugin") or {}; caps = data.get("capabilities") or {}
    security = data.get("security") or {}; contrib = data.get("contributions") or {}
    if not all(isinstance(x, dict) for x in (plugin, caps, security, contrib)):
        raise PluginManifestError("manifest sections must be TOML tables")
    _strict(plugin, {"id", "name", "version", "api_version", "description"}, "plugin")
    _strict(caps, {"groups"}, "capabilities")
    _strict(security, {"trusted_builtin"}, "security")
    _strict(contrib, {"tool_provider", "skills_dir", "agents_dir", "commands_dir", "settings_schema"}, "contributions")
    plugin_id = str(plugin.get("id") or "").strip()
    if not _PLUGIN_ID_RE.fullmatch(plugin_id): raise PluginManifestError("invalid plugin.id")
    api_version = str(plugin.get("api_version") or "").strip()
    if api_version != PLUGIN_API_VERSION: raise PluginManifestError(f"unsupported api_version: {api_version}")
    groups = caps.get("groups") or []
    if not isinstance(groups, list) or not all(isinstance(x, str) and x.strip() for x in groups):
        raise PluginManifestError("capabilities.groups must be a string array")
    for key in ("skills_dir", "agents_dir", "commands_dir", "settings_schema"):
        value = contrib.get(key)
        if value is not None:
            if not isinstance(value, str): raise PluginManifestError(f"contributions.{key} must be a string")
            _safe_ref(value.strip(), f"contributions.{key}")
    provider = contrib.get("tool_provider")
    if provider is not None and (not isinstance(provider, str) or not provider.strip()):
        raise PluginManifestError("contributions.tool_provider must be a non-empty string")
    return PluginManifest(
        plugin_id=plugin_id, name=str(plugin.get("name") or plugin_id),
        version=str(plugin.get("version") or "0"), api_version=api_version,
        description=str(plugin.get("description") or ""), scope=scope, path=path,
        capability_groups=tuple(dict.fromkeys(x.strip() for x in groups)),
        tool_provider=provider.strip() if isinstance(provider, str) else None,
        trusted_builtin=bool(security.get("trusted_builtin")) if scope == "builtin" else False,
    )


def _state(config_dir: Path) -> dict[str, Any]:
    path = config_dir / "plugins.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_plugin_enabled(plugin_id: str, enabled: bool, *, config_dir: Path | None = None) -> None:
    config = config_dir or _config_dir(); config.mkdir(parents=True, exist_ok=True)
    data = _state(config); states = data.setdefault("enabled", {}); states[plugin_id] = bool(enabled)
    path = config / "plugins.json"; tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600); os.replace(tmp, path)


class PluginRegistry:
    def __init__(self, records: dict[str, PluginRecord], shadowed: list[PluginRecord] | None = None):
        self.records = records; self.shadowed = shadowed or []

    def get(self, plugin_id: str) -> PluginRecord | None: return self.records.get(plugin_id)
    def all(self) -> list[PluginRecord]: return [self.records[k] for k in sorted(self.records)]
    def providers(self) -> list[ToolProvider]:
        return [r.provider for r in self.all() if r.enabled and r.executable and r.provider is not None]
    def tool_schemas(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for provider in self.providers():
            for tool in provider.tools():
                schema = dict(tool.schema)
                schema["capabilities"] = list(tool.capabilities)
                annotations = dict(schema.get("annotations") or {}) if isinstance(schema.get("annotations"), dict) else {}
                annotations.update({
                    "readOnlyHint": bool(tool.security.read_only),
                    "destructiveHint": bool(tool.security.destructive),
                })
                schema["annotations"] = annotations
                result.append(schema)
        return result
    def provider_for_tool(self, name: str) -> ToolProvider | None:
        for provider in self.providers():
            if any(tool.name == name for tool in provider.tools()): return provider
        return None


def _external_records(root: Path, scope: str) -> list[PluginRecord]:
    if not root.is_dir() or root.is_symlink(): return []
    result = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.is_symlink(): continue
        manifest_path = child / "plugin.toml"
        if not manifest_path.is_file() or manifest_path.is_symlink(): continue
        try:
            manifest = load_manifest(manifest_path, scope=scope)
            rec = PluginRecord(manifest=manifest)
            if manifest.tool_provider:
                rec.diagnostics.append("external Python provider declared but not loaded before trust broker phase")
            result.append(rec)
        except PluginManifestError as exc:
            result.append(PluginRecord(
                manifest=PluginManifest(child.name, child.name, "?", PLUGIN_API_VERSION, "", scope, manifest_path),
                enabled=False, diagnostics=[str(exc)],
            ))
    return result


def discover_plugins(workspace_root: str | Path, *, config_dir: Path | None = None) -> PluginRegistry:
    config = config_dir or _config_dir(); workspace = Path(workspace_root).resolve()
    builtin_manifest = PluginManifest("settings", "Settings", "1.0.0", PLUGIN_API_VERSION,
        "Built-in typed AICoder settings provider", "builtin", None, ("settings",),
        "builtin:settings", True)
    builtin = PluginRecord(builtin_manifest, provider=SettingsToolProvider(), executable=True)
    from .local_os import LocalOSToolProvider
    local_os_manifest = PluginManifest(
        "local-os", "Local OS", "1.0.0", PLUGIN_API_VERSION,
        "Built-in typed local operating-system provider", "builtin", None,
        ("system_diagnostics", "packages", "services", "containers", "network", "storage"),
        "builtin:local-os", True,
    )
    local_os = PluginRecord(local_os_manifest, provider=LocalOSToolProvider(), executable=True)
    candidates = [builtin, local_os]
    candidates += _external_records(config / "plugins", "user")
    candidates += _external_records(workspace / ".aicoder" / "plugins", "workspace")
    winners: dict[str, PluginRecord] = {}; shadowed: list[PluginRecord] = []
    for record in candidates:  # later scopes win deterministically
        previous = winners.get(record.plugin_id)
        if previous is not None:
            previous.conflicts.append(f"shadowed by {record.scope}")
            record.conflicts.append(f"overrides {previous.scope}")
            shadowed.append(previous)
        winners[record.plugin_id] = record
    enabled = _state(config).get("enabled", {})
    if isinstance(enabled, dict):
        for plugin_id, record in winners.items():
            if plugin_id in enabled: record.enabled = bool(enabled[plugin_id])
    return PluginRegistry(winners, shadowed)
