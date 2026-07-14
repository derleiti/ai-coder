from __future__ import annotations

import base64
import json
import time
import unittest

from aicoder.client import _decode_jwt_exp
from aicoder.executor import parse_tool_calls
from aicoder.setup import _is_token_expired


def _unsigned_token(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class AuthCompatibilityTests(unittest.TestCase):
    def test_expired_session_is_detected(self):
        token = _unsigned_token(int(time.time()) - 60)
        self.assertLess(_decode_jwt_exp(token), time.time())
        self.assertTrue(_is_token_expired(token))

    def test_valid_session_is_not_expired(self):
        self.assertFalse(_is_token_expired(_unsigned_token(int(time.time()) + 3600)))


class ToolCallCompatibilityTests(unittest.TestCase):
    def test_native_openai_shape(self):
        text = '<tool_call>{"type":"function","function":{"name":"code_read","arguments":"{\\"path\\":\\"README.md\\"}"}}</tool_call>'
        self.assertEqual(
            parse_tool_calls(text),
            [{"name": "code_read", "arguments": {"path": "README.md"}}],
        )

    def test_mistral_shape(self):
        text = '[TOOL_CALLS] [{"name":"code_search","arguments":{"query":"oauth"}}]'
        self.assertEqual(
            parse_tool_calls(text),
            [{"name": "code_search", "arguments": {"query": "oauth"}}],
        )

    def test_hermes_function_shape(self):
        text = '<function=health>{}</function>'
        self.assertEqual(parse_tool_calls(text), [{"name": "health", "arguments": {}}])

    def test_fenced_alias_shape(self):
        text = '```json\n{"tool":"code_tree","args":{"path":"."}}\n```'
        self.assertEqual(
            parse_tool_calls(text),
            [{"name": "code_tree", "arguments": {"path": "."}}],
        )


if __name__ == "__main__":
    unittest.main()
