"""OAuth 2.x Authorization Code + PKCE support for remote MCP servers.

AICoder supports pre-registered OAuth clients. Client secrets, access tokens,
refresh tokens and token expiry are stored exclusively in the OS keyring.
Metadata such as client id, scopes and endpoint URLs remains in the MCP registry.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .mcp_credentials import (
    MCPAuthError, get_mcp_secret, restore_mcp_secrets, set_mcp_secret, snapshot_mcp_secrets,
)

_DISCOVERY_TIMEOUT = 10
_TOKEN_SKEW_SECONDS = 60


def _loopback_http_allowed(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "http" and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


def _validated_endpoint(url: str, label: str) -> str:
    value = str(url or "").strip()
    parsed = urlsplit(value)
    if not parsed.netloc or (parsed.scheme != "https" and not _loopback_http_allowed(value)):
        raise MCPAuthError(f"{label} must use HTTPS (loopback HTTP is allowed for local development)")
    if parsed.username or parsed.password:
        raise MCPAuthError(f"{label} must not contain credentials")
    return value


def _resource_identifier(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


def _json_get(url: str, timeout: int = _DISCOVERY_TIMEOUT) -> dict[str, Any]:
    endpoint = _validated_endpoint(url, "OAuth discovery URL")
    request = Request(endpoint, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_000).decode("utf-8", errors="strict")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise MCPAuthError(f"OAuth discovery failed ({type(exc).__name__})") from exc
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise MCPAuthError("OAuth discovery returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise MCPAuthError("OAuth discovery returned an invalid document")
    return value


def _resource_metadata_from_challenge(resource_url: str) -> dict[str, Any] | None:
    """Resolve RFC9728 metadata advertised by an MCP 401 WWW-Authenticate challenge."""
    endpoint = _validated_endpoint(resource_url, "MCP resource URL")
    request = Request(endpoint, headers={"Accept": "application/json, text/event-stream"}, method="GET")
    try:
        with urlopen(request, timeout=_DISCOVERY_TIMEOUT):
            return None
    except HTTPError as exc:
        if exc.code != 401:
            return None
        challenge = str(exc.headers.get("WWW-Authenticate") or "")
    except (URLError, TimeoutError, OSError):
        return None
    match = re.search(r'(?:^|[,\\s])resource_metadata\\s*=\\s*"([^"\\r\\n]+)"', challenge, re.I)
    if not match:
        return None
    metadata = _json_get(match.group(1))
    advertised_resource = str(metadata.get("resource") or "").rstrip("/")
    expected_resource = _resource_identifier(resource_url).rstrip("/")
    if advertised_resource and advertised_resource != expected_resource:
        raise MCPAuthError("OAuth protected-resource metadata does not match the configured MCP resource")
    return metadata


def _protected_resource_candidates(resource_url: str) -> list[str]:
    parsed = urlsplit(resource_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/"
    candidates = [origin + "/.well-known/oauth-protected-resource" + path]
    root = origin + "/.well-known/oauth-protected-resource"
    if root not in candidates:
        candidates.append(root)
    return candidates


def _authorization_server_candidates(issuer: str) -> list[str]:
    parsed = urlsplit(issuer)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    values = [origin + "/.well-known/oauth-authorization-server" + path]
    oidc = origin + "/.well-known/openid-configuration" + path
    if oidc not in values:
        values.append(oidc)
    return values


def discover_oauth_metadata(config) -> dict[str, Any]:
    """Resolve OAuth endpoints from explicit metadata or MCP/OAuth well-known docs."""
    authorization_url = str(getattr(config, "oauth_authorization_url", "") or "").strip()
    token_url = str(getattr(config, "oauth_token_url", "") or "").strip()
    if authorization_url and token_url:
        return {
            "authorization_endpoint": _validated_endpoint(authorization_url, "OAuth authorization URL"),
            "token_endpoint": _validated_endpoint(token_url, "OAuth token URL"),
        }

    resource_url = _validated_endpoint(str(getattr(config, "url", "") or ""), "MCP resource URL")
    resource_metadata: dict[str, Any] | None = None
    for candidate in _protected_resource_candidates(resource_url):
        try:
            resource_metadata = _json_get(candidate)
            break
        except MCPAuthError:
            continue
    if resource_metadata is None:
        resource_metadata = _resource_metadata_from_challenge(resource_url)
    if resource_metadata is None:
        raise MCPAuthError("OAuth metadata discovery failed; configure authorization and token URLs explicitly")

    authorization_servers = resource_metadata.get("authorization_servers")
    if not isinstance(authorization_servers, list) or not authorization_servers:
        raise MCPAuthError("OAuth protected-resource metadata has no authorization server")
    issuer = _validated_endpoint(str(authorization_servers[0]), "OAuth authorization server")

    server_metadata: dict[str, Any] | None = None
    for candidate in _authorization_server_candidates(issuer):
        try:
            server_metadata = _json_get(candidate)
            break
        except MCPAuthError:
            continue
    if server_metadata is None:
        raise MCPAuthError("OAuth authorization-server metadata discovery failed")

    auth = authorization_url or str(server_metadata.get("authorization_endpoint") or "")
    token = token_url or str(server_metadata.get("token_endpoint") or "")
    if not auth or not token:
        raise MCPAuthError("OAuth discovery did not provide required authorization/token endpoints")
    return {
        "authorization_endpoint": _validated_endpoint(auth, "OAuth authorization URL"),
        "token_endpoint": _validated_endpoint(token, "OAuth token URL"),
        "issuer": issuer,
        "resource_metadata": resource_metadata,
        "authorization_server_metadata": server_metadata,
    }


def _post_token(config, token_url: str, form: dict[str, str]) -> dict[str, Any]:
    client_id = str(getattr(config, "oauth_client_id", "") or "").strip()
    if not client_id:
        raise MCPAuthError("OAuth requires a registered client ID")
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    client_secret = get_mcp_secret(str(getattr(config, "name", "") or ""), "oauth_client_secret")
    if client_secret:
        credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {credentials}"
    else:
        form.setdefault("client_id", client_id)
    request = Request(
        _validated_endpoint(token_url, "OAuth token URL"),
        data=urlencode(form).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(10, min(60, int(getattr(config, "timeout", 30) or 30)))) as response:
            raw = response.read(1_000_000).decode("utf-8", errors="strict")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        # Never include response bodies or URLs here: both may contain sensitive
        # OAuth provider diagnostics or authorization codes.
        raise MCPAuthError(f"OAuth token request failed ({type(exc).__name__})") from exc
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise MCPAuthError("OAuth token endpoint returned invalid JSON") from exc
    if not isinstance(value, dict) or not str(value.get("access_token") or ""):
        raise MCPAuthError("OAuth token endpoint returned no access token")
    return value


def _store_token_response(server: str, payload: dict[str, Any]) -> None:
    access = str(payload.get("access_token") or "")
    if not access:
        raise MCPAuthError("OAuth access token is missing")
    previous = snapshot_mcp_secrets(server, ("oauth_access_token", "oauth_refresh_token", "oauth_expires_at"))
    try:
        set_mcp_secret(server, "oauth_access_token", access)
        refresh = str(payload.get("refresh_token") or "")
        if refresh:
            set_mcp_secret(server, "oauth_refresh_token", refresh)
        expires_in = payload.get("expires_in")
        try:
            seconds = max(0, int(expires_in))
        except (TypeError, ValueError):
            seconds = 0
        if seconds:
            set_mcp_secret(server, "oauth_expires_at", str(int(time.time()) + seconds))
    except Exception:
        restore_mcp_secrets(server, previous, ("oauth_access_token", "oauth_refresh_token", "oauth_expires_at"))
        raise


def refresh_oauth_token(config) -> str:
    server = str(getattr(config, "name", "") or "")
    refresh = get_mcp_secret(server, "oauth_refresh_token")
    if not refresh:
        raise MCPAuthError("OAuth authorization is required; no refresh token is available")
    metadata = discover_oauth_metadata(config)
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "resource": _resource_identifier(str(getattr(config, "url", "") or "")),
    }
    scopes = [str(item).strip() for item in (getattr(config, "oauth_scopes", []) or []) if str(item).strip()]
    if scopes:
        form["scope"] = " ".join(scopes)
    payload = _post_token(config, str(metadata["token_endpoint"]), form)
    _store_token_response(server, payload)
    return str(payload["access_token"])


def oauth_access_token(config) -> str:
    server = str(getattr(config, "name", "") or "")
    token = get_mcp_secret(server, "oauth_access_token")
    expiry_raw = get_mcp_secret(server, "oauth_expires_at")
    expires_at = 0
    try:
        expires_at = int(expiry_raw) if expiry_raw else 0
    except ValueError:
        expires_at = 0
    if token and (not expires_at or expires_at > int(time.time()) + _TOKEN_SKEW_SECONDS):
        return token
    if get_mcp_secret(server, "oauth_refresh_token"):
        return refresh_oauth_token(config)
    raise MCPAuthError("OAuth authorization is required for this MCP server")


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    queue: Queue[dict[str, str]]

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        values = parse_qs(parsed.query, keep_blank_values=True)
        payload = {key: vals[0] for key, vals in values.items() if vals}
        try:
            type(self).queue.put_nowait(payload)
        except Exception:
            pass
        body = b"AICoder OAuth authorization received. You can close this window."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def authorize_oauth(config, *, timeout: int = 180, open_browser: bool = True) -> dict[str, Any]:
    """Run an explicit local Authorization Code + PKCE flow for a registered client."""
    client_id = str(getattr(config, "oauth_client_id", "") or "").strip()
    if not client_id:
        raise MCPAuthError("OAuth requires a registered client ID")
    metadata = discover_oauth_metadata(config)
    authorization_endpoint = str(metadata["authorization_endpoint"])
    token_endpoint = str(metadata["token_endpoint"])

    callback_queue: Queue[dict[str, str]] = Queue(maxsize=1)
    handler = type("AICoderOAuthCallback", (_OAuthCallbackHandler,), {"queue": callback_queue})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    scopes = [str(item).strip() for item in (getattr(config, "oauth_scopes", []) or []) if str(item).strip()]
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": _resource_identifier(str(getattr(config, "url", "") or "")),
    }
    if scopes:
        query["scope"] = " ".join(scopes)
    separator = "&" if "?" in authorization_endpoint else "?"
    authorization_url = authorization_endpoint + separator + urlencode(query)

    try:
        if open_browser:
            try:
                webbrowser.open(authorization_url, new=1, autoraise=True)
            except Exception:
                pass
        try:
            callback = callback_queue.get(timeout=max(10, min(600, int(timeout))))
        except Empty as exc:
            raise MCPAuthError("OAuth authorization timed out") from exc
        if callback.get("state") != state:
            raise MCPAuthError("OAuth callback state validation failed")
        if callback.get("error"):
            raise MCPAuthError("OAuth authorization was rejected by the authorization server")
        code = str(callback.get("code") or "")
        if not code:
            raise MCPAuthError("OAuth callback did not contain an authorization code")
        payload = _post_token(config, token_endpoint, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": _resource_identifier(str(getattr(config, "url", "") or "")),
        })
        _store_token_response(str(getattr(config, "name", "") or ""), payload)
        return {
            "ok": True,
            "authorized": True,
            "refresh_token": bool(payload.get("refresh_token")),
            "expires_in": payload.get("expires_in"),
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
