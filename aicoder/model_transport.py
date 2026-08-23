"""Model transport adapters for the opt-in native-light runtime.

TriForce remains the default model/tool backend.  For internal compatibility
work, native-light can route only model calls to an OpenAI-compatible endpoint
while keeping AILinux/TriForce MCP tools and safety policy unchanged.
"""
from __future__ import annotations

import json
import os
import ssl
import threading
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .client import ClientError, USER_AGENT, _normalize_chat_response


class ModelTransport(Protocol):
    timeout: int

    def chat(self, **kwargs: Any) -> dict[str, Any]: ...


def _openai_tools(tools: list[dict] | None) -> list[dict]:
    """Convert AICoder/MCP tool schemas to OpenAI-compatible function tools."""
    converted: list[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            converted.append(dict(tool))
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        schema = tool.get("inputSchema") or tool.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        converted.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(tool.get("description") or ""),
                "parameters": schema,
            },
        })
    return converted


class OpenAICompatibleTransport:
    """Minimal direct transport for OpenAI-compatible Chat Completions APIs.

    This is intentionally dependency-light and internal-preview quality.  The
    agent runtime, tools, approvals, plans and MCP stay AICoder-owned; only the
    model request is sent to the configured endpoint.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        timeout: int = 300,
        headers: dict[str, str] | None = None,
    ):
        base = str(base_url or "").strip()
        if not base:
            raise ValueError("OpenAI-compatible base URL is required")
        self.base_url = base.rstrip("/")
        self.api_key = str(api_key or "")
        self.timeout = max(10, min(300, int(timeout)))
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self._active_response_lock = threading.Lock()
        self._active_response = None

    def cancel_current_request(self) -> bool:
        """Best-effort cancellation once the HTTP response handle exists."""
        with self._active_response_lock:
            response = self._active_response
            self._active_response = None
        if response is None:
            return False
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
                return True
            except Exception:
                return False
        return False

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return urljoin(self.base_url + "/", "chat/completions")

    def _post_json(self, payload: dict[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **self.headers,
        }
        if self.api_key and not any(key.lower() == "authorization" for key in headers):
            headers["Authorization"] = f"Bearer {self.api_key}"
        if request_id:
            headers["X-AICoder-Request-ID"] = request_id
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        context = ssl.create_default_context()
        try:
            response = urlopen(request, timeout=self.timeout, context=context)
            with self._active_response_lock:
                self._active_response = response
            try:
                raw = response.read().decode("utf-8", errors="replace")
            finally:
                with self._active_response_lock:
                    if self._active_response is response:
                        self._active_response = None
                try:
                    response.close()
                except Exception:
                    pass
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise ClientError(f"OpenAI-compatible HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ClientError(f"OpenAI-compatible request failed: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ClientError("OpenAI-compatible endpoint returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ClientError("OpenAI-compatible endpoint returned a non-object response")
        return data

    def chat(
        self,
        message: str = "",
        model: str | None = None,
        system_prompt: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        fallback_model: str | None = None,
        messages: list | None = None,
        tools: list | None = None,
        tool_choice: Any = "auto",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_messages = [dict(item) for item in (messages or []) if isinstance(item, dict)]
        if not request_messages:
            if system_prompt:
                request_messages.append({"role": "system", "content": system_prompt})
            request_messages.append({"role": "user", "content": message})
        payload: dict[str, Any] = {
            "model": str(model or ""),
            "messages": request_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not payload["model"]:
            raise ClientError("Direct OpenAI-compatible transport requires a model id")
        converted_tools = _openai_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
            payload["tool_choice"] = tool_choice

        started = time.monotonic()
        try:
            normalized = _normalize_chat_response(self._post_json(payload, request_id=request_id))
        except ClientError:
            if fallback_model and fallback_model != model:
                payload["model"] = fallback_model
                normalized = _normalize_chat_response(self._post_json(payload, request_id=request_id))
                normalized = dict(normalized)
                normalized["fallback_used"] = True
                normalized.setdefault("primary_model", model)
            else:
                raise
        normalized = dict(normalized)
        normalized.setdefault("model", payload["model"])
        normalized.setdefault("backend", "openai-compatible-direct")
        elapsed_s = time.monotonic() - started
        normalized.setdefault("latency_ms", int(elapsed_s * 1000))
        normalized.setdefault("_transport_telemetry", {
            "transport": "openai-compatible-direct",
            "streaming": False,
            "timeout_semantics": "blocking-request",
            "elapsed_s": round(elapsed_s, 3),
            "keepalive_chunks": 0,
            "payload_chunks": 1,
            "request_id": request_id or "",
        })
        return normalized


def native_model_transport_from_env(
    default: ModelTransport,
    *,
    default_model: str | None = None,
) -> tuple[ModelTransport, str | None]:
    """Resolve the internal native-light model transport without persisting secrets.

    AICODER_NATIVE_MODEL_BASE_URL opts into the direct transport. API keys and
    optional headers stay in the process environment instead of state.json.
    """
    base_url = os.environ.get("AICODER_NATIVE_MODEL_BASE_URL", "").strip()
    if not base_url:
        return default, default_model
    raw_headers = os.environ.get("AICODER_NATIVE_MODEL_HEADERS", "").strip()
    headers: dict[str, str] = {}
    if raw_headers:
        try:
            decoded = json.loads(raw_headers)
        except json.JSONDecodeError as exc:
            raise ClientError("AICODER_NATIVE_MODEL_HEADERS must be a JSON object") from exc
        if not isinstance(decoded, dict):
            raise ClientError("AICODER_NATIVE_MODEL_HEADERS must be a JSON object")
        headers = {str(key): str(value) for key, value in decoded.items()}
    model = os.environ.get("AICODER_NATIVE_MODEL", "").strip() or default_model
    transport = OpenAICompatibleTransport(
        base_url,
        api_key=os.environ.get("AICODER_NATIVE_MODEL_API_KEY", ""),
        timeout=getattr(default, "timeout", 300),
        headers=headers,
    )
    return transport, model
