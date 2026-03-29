from __future__ import annotations
import base64
import json
import ssl
import time
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

USER_AGENT = "ai-coder/0.6.5 (AILinux Coding Client)"


def _ssl_context() -> ssl.SSLContext:
    """SSL context with proper CA certs (fixes PyInstaller on Windows/Android)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    return ssl.create_default_context()


def _decode_jwt_exp(token: str) -> Optional[int]:
    """Decode JWT expiry timestamp without verification (offline check only)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        # Decode payload (part 1), add padding
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("exp")
    except Exception:
        return None


class ClientError(RuntimeError):
    pass


class TokenExpiredError(ClientError):
    """Raised when JWT token is expired and no auto-refresh is possible."""
    pass


class TriForceClient:
    def __init__(self, base_url: str, token: Optional[str] = None, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def token_expires_in(self) -> Optional[float]:
        """Seconds until token expires. None if unknown, negative if expired."""
        if not self.token:
            return None
        exp = _decode_jwt_exp(self.token)
        if exp is None:
            return None
        return exp - time.time()

    def is_token_expired(self) -> bool:
        """Check if token is expired (with 30s grace period)."""
        remaining = self.token_expires_in()
        if remaining is None:
            return False  # Can't check — assume valid
        return remaining < 30  # Expired or expires within 30s

    def token_status(self) -> str:
        """Human-readable token status for UI display."""
        remaining = self.token_expires_in()
        if remaining is None:
            return "unbekannt"
        if remaining < 0:
            return "abgelaufen"
        if remaining < 300:
            m = int(remaining / 60)
            return f"läuft in {m}min ab"
        hours = int(remaining / 3600)
        if hours > 0:
            return f"gültig ({hours}h)"
        return f"gültig ({int(remaining/60)}min)"

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        require_auth: bool = False,
        _label: str = "",
    ) -> Dict[str, Any]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        data = None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if require_auth:
            if not self.token:
                raise ClientError("Kein Token vorhanden. Erst einloggen.")
            # Pre-flight expiry check (saves a round-trip)
            if self.is_token_expired():
                raise TokenExpiredError(
                    "Token abgelaufen. Bitte neu einloggen: aicoder setup"
                )
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = Request(url=url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(req, timeout=self.timeout, context=_ssl_context()) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            try:
                parsed = json.loads(body) if body else {}
            except Exception:
                parsed = {"raw": body}
            label = f" [{_label}]" if _label else ""
            # Detect 401/403 from expired token specifically
            if e.code in (401, 403):
                detail = parsed.get("detail", "") or parsed.get("raw", "")
                if "expire" in str(detail).lower() or "token" in str(detail).lower():
                    raise TokenExpiredError(
                        f"Token abgelaufen (HTTP {e.code}). Bitte neu einloggen: aicoder setup"
                    ) from e
            raise ClientError(f"HTTP {e.code}{label} bei {path}: {parsed}") from e
        except TimeoutError:
            label = f" [{_label}]" if _label else ""
            raise ClientError(
                f"Timeout nach {self.timeout}s{label} bei {path}. "
                "Backend erreichbar? Timeout via --timeout erhöhen."
            )
        except URLError as e:
            raise ClientError(f"Verbindung fehlgeschlagen zu {url}: {e}") from e

    def login(self, email: str, password: str) -> Dict[str, Any]:
        result = self._request(
            "POST", "/v1/auth/login", {"email": email, "password": password},
            require_auth=False, _label="login",
        )
        token = result.get("token")
        if not token:
            raise ClientError(f"Login fehlgeschlagen: {result}")
        self.token = token
        return result

    def verify(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/auth/verify", require_auth=True, _label="verify")

    def handshake(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/auth/client/handshake", require_auth=True, _label="handshake")

    def mcp_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
            "id": 1,
        }
        return self._request("POST", "/v1/mcp", payload, require_auth=True, _label=tool_name)

    def chat(
        self,
        message: str = "",
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        fallback_model: Optional[str] = None,
        messages: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Call /v1/client/chat. Supports messages array for multi-turn context."""
        payload: Dict[str, Any] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if messages:
            payload["messages"] = messages
        else:
            payload["message"] = message
        if model:
            payload["model"] = model
        if system_prompt:
            payload["system_prompt"] = system_prompt
        try:
            return self._request(
                "POST", "/v1/client/chat", payload, require_auth=True,
                _label=f"chat/{model or 'default'}"
            )
        except ClientError as e:
            if fallback_model and fallback_model != model:
                import sys
                print(f"\n[FALLBACK: {model} failed → {fallback_model}]", file=sys.stderr)
                payload["model"] = fallback_model
                return self._request(
                    "POST", "/v1/client/chat", payload, require_auth=True,
                    _label=f"chat/{fallback_model}(fallback)"
                )
            raise
