from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from aicoder.client import ClientError
from aicoder.executor import LOCAL_FILE_READ_SCHEMA
from aicoder.model_transport import (
    OpenAICompatibleTransport,
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
