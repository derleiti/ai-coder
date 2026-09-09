"""Canonical MCP configuration service shared by GUI, CLI, REPL and runtime."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .mcp_credentials import (
    SECRET_FIELDS,
    credential_status,
    delete_mcp_secret,
    restore_mcp_secrets,
    set_mcp_secret,
    snapshot_mcp_secrets,
)
from .mcp_registry import (
    MCPRegistry,
    MCPRegistryError,
    MCPServerConfig,
    _validate,
    doctor_server,
    list_server_tools,
)


class MCPServiceError(RuntimeError):
    pass


def _registry(registry: MCPRegistry | None) -> MCPRegistry:
    return registry or MCPRegistry()


def _invalidate() -> None:
    from .executor import invalidate_tool_cache
    invalidate_tool_cache()


def list_servers(registry: MCPRegistry | None = None) -> list[dict[str, Any]]:
    return _registry(registry).list()


def get_server(name: str, registry: MCPRegistry | None = None) -> MCPServerConfig | None:
    return _registry(registry).get(name)


def get_server_view(name: str, registry: MCPRegistry | None = None) -> dict[str, Any]:
    if name == "triforce":
        return {"name": "triforce", "builtin": True, "enabled": True, "transport": "builtin", "trust": "builtin", "credential_status": {}}
    config = get_server(name, registry)
    if config is None:
        raise MCPServiceError(f"unknown MCP server: {name}")
    data = asdict(config)
    data["builtin"] = False
    data["credential_status"] = credential_status(config.name)
    return data


def _credential_field_for(config: MCPServerConfig) -> str | None:
    return {
        "api-key": "api_key",
        "bearer": "bearer_token",
        "basic": "basic_password",
        "custom-header": "custom_header",
    }.get(config.auth_type)


def required_secret_field(config: MCPServerConfig) -> str | None:
    return _credential_field_for(config)


def _cleanup_obsolete_auth_fields(config: MCPServerConfig) -> None:
    keep = {field for field in SECRET_FIELDS if field.startswith("oauth_")} if config.auth_type == "oauth2" else set()
    active = _credential_field_for(config)
    if active:
        keep.add(active)
    present = credential_status(config.name)
    for field in SECRET_FIELDS:
        if field in keep or not present.get(field):
            continue
        delete_mcp_secret(config.name, field)


def save_server(
    config: MCPServerConfig,
    *,
    secrets: dict[str, str] | None = None,
    registry: MCPRegistry | None = None,
    test: bool = True,
) -> dict[str, Any]:
    """Validate, optionally test, then persist config with transactional keyring rollback."""
    registry = _registry(registry)
    if config.name.lower() == "triforce":
        raise MCPServiceError("built-in TriForce profile is managed by AICoder login/settings")
    config = _validate(config)
    previous_config = registry.get(config.name)
    previous_secrets = snapshot_mcp_secrets(config.name)
    supplied = {str(k): str(v) for k, v in (secrets or {}).items() if str(v)}
    try:
        for field, value in supplied.items():
            if field not in SECRET_FIELDS:
                raise MCPServiceError(f"unsupported MCP credential field: {field}")
            set_mcp_secret(config.name, field, value)

        if config.auth_type not in {"none", "oauth2"}:
            required = _credential_field_for(config)
            if required and not credential_status(config.name).get(required):
                raise MCPServiceError(f"required credential is not configured for {config.auth_type}")

        check = doctor_server(config) if test else {
            "name": config.name, "ok": True, "transport": config.transport,
            "tool_count": None, "env_names": list(config.env_names), "error": "",
        }
        if test and not check.get("ok"):
            raise MCPServiceError(str(check.get("error") or "MCP connection test failed"))

        registry.put(config)
        _cleanup_obsolete_auth_fields(config)
        _invalidate()
        return check
    except Exception:
        restore_mcp_secrets(config.name, previous_secrets)
        # Registry is written only after a successful test. If persistence itself
        # fails after replacing an existing entry, restore that previous metadata.
        try:
            current = registry.get(config.name)
            if previous_config is None and current is not None:
                registry.remove(config.name)
            elif previous_config is not None and current != previous_config:
                registry.put(previous_config)
        except Exception:
            pass
        raise


def create_server(config: MCPServerConfig, **kwargs: Any) -> dict[str, Any]:
    registry = _registry(kwargs.get("registry"))
    if registry.get(config.name) is not None:
        raise MCPServiceError(f"MCP server already exists: {config.name}")
    return save_server(config, **kwargs)


def update_server(config: MCPServerConfig, **kwargs: Any) -> dict[str, Any]:
    registry = _registry(kwargs.get("registry"))
    if registry.get(config.name) is None:
        raise MCPServiceError(f"unknown MCP server: {config.name}")
    return save_server(config, **kwargs)



def authorize_and_save_server(
    config: MCPServerConfig,
    *,
    secrets: dict[str, str] | None = None,
    registry: MCPRegistry | None = None,
    oauth_timeout: int = 180,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Authorize OAuth, test the MCP connection, and persist as one rollback unit."""
    registry = _registry(registry)
    config = _validate(config)
    if config.auth_type != "oauth2":
        return save_server(config, secrets=secrets, registry=registry, test=True)
    previous_config = registry.get(config.name)
    previous_secrets = snapshot_mcp_secrets(config.name)
    try:
        for field, value in (secrets or {}).items():
            if value:
                if field not in SECRET_FIELDS:
                    raise MCPServiceError(f"unsupported MCP credential field: {field}")
                set_mcp_secret(config.name, field, value)
        from .mcp_oauth import authorize_oauth as _authorize
        auth_result = _authorize(config, timeout=oauth_timeout, open_browser=open_browser)
        check = save_server(config, registry=registry, test=True)
        return {**check, "oauth": auth_result}
    except Exception:
        restore_mcp_secrets(config.name, previous_secrets)
        try:
            current = registry.get(config.name)
            if previous_config is None and current is not None:
                registry.remove(config.name)
            elif previous_config is not None and current != previous_config:
                registry.put(previous_config)
        except Exception:
            pass
        raise

