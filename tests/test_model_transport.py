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

    def test_fallback_retries_same_endpoint_with_other_model(self):
        transport = OpenAICompatibleTransport("http://localhost:1234/v1", timeout=30)
        transport._post_json = MagicMock(side_effect=[
            ClientError("primary failed"),
            {"choices": [{"message": {"content": "ok"}}], "model": "fallback/model"},
        ])
        result = transport.chat(
            model="primary/model",
            fallback_model="fallback/model",
            messages=[{"role": "user", "content": "hello"}],
        )
        self.assertEqual(result["response"], "ok")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(transport._post_json.call_args.args[0]["model"], "fallback/model")

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
