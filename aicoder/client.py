from __future__ import annotations
import json
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

USER_AGENT = "ai-coder/0.2 (AILinux Coding Client)"


class ClientError(RuntimeError):
    pass


class TriForceClient:
    def __init__(self, base_url: str, token: Optional[str] = None, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

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
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = Request(url=url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            try:
                parsed = json.loads(body) if body else {}
            except Exception:
                parsed = {"raw": body}
            label = f" [{_label}]" if _label else ""
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
        message: str,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "message": message,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if model:
            payload["model"] = model
        if system_prompt:
            payload["system_prompt"] = system_prompt
        return self._request(
            "POST", "/v1/client/chat", payload, require_auth=True,
            _label=f"chat/{model or 'default'}"
        )