def remove_server(name: str, registry: MCPRegistry | None = None) -> bool:
    registry = _registry(registry)
    if name.lower() == "triforce":
        raise MCPServiceError("built-in TriForce profile cannot be removed")
    previous_config = registry.get(name)
    if previous_config is None:
        return False
    previous_secrets = snapshot_mcp_secrets(name)
    removed = registry.remove(name)
    if not removed:
        return False
    try:
        present = credential_status(name)
        for field in SECRET_FIELDS:
            if present.get(field):
                delete_mcp_secret(name, field)
        _invalidate()
        return True
    except Exception:
        registry.put(previous_config)
        restore_mcp_secrets(name, previous_secrets)
        raise


def set_server_enabled(name: str, enabled: bool, registry: MCPRegistry | None = None) -> MCPServerConfig:
    if name.lower() == "triforce":
        raise MCPServiceError("built-in TriForce profile is controlled by AICoder login/RBAC")
    result = _registry(registry).set_enabled(name, enabled)
    _invalidate()
    return result


def enable_server(name: str, registry: MCPRegistry | None = None) -> MCPServerConfig:
    return set_server_enabled(name, True, registry)


def disable_server(name: str, registry: MCPRegistry | None = None) -> MCPServerConfig:
    return set_server_enabled(name, False, registry)


def test_config(config: MCPServerConfig) -> dict[str, Any]:
    return doctor_server(_validate(config))


def test_candidate(config: MCPServerConfig, *, secrets: dict[str, str] | None = None) -> dict[str, Any]:
    """Test unsaved editor data without persisting registry metadata or credentials."""
    config = _validate(config)
    previous = snapshot_mcp_secrets(config.name)
    try:
        for field, value in (secrets or {}).items():
            if field not in SECRET_FIELDS:
                raise MCPServiceError(f"unsupported MCP credential field: {field}")
            if value:
                set_mcp_secret(config.name, field, value)
        return doctor_server(config)
    finally:
        restore_mcp_secrets(config.name, previous)


def test_server(name: str, registry: MCPRegistry | None = None) -> dict[str, Any]:
    config = _registry(registry).get(name)
    if config is None:
        raise MCPServiceError(f"unknown MCP server: {name}")
    return doctor_server(config)


def doctor(name: str | None = None, registry: MCPRegistry | None = None) -> dict[str, Any] | list[dict[str, Any]]:
    registry = _registry(registry)
    if name:
        if name == "triforce":
            return {"name": "triforce", "ok": True, "transport": "builtin", "managed_by": "login/rbac"}
        return test_server(name, registry)
    rows = []
    for row in registry.list(include_builtin=False):
        config = MCPServerConfig.from_dict(row)
        rows.append(doctor_server(config))
    return rows


def server_tools(name: str, registry: MCPRegistry | None = None) -> list[dict[str, Any]]:
    config = _registry(registry).get(name)
    if config is None:
        raise MCPServiceError(f"unknown MCP server: {name}")
    if not config.enabled:
        return []
    return list_server_tools(config)


def authentication_status(name: str, registry: MCPRegistry | None = None) -> dict[str, Any]:
    config = _registry(registry).get(name)
    if config is None:
        raise MCPServiceError(f"unknown MCP server: {name}")
    present = credential_status(name)
    required = _credential_field_for(config)
    if config.auth_type == "oauth2":
        configured = bool(present.get("oauth_access_token") or present.get("oauth_refresh_token"))
    elif required:
        configured = bool(present.get(required))
    else:
        configured = True
    return {"name": name, "auth_type": config.auth_type, "configured": configured, "credential_status": present}


def authorize_oauth(name: str, registry: MCPRegistry | None = None, **kwargs: Any) -> dict[str, Any]:
    config = _registry(registry).get(name)
    if config is None:
        raise MCPServiceError(f"unknown MCP server: {name}")
    if config.auth_type != "oauth2":
        raise MCPServiceError("server is not configured for OAuth")
    previous = snapshot_mcp_secrets(name)
    from .mcp_oauth import authorize_oauth as _authorize
    try:
        result = _authorize(config, **kwargs)
        check = doctor_server(config)
        if not check.get("ok"):
            raise MCPServiceError(str(check.get("error") or "MCP connection test failed after OAuth authorization"))
        _invalidate()
        return {**result, **check}
    except Exception:
        restore_mcp_secrets(name, previous)
        raise


def external_tool_schemas(registry: MCPRegistry | None = None) -> list[dict[str, Any]]:
    """Runtime-facing schemas routed through the canonical configuration service."""
    from .mcp_registry import external_tool_schemas as _schemas
    return _schemas(_registry(registry))


def call_external_tool(
    name: str, args: dict[str, Any], registry: MCPRegistry | None = None
) -> tuple[str, bool]:
    """Runtime-facing call routed through the canonical configuration service."""
    from .mcp_registry import call_external_tool as _call
    return _call(name, args, _registry(registry))
