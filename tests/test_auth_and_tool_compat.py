from __future__ import annotations

import base64
import contextlib
import io
import json
import time
import unittest
from unittest.mock import patch

from aicoder.client import _decode_jwt_exp
from aicoder.config import Session
from aicoder.executor import parse_tool_calls
from aicoder.setup import _is_token_expired, run_setup


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

    def test_setup_reauthenticates_an_expired_session(self):
        previous = Session(
            base_url="https://api.ailinux.me",
            token=_unsigned_token(int(time.time()) - 60),
            client_id="old-client",
            user_id="user@example.com",
            tier="registered",
            account_role="unknown",
        )
        login_result = {
            "token": _unsigned_token(int(time.time()) + 3600),
            "client_id": "new-client",
            "user_id": previous.user_id,
            "tier": "registered",
        }

        class FakeClient:
            def __init__(self, base_url):
                self.base_url = base_url

            def login(self, email, password):
                self.email = email
                self.password = password
                return login_result

        saved = []

        def answer(prompt, default=""):
            return default

        with (
            patch("aicoder.setup.load_session", return_value=previous),
            patch("aicoder.setup.get_state", return_value={"selected_model": "ollama/test"}),
            patch("aicoder.setup._ask", side_effect=answer),
            patch("aicoder.setup.getpass", return_value="secret"),
            patch("aicoder.setup.save_session", side_effect=saved.append),
            patch("aicoder.client.TriForceClient", FakeClient),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertTrue(run_setup())

        self.assertEqual(saved[0].client_id, "new-client")
        self.assertEqual(saved[0].user_id, previous.user_id)


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
