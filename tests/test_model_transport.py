from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from aicoder.client import ClientError
from aicoder.executor import LOCAL_FILE_READ_SCHEMA
from aicoder.model_transport import (
    AnthropicMessagesTransport,
    OpenAICompatibleTransport,
    ProviderRoutingTransport,
    _openai_tools,
    native_model_transport_from_env,
)


class OpenAICompatibleTransportTests(unittest.TestCase):
    def test_converts_aicoder_tool_schema(self):
        converted = _openai_tools([LOCAL_FILE_READ_SCHEMA])
        self.assertEqual(converted[0]["type"], "function")
        self.assertEqual(converted[0]["function"]["name"], "file_read")
        self.assertEqual(
            converted[0]["function"]["parameters"],
            LOCAL_FILE_READ_SCHEMA["inputSchema"],
        )

    def test_tool_only_assistant_message_normalizes_null_content(self):
        transport = OpenAICompatibleTransport("https://example.invalid/v1", api_key="x")
        with patch.object(transport, "_post_json", return_value={
            "choices": [{"message": {"content": "ok"}}]
        }) as post:
            transport.chat(
                model="openrouter/test-model",
                messages=[{
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    }],
                }],
            )
        payload = post.call_args.args[0]
        self.assertEqual(payload["messages"][0]["content"], "")

    def test_invalid_message_content_fails_locally(self):
        transport = OpenAICompatibleTransport("https://example.invalid/v1", api_key="x")
        with self.assertRaisesRegex(ClientError, "expected string or content-block list"):
            transport.chat(
                model="openrouter/test-model",
                messages=[{"role": "user", "content": {"bad": "shape"}}],
            )

    def test_chat_preserves_openai_tool_call_id(self):
        transport = OpenAICompatibleTransport("http://127.0.0.1:11434/v1", timeout=30)
        transport._post_json = MagicMock(return_value={
            "id": "chatcmpl-test",
            "model": "qwen-test",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": '{"path":"README.md"}'},
                    }],
                }
            }],
        })
        result = transport.chat(
            model="qwen-test",
            messages=[{"role": "user", "content": "read it"}],
            tools=[LOCAL_FILE_READ_SCHEMA],
        )
        self.assertEqual(result["tool_calls"][0]["id"], "call_123")
        payload = transport._post_json.call_args.args[0]
        self.assertEqual(payload["tools"][0]["function"]["name"], "file_read")
        self.assertEqual(payload["model"], "qwen-test")

    def test_direct_transport_cancel_closes_active_response(self):
        transport = OpenAICompatibleTransport("https://example.invalid/v1", api_key="x")
        response = MagicMock()
        with transport._active_response_lock:
            transport._active_responses["req-one"] = response
        self.assertTrue(transport.cancel_current_request("req-one"))
        response.close.assert_called_once()
        self.assertFalse(transport.cancel_current_request())


    def test_direct_transport_cancels_only_named_parallel_request(self):
        transport = OpenAICompatibleTransport("https://example.invalid/v1", api_key="x")
        first, second = MagicMock(), MagicMock()
        with transport._active_response_lock:
            transport._active_responses["req-a"] = first
            transport._active_responses["req-b"] = second
        self.assertTrue(transport.cancel_current_request("req-a"))
        first.close.assert_called_once()
        second.close.assert_not_called()
        self.assertIn("req-b", transport._active_responses)

    def test_direct_transport_reports_blocking_timeout_semantics(self):
        transport = OpenAICompatibleTransport("https://example.invalid/v1", api_key="x", timeout=45)
        with patch.object(transport, "_post_json", return_value={
            "choices": [{"message": {"content": "ok"}}], "model": "direct/model"
        }):
            result = transport.chat(message="hello", model="direct/model")
        telemetry = result["_transport_telemetry"]
        self.assertEqual(telemetry["transport"], "openai-compatible-direct")
        self.assertFalse(telemetry["streaming"])
        self.assertEqual(telemetry["timeout_semantics"], "blocking-request")
        self.assertEqual(telemetry["keepalive_chunks"], 0)


    def test_ollama_canonical_model_id_is_stripped_only_for_transport(self):
        transport = OpenAICompatibleTransport("http://127.0.0.1:11434/v1")
        with patch.object(transport, "_post_json", return_value={
            "choices": [{"message": {"content": "OK"}}],
            "model": "nemotron-3-ultra:cloud",
        }) as post:
            result = transport.chat(
                message="ping", model="ollama/nemotron-3-ultra:cloud", max_tokens=32
            )
        payload = post.call_args.args[0]
        self.assertEqual(payload["model"], "nemotron-3-ultra:cloud")
        self.assertEqual(result["model"], "nemotron-3-ultra:cloud")

    def test_legacy_fallback_argument_never_switches_models(self):
        transport = OpenAICompatibleTransport("http://localhost:1234/v1", timeout=30)
        transport._post_json = MagicMock(side_effect=ClientError("primary failed"))
        with self.assertRaises(ClientError):
            transport.chat(
                model="primary/model",
                fallback_model="fallback/model",
                messages=[{"role": "user", "content": "hello"}],
            )
        self.assertEqual(transport._post_json.call_count, 1)
        self.assertEqual(transport._post_json.call_args.args[0]["model"], "primary/model")

    def test_direct_transport_strips_canonical_provider_prefix_for_matching_endpoint(self):
        cases = [
            ("http://127.0.0.1:11434/v1", "ollama/qwen3.5:cloud", "qwen3.5:cloud"),
            ("https://openrouter.ai/api/v1", "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3-ultra-550b-a55b:free"),
            ("https://integrate.api.nvidia.com/v1", "nvidia/nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3-ultra-550b-a55b"),
        ]
        for base_url, requested, expected in cases:
            with self.subTest(base_url=base_url, requested=requested):
                transport = OpenAICompatibleTransport(base_url)
                with patch.object(transport, "_post_json", return_value={
                    "choices": [{"message": {"content": "ok"}}]
                }) as post:
                    result = transport.chat(message="x", model=requested)
                self.assertEqual(post.call_args.args[0]["model"], expected)
                self.assertEqual(result["response"], "ok")



    def test_anthropic_transport_uses_native_messages_shape(self):
        transport = AnthropicMessagesTransport("https://api.anthropic.com/v1", api_key="secret", timeout=30)
        with patch.object(transport, "_post_json", return_value={
            "id": "msg_1", "model": "claude-sonnet-test",
            "content": [{"type": "text", "text": "OK"}],
        }) as post:
            result = transport.chat(
                message="hello", model="anthropic/claude-sonnet-test",
                system_prompt="system", max_tokens=123,
            )
        payload = post.call_args.args[0]
        self.assertEqual(payload["model"], "claude-sonnet-test")
        self.assertEqual(payload["system"], "system")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(payload["max_tokens"], 123)
        self.assertEqual(result["response"], "OK")
        self.assertEqual(result["backend"], "anthropic-direct")

    @patch("aicoder.model_transport.provider_api_key", return_value=("stored-key", "keyring"))
    def test_provider_router_routes_anthropic_directly(self, key_lookup):
        default = MagicMock(); default.timeout = 55
        router = ProviderRoutingTransport(default)
        with patch.object(AnthropicMessagesTransport, "chat", return_value={"response": "OK"}) as direct_chat:
            result = router.chat(message="x", model="anthropic/claude-sonnet-test")
        self.assertEqual(result["response"], "OK")
        default.chat.assert_not_called()
        self.assertIsInstance(router._direct["anthropic"], AnthropicMessagesTransport)
        direct_chat.assert_called_once()

    @patch("aicoder.model_transport.provider_api_key", return_value=("stored-key", "keyring"))
    def test_provider_router_routes_matching_model_directly(self, key_lookup):
        default = MagicMock()
        default.timeout = 55
        router = ProviderRoutingTransport(default)
        with patch.object(OpenAICompatibleTransport, "chat", return_value={"response": "OK"}) as direct_chat:
            result = router.chat(message="x", model="gemini/gemini-2.5-flash")
        self.assertEqual(result["response"], "OK")
        default.chat.assert_not_called()
        self.assertEqual(router._direct["google"].base_url, "https://generativelanguage.googleapis.com/v1beta/openai")
        direct_chat.assert_called_once()

    @patch("aicoder.model_transport.provider_api_key", return_value=("", "none"))
    def test_provider_router_falls_back_to_triforce_without_own_key(self, key_lookup):
        default = MagicMock()
        default.timeout = 30
        default.chat.return_value = {"response": "backend"}
        router = ProviderRoutingTransport(default)
        result = router.chat(message="x", model="gemini/gemini-2.5-flash")
        self.assertEqual(result["response"], "backend")
        default.chat.assert_called_once()

    def test_native_transport_wraps_default_once_when_no_explicit_env_override(self):
        default = MagicMock()
        default.timeout = 30
        with patch.dict(os.environ, {"AICODER_NATIVE_MODEL_BASE_URL": ""}, clear=False):
            first, _ = native_model_transport_from_env(default, default_model="gemini/test")
            second, _ = native_model_transport_from_env(first, default_model="gemini/test")
        self.assertIsInstance(first, ProviderRoutingTransport)
        self.assertIs(first, second)

    def test_env_reasoning_effort_is_forwarded_without_persisting_state(self):
        default = MagicMock()
        default.timeout = 45
        with patch.dict(os.environ, {
            "AICODER_NATIVE_MODEL_BASE_URL": "http://localhost:11434/v1",
            "AICODER_NATIVE_REASONING_EFFORT": "low",
        }, clear=False):
            transport, _ = native_model_transport_from_env(default, default_model="ollama/test:cloud")
        self.assertEqual(transport.reasoning_effort, "low")
        with patch.object(transport, "_post_json", return_value={
            "choices": [{"message": {"content": "ok"}}]
        }) as post:
            transport.chat(message="x", model="ollama/test:cloud")
        self.assertEqual(post.call_args.args[0]["reasoning_effort"], "low")
        self.assertEqual(post.call_args.args[0]["model"], "test:cloud")

    def test_env_opt_in_does_not_persist_api_key(self):
        default = MagicMock()
        default.timeout = 45
        env = {
            "AICODER_NATIVE_MODEL_BASE_URL": "http://localhost:11434/v1",
            "AICODER_NATIVE_MODEL": "qwen2.5-coder",
            "AICODER_NATIVE_MODEL_API_KEY": "secret",
        }
        with patch.dict(os.environ, env, clear=False):
            transport, model = native_model_transport_from_env(default, default_model="old")
        self.assertIsInstance(transport, OpenAICompatibleTransport)
        self.assertEqual(model, "qwen2.5-coder")
        self.assertEqual(transport.api_key, "secret")
        self.assertEqual(transport.timeout, 45)


if __name__ == "__main__":
    unittest.main()
