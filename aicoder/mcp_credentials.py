"""Secret storage and HTTP authentication for user-managed MCP servers.

Persistent MCP configuration contains metadata only. Every secret value is kept
in the operating-system secret store and is materialized only for the outbound
request that needs it.
"""
from __future__ import annotations

import base64
from typing import Iterable

from .provider_credentials import CredentialStoreError, _secret_delete, _secret_get, _secret_set

SERVICE_NAME = "ailinux.aicoder.mcp-credentials"

# Keep the list explicit so remove/rollback operations can never accidentally
# sweep unrelated keyring entries.
SECRET_FIELDS = (
    "api_key",
    "bearer_token",
    "basic_password",
    "custom_header",
    "oauth_client_secret",
    "oauth_access_token",
    "oauth_refresh_token",
    "oauth_expires_at",
    # Legacy prototype fields. They are read only for Bearer migration and
    # cleaned when a server is saved/removed through the shared service.
    "token",
    "password",
    "client_secret",
)


class MCPAuthError(RuntimeError):
    """Authentication configuration/credential error with secret-safe text."""


def _account(server: str, field: str) -> str:
    server = str(server or "").strip()
    field = str(field or "").strip()
    if not server or not field:
        raise CredentialStoreError("MCP server and credential field are required")
    return f"{server}:{field}"


def set_mcp_secret(server: str, field: str, value: str) -> None:
    secret = str(value or "")
    if not secret:
        raise CredentialStoreError("MCP credential must not be empty")
    try:
        _secret_set(SERVICE_NAME, _account(server, field), secret)
    except CredentialStoreError as exc:
        raise CredentialStoreError(f"Could not store MCP credential: {exc}") from exc


def get_mcp_secret(server: str, field: str) -> str:
    try:
        return _secret_get(SERVICE_NAME, _account(server, field))
    except CredentialStoreError:
        # Reads are deliberately quiet so status screens can remain usable on a
        # headless machine with no secret-service backend. Writes fail closed.
        return ""


def delete_mcp_secret(server: str, field: str) -> bool:
    try:
        return _secret_delete(SERVICE_NAME, _account(server, field))
    except CredentialStoreError as exc:
        raise CredentialStoreError(f"Could not delete MCP credential: {exc}") from exc


def snapshot_mcp_secrets(server: str, fields: Iterable[str] = SECRET_FIELDS) -> dict[str, str]:
    """Take an in-memory snapshot for transactional rollback; never serialize it."""
    return {field: value for field in fields if (value := get_mcp_secret(server, field))}


def restore_mcp_secrets(
    server: str,
    snapshot: dict[str, str],
    fields: Iterable[str] = SECRET_FIELDS,
) -> None:
    """Restore a previous keyring state after a failed save/test operation."""
    wanted = set(fields)
    for field in wanted:
        current = get_mcp_secret(server, field)
        previous = snapshot.get(field, "")
        if previous:
            if current != previous:
                set_mcp_secret(server, field, previous)
        elif current:
            try:
                delete_mcp_secret(server, field)
            except CredentialStoreError:
                # Rollback is best effort per field, but callers still receive
                # their original operation error and no plaintext fallback is used.
                pass


def credential_status(server: str) -> dict[str, bool]:
    """Return presence flags only; never expose secret-store values."""
    return {field: bool(get_mcp_secret(server, field)) for field in SECRET_FIELDS}


def _required_secret(server: str, field: str, label: str) -> str:
    value = get_mcp_secret(server, field)
    if value:
        return value
    raise MCPAuthError(f"{label} is not configured in the OS secret store")


def auth_headers(config) -> dict[str, str]:
    """Materialize authentication headers for one outbound HTTP request."""
    mode = str(getattr(config, "auth_type", "none") or "none").lower()
    server = str(getattr(config, "name", "") or "")
    if mode == "none":
        return {}
    if mode == "api-key":
        header = str(getattr(config, "auth_header", "") or "X-API-Key")
        return {header: _required_secret(server, "api_key", "API key")}
    if mode == "bearer":
        token = get_mcp_secret(server, "bearer_token") or get_mcp_secret(server, "token")
        if not token:
            raise MCPAuthError("Bearer token is not configured in the OS secret store")
        return {"Authorization": f"Bearer {token}"}
    if mode == "basic":
        username = str(getattr(config, "auth_username", "") or "")
        if not username:
            raise MCPAuthError("Basic authentication requires a username")
        password = get_mcp_secret(server, "basic_password") or get_mcp_secret(server, "password")
        if not password:
            raise MCPAuthError("Basic password is not configured in the OS secret store")
        encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    if mode == "custom-header":
        header = str(getattr(config, "auth_header", "") or "")
        return {header: _required_secret(server, "custom_header", "Custom header credential")}
    if mode == "oauth2":
        # OAuth is intentionally not a static bearer-token alias. The OAuth
        # module owns access-token expiry/refresh and explicit authorization.
        from .mcp_oauth import oauth_access_token

        return {"Authorization": f"Bearer {oauth_access_token(config)}"}
    raise MCPAuthError(f"Unsupported MCP authentication mode: {mode}")
