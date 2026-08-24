from __future__ import annotations
import base64
import json
import ssl
import sys
import threading
import time
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from . import __version__
from .tool_policy import (
    LOCAL_ONLY_TOOLS, OPERATOR_MCP_TOOLS, canonical_tool_name, require_allowed_tool,
    triforce_host_forbidden_reason,
)
USER_AGENT = f"ai-coder/{__version__} (AILinux Operator Client)"
CLIENT_PROFILE = "ai-coder"

# ── Connection pool (keep-alive) ──────────────────────────────
_POOL = None

def _get_pool():
    """Lazy-init urllib3 PoolManager for connection reuse (keep-alive)."""
    global _POOL
    if _POOL is not None:
        return _POOL
    try:
        import urllib3
        _POOL = urllib3.PoolManager(
            num_pools=4, maxsize=4, retries=False,
            timeout=urllib3.Timeout(connect=10, read=60),
        )
        return _POOL
    except ImportError:
        return None  # Fallback to urlopen if urllib3 not installed

_SSL_CTX = None

def _ssl_context() -> ssl.SSLContext:
    """SSL context with proper CA certs. Cached at module level."""
    global _SSL_CTX
    if _SSL_CTX is not None: return _SSL_CTX
    try:
        import certifi
        _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


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
    def __init__(
        self, message: str, *, status_code: int | None = None,
        retryable: bool | None = None, retry_after: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after
        self.payload = payload


def _error_metadata(payload: Any, status_code: int | None = None) -> tuple[int | None, bool | None, int | None]:
    data = payload if isinstance(payload, dict) else {}
    error = data.get("error") if isinstance(data.get("error"), dict) else data
    status = error.get("status", error.get("status_code", status_code))
    try:
        status = int(status) if status is not None else status_code
    except (TypeError, ValueError):
        status = status_code
    retryable = error.get("retryable")
    if retryable is None and status is not None:
        retryable = status in {408, 429, 500, 502, 503, 504, 524}
    retry_after = error.get("retry_after")
    try:
        retry_after = int(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    return status, bool(retryable) if retryable is not None else None, retry_after


class TokenExpiredError(ClientError):
    """Raised when JWT token is expired and no auto-refresh is possible."""
    pass


def _content_text(value: Any) -> str:
    """Extract visible text without destroying structured provider blocks."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"text", "output_text"} and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _normalize_chat_response(data: Any) -> Dict[str, Any]:
    """Normalize common provider envelopes while preserving raw tool-call identity."""
    if not isinstance(data, dict):
        raise ClientError(f"Chat backend returned {type(data).__name__}, expected object")
    if data.get("error") is not None and not data.get("response"):
        status, retryable, retry_after = _error_metadata(data)
        detail = data.get("error")
        raise ClientError(
            f"Chat backend error: {json.dumps(detail, ensure_ascii=False, default=str)}",
            status_code=status, retryable=retryable, retry_after=retry_after, payload=data,
        )
    if "response" in data:
        return data

    # OpenAI Chat Completions and compatible APIs (Mistral, Groq, etc.).
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {})
        if isinstance(message, dict):
            normalized = dict(data)
            normalized["response"] = _content_text(message.get("content"))
            if isinstance(message.get("tool_calls"), list):
                normalized["tool_calls"] = message["tool_calls"]
            return normalized

    # OpenAI Responses API: text and function calls are sibling output items.
    output = data.get("output")
    if isinstance(output, list):
        texts: list[str] = []
        calls: list[dict] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                text = _content_text(item.get("content"))
                if text:
                    texts.append(text)
            elif item.get("type") in {"function_call", "tool_call"}:
                calls.append(dict(item))
        normalized = dict(data)
        normalized["response"] = "\n".join(texts)
        if calls:
            normalized["tool_calls"] = calls
        return normalized

    # Anthropic Messages API. Keep tool_use blocks intact so their IDs survive.
    content = data.get("content")
    if isinstance(content, list):
        texts: list[str] = []
        calls: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") in {"tool_use", "tool_call"}:
                calls.append(dict(block))
        normalized = dict(data)
        normalized["response"] = "\n".join(texts)
        if calls:
            normalized["tool_calls"] = calls
        return normalized

    # Gemini generateContent. Preserve the complete functionCall payload and
    # any thought signature attached to its containing part.
    candidates = data.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        texts: list[str] = []
        calls: list[dict] = []
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
            function_call = part.get("functionCall")
            if isinstance(function_call, dict):
                raw_call = {"functionCall": dict(function_call)}
                for key in ("thoughtSignature", "thought_signature"):
                    if key in part:
                        raw_call[key] = part[key]
                calls.append(raw_call)
        normalized = dict(data)
        normalized["response"] = "\n".join(texts)
        if calls:
            normalized["tool_calls"] = calls
        return normalized

    # Ollama and other APIs that return a top-level message object.
    message = data.get("message")
    if isinstance(message, dict):
        normalized = dict(data)
        normalized["response"] = _content_text(message.get("content"))
        if isinstance(message.get("tool_calls"), list):
            normalized["tool_calls"] = message["tool_calls"]
        return normalized

    raise ClientError("Chat backend response contains no recognized response envelope")


def model_identifier(value: Any) -> str:
    """Return a stable model id from string or catalogue-object variants."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("id", "model", "name", "model_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return ""


class TriForceClient:
    def __init__(self, base_url: str, token: Optional[str] = None, timeout: int = 300):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._active_response_lock = threading.Lock()
        self._active_responses: dict[str, Any] = {}

    def _set_active_response(self, response: Any, request_id: str | None = None) -> str:
        key = str(request_id or f"thread-{threading.get_ident()}")
        with self._active_response_lock:
            self._active_responses[key] = response
        return key

    def _clear_active_response(self, response: Any, request_id: str | None = None) -> None:
        with self._active_response_lock:
            if request_id:
                key = str(request_id)
                if self._active_responses.get(key) is response:
                    self._active_responses.pop(key, None)
                return
            for key, active in list(self._active_responses.items()):
                if active is response:
                    self._active_responses.pop(key, None)
                    return

    def cancel_current_request(self, request_id: str | None = None) -> bool:
        """Cancel a specific parallel request when possible; no-id keeps legacy semantics."""
        with self._active_response_lock:
            if request_id:
                responses = [self._active_responses.pop(str(request_id), None)]
            elif len(self._active_responses) == 1:
                key, response = next(iter(self._active_responses.items()))
                self._active_responses.pop(key, None); responses = [response]
            else:
                responses = list(self._active_responses.values())
                self._active_responses.clear()
        closed = False
        for response in responses:
            if response is None:
                continue
            for method_name in ("close", "release_conn"):
                method = getattr(response, method_name, None)
                if callable(method):
                    try:
                        method(); closed = True
                    except Exception:
                        pass
        return closed

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
            return "expired"
        if remaining < 300:
            m = int(remaining / 60)
            return f"expires in {m}min"
        hours = int(remaining / 3600)
        if hours > 0:
            return f"valid ({hours}h)"
        return f"valid ({int(remaining/60)}min)"

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        require_auth: bool = False,
        _label: str = "",
        _retries: int = 1,
        _extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        data = None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-Client-Profile": CLIENT_PROFILE,
        }
        if _extra_headers:
            headers.update({str(k): str(v) for k, v in _extra_headers.items()})
        if require_auth:
            if not self.token:
                raise ClientError("Kein Token vorhanden. Erst einloggen.")
            if self.is_token_expired():
                raise TokenExpiredError(
                    "Token expired. Please re-login: aicoder setup"
                )
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        last_err = None
        for attempt in range(_retries + 1):
            if attempt > 0:
                time.sleep(min(2 ** attempt, 4))
                print(f"  ↻ retry {attempt}/{_retries} [{_label}]", file=sys.stderr)
            try:
                return self._do_request(method, url, headers, data, _label)
            except ClientError as e:
                last_err = e
                err_str = str(e)
                # Do not retry permanent 4xx/auth errors. 408 and 429 are transient.
                if (
                    ("HTTP 4" in err_str and "HTTP 408" not in err_str and "HTTP 429" not in err_str)
                    or "Token expired" in err_str
                ):
                    raise
                # Retry on 5xx, 408/429, timeout, and connection errors.
                if attempt < _retries:
                    continue
                raise
        raise last_err  # unreachable but satisfies type checker

    def _do_request(
        self, method: str, url: str, headers: dict, data: Optional[bytes], _label: str
    ) -> Dict[str, Any]:
        """Execute single HTTP request. Uses urllib3 pool if available, else urlopen."""
        request_id = str(headers.get("X-AICoder-Request-ID") or "") or None
        pool = _get_pool()
        if pool is not None:
            try:
                keepalive_stream = str(headers.get("X-AICoder-Keepalive", "")).lower() in {"1", "true", "json"}
                resp = pool.request(
                    method.upper(), url, headers=headers, body=data,
                    timeout=self.timeout, redirect=False,
                    preload_content=not keepalive_stream,
                )
                self._set_active_response(resp, request_id)
                if resp.status >= 400:
                    raw_error = resp.read() if keepalive_stream else resp.data
                    body = raw_error.decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(body) if body else {}
                    except Exception:
                        parsed = {"raw": body}
                    label = f" [{_label}]" if _label else ""
                    if resp.status in (401, 403):
                        detail = parsed.get("detail", "") or parsed.get("raw", "")
                        if "expire" in str(detail).lower() or "token" in str(detail).lower():
                            raise TokenExpiredError(
                                f"Token expired (HTTP {resp.status}). Please re-login: aicoder setup"
                            )
                    status, retryable, retry_after = _error_metadata(parsed, resp.status)
                    self._clear_active_response(resp, request_id)
                    raise ClientError(
                        f"HTTP {resp.status}{label} bei {url}: {parsed}",
                        status_code=status, retryable=retryable, retry_after=retry_after, payload=parsed,
                    )
                if keepalive_stream:
                    started = time.monotonic()
                    last_rx = started
                    max_gap = 0.0
                    chunks = 0
                    byte_count = 0
                    keepalive_chunks = 0
                    payload_chunks = 0
                    keepalive_times_s: list[float] = []
                    parts: list[bytes] = []
                    try:
                        while True:
                            # self.timeout is an inactivity timeout, not a maximum
                            # model-thinking duration. The backend emits keepalive bytes
                            # while provider inference is still alive so proxies do not
                            # mistake long reasoning for an idle origin connection.
                            chunk = resp.read(2048)
                            if not chunk:
                                break
                            now = time.monotonic()
                            max_gap = max(max_gap, now - last_rx)
                            last_rx = now
                            chunks += 1
                            byte_count += len(chunk)
                            if not chunk.strip():
                                keepalive_chunks += 1
                                keepalive_times_s.append(round(now - started, 3))
                            else:
                                payload_chunks += 1
                            parts.append(chunk)
                    finally:
                        self._clear_active_response(resp, request_id)
                        try:
                            resp.release_conn()
                        except Exception:
                            pass
                    raw = b"".join(parts).decode("utf-8")
                    result = json.loads(raw) if raw else {}
                    if isinstance(result, dict):
                        result = dict(result)
                        result["_transport_telemetry"] = {
                            "elapsed_s": round(time.monotonic() - started, 3),
                            "chunks": chunks,
                            "bytes": byte_count,
                            "keepalive_chunks": keepalive_chunks,
                            "payload_chunks": payload_chunks,
                            "keepalive_times_s": keepalive_times_s[-32:],
                            "max_rx_gap_s": round(max_gap, 3),
                            "last_rx_age_s": round(max(0.0, time.monotonic() - last_rx), 3),
                        }
                    return result
                try:
                    raw = resp.data.decode("utf-8")
                    return json.loads(raw) if raw else {}
                finally:
                    self._clear_active_response(resp, request_id)
            except (TokenExpiredError, ClientError):
                raise
            except Exception as e:
                # urllib3 already performed the request. Falling through to
                # urlopen here sends it a second time and can double the wait
                # after a read timeout.
                label = f" [{_label}]" if _label else ""
                raise ClientError(
                    f"Verbindung/Timeout nach {self.timeout}s{label} bei {url}: {e}"
                ) from e

        # Fallback: plain urlopen (no pool)
        req = Request(url=url, data=data, headers=headers, method=method.upper())
        try:
            resp = urlopen(req, timeout=self.timeout, context=_ssl_context())
            self._set_active_response(resp, request_id)
            try:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
            finally:
                self._clear_active_response(resp, request_id)
                try:
                    resp.close()
                except Exception:
                    pass
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            try:
                parsed = json.loads(body) if body else {}
            except Exception:
                parsed = {"raw": body}
            label = f" [{_label}]" if _label else ""
            if e.code in (401, 403):
                detail = parsed.get("detail", "") or parsed.get("raw", "")
                if "expire" in str(detail).lower() or "token" in str(detail).lower():
                    raise TokenExpiredError(
                        f"Token expired (HTTP {e.code}). Please re-login: aicoder setup"
                    ) from e
            status, retryable, retry_after = _error_metadata(parsed, e.code)
            raise ClientError(
                f"HTTP {e.code}{label} bei {url}: {parsed}",
                status_code=status, retryable=retryable, retry_after=retry_after, payload=parsed,
            ) from e
        except TimeoutError:
            label = f" [{_label}]" if _label else ""
            raise ClientError(
                f"Timeout nach {self.timeout}s{label} bei {url}. "
                "Backend reachable? Increase timeout via --timeout."
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
        # Older TriForce deployments expose only tier; current deployments also
        # return the concrete client account role. Keep one stable client shape.
        result.setdefault("account_role", result.get("role") or result.get("tier", "unknown"))
        self.token = token
        return result

    def verify(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/auth/verify", require_auth=True, _label="verify")

    def handshake(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/auth/client/handshake", require_auth=True, _label="handshake")

    def mcp_call(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        allow_internal: bool = False,
    ) -> Dict[str, Any]:
        if canonical_tool_name(tool_name) in LOCAL_ONLY_TOOLS:
            raise ClientError(
                f"tool '{tool_name}' is local-only in ai-coder and cannot be dispatched over MCP"
            )
        host_reason = triforce_host_forbidden_reason(tool_name)
        if host_reason:
            raise ClientError(host_reason)
        allowed, reason = require_allowed_tool(
            tool_name, OPERATOR_MCP_TOOLS, allow_internal=allow_internal,
        )
        if not allowed:
            raise ClientError(reason)
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
            "id": 1,
        }
        response = self._request(
            "POST", "/v1/mcp", payload, require_auth=True,
            _label=tool_name, _retries=0,
        )
        if isinstance(response, dict) and response.get("error") is not None:
            raise ClientError(
                f"MCP {tool_name} failed: "
                f"{json.dumps(response['error'], ensure_ascii=False, default=str)}"
            )
        if not isinstance(response, dict):
            raise ClientError(f"MCP {tool_name} returned a non-object response")
        # Accept the legacy gateway shape while presenting one JSON-RPC shape
        # to the rest of the client.
        if "result" not in response and "content" in response:
            response = {"jsonrpc": "2.0", "id": response.get("id", 1), "result": response}
        if "result" not in response:
            raise ClientError(f"MCP {tool_name} response contains neither result nor error")
        return response

    def list_models(self) -> list:
        """Fetch available models from /v1/client/models."""
        try:
            data = self._request("GET", "/v1/client/models", require_auth=True, _label="models")
            details = data.get("model_details") or []
            if isinstance(details, list) and details:
                return [m for m in details if isinstance(m, dict)]
            models = data.get("models", [])
            result = []
            for m in models:
                if isinstance(m, str):
                    prov = m.split("/")[0] if "/" in m else "other"
                    result.append({"id": m, "model": m, "name": m, "provider": prov, "capabilities": ["chat"]})
                elif isinstance(m, dict):
                    result.append(m)
            return result
        except TokenExpiredError:
            print("⚠ Token expired — run: aicoder setup", file=sys.stderr)
            return []
        except ClientError as e:
            print(f"⚠ Models laden fehlgeschlagen: {e}", file=sys.stderr)
            return []
        except Exception as e:
            print(f"⚠ Models: unerwarteter Fehler: {e}", file=sys.stderr)
            return []

    def chat(
        self,
        message: str = "",
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        fallback_model: Optional[str] = None,
        messages: Optional[list] = None,
        tools: Optional[list] = None,
        tool_choice: Any = "auto",
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Call /v1/client/chat. Supports messages array for multi-turn context.

        ``fallback_model`` is accepted only for source compatibility and is intentionally ignored.
        AICoder never switches models implicitly.
        """
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
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        try:
            primary_result = _normalize_chat_response(self._request(
                "POST", "/v1/client/chat", payload, require_auth=True,
                _label=f"chat/{model or 'default'}", _retries=0,
                _extra_headers={"X-AICoder-Keepalive": "json", **({"X-AICoder-Request-ID": request_id} if request_id else {})},
            ))
            if isinstance(primary_result, dict) and request_id:
                primary_result = dict(primary_result)
                telemetry = dict(primary_result.get("_transport_telemetry") or {})
                telemetry["request_id"] = request_id
                primary_result["_transport_telemetry"] = telemetry
            return primary_result
        except TokenExpiredError:
            raise
        except ClientError:
            raise
