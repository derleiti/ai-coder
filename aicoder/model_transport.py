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
from .provider_credentials import (
    direct_provider_spec, provider_api_key, provider_for_model, transport_model_id,
)


class ModelTransport(Protocol):
    timeout: int

    def chat(self, **kwargs: Any) -> dict[str, Any]: ...


def _normalize_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize and validate messages at the OpenAI-compatible provider boundary.

    Tool-only assistant turns commonly have no textual response. Internally we
    represent those turns with an empty string because several OpenAI-compatible
    providers reject ``content: null`` even when ``tool_calls`` is present.
    """
    normalized: list[dict[str, Any]] = []
    for raw in messages:
        message = dict(raw)
        role = str(message.get("role") or "").strip()
        content = message.get("content")
        if content is None and role == "assistant" and message.get("tool_calls"):
            message["content"] = ""
        elif not isinstance(content, (str, list)):
            raise ClientError(
                f"Invalid message content for role={role or '?'}: "
                f"expected string or content-block list, got {type(content).__name__}"
            )
        normalized.append(message)
    return normalized


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
        reasoning_effort: str = "",
    ):
        base = str(base_url or "").strip()
        if not base:
            raise ValueError("OpenAI-compatible base URL is required")
        self.base_url = base.rstrip("/")
        self.api_key = str(api_key or "")
        self.timeout = max(10, min(300, int(timeout)))
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        effort = str(reasoning_effort or "").strip().lower()
        if effort not in {"", "high", "medium", "low", "none"}:
            raise ValueError("reasoning_effort must be high, medium, low, none, or empty")
        self.reasoning_effort = effort
        self._active_response_lock = threading.Lock()
        self._active_responses: dict[str, Any] = {}

    def cancel_current_request(self, request_id: str | None = None) -> bool:
        """Best-effort cancellation of one request, safe under parallel team calls."""
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
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close(); closed = True
                except Exception:
                    pass
        return closed

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
            active_key = str(request_id or f"thread-{threading.get_ident()}")
            with self._active_response_lock:
                self._active_responses[active_key] = response
            try:
                raw = response.read().decode("utf-8", errors="replace")
            finally:
                with self._active_response_lock:
                    if self._active_responses.get(active_key) is response:
                        self._active_responses.pop(active_key, None)
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
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        # ``fallback_model`` is a deprecated compatibility argument. Never route a
        # failed request to another model implicitly.
        request_messages = [dict(item) for item in (messages or []) if isinstance(item, dict)]
        if not request_messages:
            if system_prompt:
                request_messages.append({"role": "system", "content": system_prompt})
            request_messages.append({"role": "user", "content": message})
        request_messages = _normalize_openai_messages(request_messages)
        requested_model = str(model or "").strip()
        transport_model = requested_model
        base_lower = self.base_url.lower()
        if requested_model.startswith("ollama/") and ("11434" in base_lower or "ollama" in base_lower):
            transport_model = requested_model[len("ollama/"):]
        else:
            provider = provider_for_model(requested_model)
            spec = direct_provider_spec(provider) if provider else None
            if spec and spec.base_url and spec.base_url.lower().rstrip("/") == self.base_url.lower().rstrip("/"):
                transport_model = transport_model_id(requested_model, provider)
        payload: dict[str, Any] = {
            "model": transport_model,
            "messages": request_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not payload["model"]:
            raise ClientError("Direct OpenAI-compatible transport requires a model id")
        effort = self.reasoning_effort if reasoning_effort is None else str(reasoning_effort or "").strip().lower()
        if effort:
            if effort not in {"high", "medium", "low", "none"}:
                raise ClientError("Invalid reasoning_effort for direct model transport")
            payload["reasoning_effort"] = effort
        converted_tools = _openai_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
            payload["tool_choice"] = tool_choice

        started = time.monotonic()
        normalized = _normalize_chat_response(self._post_json(payload, request_id=request_id))
        normalized = dict(normalized)
        normalized.setdefault("model", requested_model or payload["model"])
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


class AnthropicMessagesTransport:
    """Direct transport for Anthropic's native Messages API."""

    def __init__(self, base_url: str, *, api_key: str, timeout: int = 300):
        base = str(base_url or "").strip()
        if not base:
            raise ValueError("Anthropic base URL is required")
        self.base_url = base.rstrip("/")
        self.api_key = str(api_key or "")
        self.timeout = max(10, min(300, int(timeout)))
        self._active_response_lock = threading.Lock()
        self._active_responses: dict[str, Any] = {}

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/messages"):
            return self.base_url
        return urljoin(self.base_url + "/", "messages")

    def cancel_current_request(self, request_id: str | None = None) -> bool:
        with self._active_response_lock:
            if request_id:
                responses = [self._active_responses.pop(str(request_id), None)]
            elif len(self._active_responses) == 1:
                key, response = next(iter(self._active_responses.items()))
                self._active_responses.pop(key, None)
                responses = [response]
            else:
                responses = list(self._active_responses.values())
                self._active_responses.clear()
        closed = False
        for response in responses:
            if response is None:
                continue
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close(); closed = True
                except Exception:
                    pass
        return closed

    def _post_json(self, payload: dict[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
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
            active_key = str(request_id or f"thread-{threading.get_ident()}")
            with self._active_response_lock:
                self._active_responses[active_key] = response
            try:
                raw = response.read().decode("utf-8", errors="replace")
            finally:
                with self._active_response_lock:
                    if self._active_responses.get(active_key) is response:
                        self._active_responses.pop(active_key, None)
                try:
                    response.close()
                except Exception:
                    pass
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise ClientError(f"Anthropic HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ClientError(f"Anthropic request failed: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ClientError("Anthropic endpoint returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ClientError("Anthropic endpoint returned a non-object response")
        return data

    @staticmethod
    def _anthropic_tools(tools: list[dict] | None) -> list[dict]:
        converted: list[dict] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                fn = tool["function"]
                name = str(fn.get("name") or "").strip()
                description = str(fn.get("description") or "")
                schema = fn.get("parameters")
            else:
                name = str(tool.get("name") or "").strip()
                description = str(tool.get("description") or "")
                schema = tool.get("inputSchema") or tool.get("parameters")
            if not name:
                continue
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            converted.append({"name": name, "description": description, "input_schema": schema})
        return converted

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
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        requested_model = str(model or "").strip()
        provider = provider_for_model(requested_model)
        transport_model = transport_model_id(requested_model, provider or "anthropic")
        if not transport_model:
            raise ClientError("Direct Anthropic transport requires a model id")

        request_messages: list[dict[str, Any]] = []
        system_parts: list[str] = []
        if system_prompt:
            system_parts.append(str(system_prompt))
        for item in messages or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            content = item.get("content", "")
            if role == "system":
                if content:
                    system_parts.append(str(content))
                continue
            if role in {"user", "assistant"}:
                request_messages.append({"role": role, "content": content})
        if not request_messages:
            request_messages.append({"role": "user", "content": message})

        payload: dict[str, Any] = {
            "model": transport_model,
            "messages": request_messages,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        converted_tools = self._anthropic_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
            if tool_choice == "required":
                payload["tool_choice"] = {"type": "any"}
            elif tool_choice not in {None, "none"}:
                payload["tool_choice"] = {"type": "auto"}

        started = time.monotonic()
        normalized = dict(_normalize_chat_response(self._post_json(payload, request_id=request_id)))
        normalized.setdefault("model", requested_model or transport_model)
        normalized.setdefault("backend", "anthropic-direct")
        elapsed_s = time.monotonic() - started
        normalized.setdefault("latency_ms", int(elapsed_s * 1000))
        normalized.setdefault("_transport_telemetry", {
            "transport": "anthropic-direct",
            "streaming": False,
            "timeout_semantics": "blocking-request",
            "elapsed_s": round(elapsed_s, 3),
            "keepalive_chunks": 0,
            "payload_chunks": 1,
            "request_id": request_id or "",
        })
        return normalized


class ProviderRoutingTransport:
    """Route individual model calls through a user's secure provider credential.

    This wrapper is intentionally per-request: a Team Runtime can mix Gemini,
    OpenRouter, NVIDIA and other models without pinning the entire run to the
    provider of the primary model. Calls without a supported/configured direct
    provider fall through unchanged to the existing TriForce client.
    """

    def __init__(self, default: ModelTransport):
        self.default = default
        self.timeout = int(getattr(default, "timeout", 300))
        self._direct: dict[str, ModelTransport] = {}

    def _transport_for_model(self, model: str | None) -> ModelTransport:
        provider = provider_for_model(model)
        spec = direct_provider_spec(provider) if provider else None
        if not spec or not spec.direct_supported or not spec.base_url:
            return self.default
        api_key, source = provider_api_key(provider)
        # Existing provider environment variables historically served diagnostics
        # only. Do not silently change routing for users who already have them.
        # Direct routing is enabled by an explicit AICoder OS-keyring credential;
        # AICODER_NATIVE_MODEL_* remains the explicit environment-based opt-in.
        if not api_key or source != "keyring":
            return self.default
        cached = self._direct.get(provider)
        cached_key = str(getattr(cached, "api_key", "")) if cached is not None else ""
        if cached is None or cached_key != api_key:
            if provider == "anthropic":
                cached = AnthropicMessagesTransport(spec.base_url, api_key=api_key, timeout=self.timeout)
            else:
                cached = OpenAICompatibleTransport(spec.base_url, api_key=api_key, timeout=self.timeout)
            self._direct[provider] = cached
        return cached

    def chat(self, **kwargs: Any) -> dict[str, Any]:
        transport = self._transport_for_model(kwargs.get("model"))
        return transport.chat(**kwargs)

    def cancel_current_request(self, request_id: str | None = None) -> bool:
        cancelled = False
        targets = [self.default, *self._direct.values()]
        for target in targets:
            cancel = getattr(target, "cancel_current_request", None)
            if callable(cancel):
                try:
                    try:
                        result = cancel(request_id) if request_id is not None else cancel()
                    except TypeError:
                        # Preserve compatibility with older transports whose
                        # cancellation hook accepts no request identifier.
                        result = cancel()
                    cancelled = bool(result) or cancelled
                except Exception:
                    pass
        return cancelled

    def __getattr__(self, name: str) -> Any:
        return getattr(self.default, name)


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
        # Secure per-provider keys are an opt-in routing layer; unsupported or
        # unconfigured models continue through the existing backend unchanged.
        if isinstance(default, ProviderRoutingTransport):
            return default, default_model
        return ProviderRoutingTransport(default), default_model
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
        reasoning_effort=os.environ.get("AICODER_NATIVE_REASONING_EFFORT", ""),
    )
    return transport, model
