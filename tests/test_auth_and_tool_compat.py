from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from aicoder.client import ClientError, TriForceClient, _decode_jwt_exp
from aicoder.config import Session
from aicoder.executor import is_simple_chat_message, parse_tool_calls
from aicoder.gui.chat_widget import _AgentWorker, _select_chat_route
import aicoder.gui.settings_widget as settings_widget
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


class FastChatTests(unittest.TestCase):
    def test_greetings_skip_agent_tools(self):
        for text in ("hi", "Hallo!", "moin 👋", "Wie geht's?"):
            with self.subTest(text=text):
                self.assertTrue(is_simple_chat_message(text))

    def test_coding_request_is_not_fast_chat(self):
        self.assertFalse(is_simple_chat_message("Hi, prüfe bitte den OAuth-Code"))
        self.assertFalse(is_simple_chat_message("Warum ist das Modell langsam?"))

    def test_greeting_uses_fast_fallback_directly(self):
        self.assertEqual(
            _select_chat_route("anthropic/slow", "ollama/llama3.2:latest", True),
            ("ollama/llama3.2:latest", "", True),
        )
        self.assertEqual(
            _select_chat_route("anthropic/slow", "ollama/llama3.2:latest", False),
            ("anthropic/slow", "ollama/llama3.2:latest", False),
        )


class ClientLatencyGuardTests(unittest.TestCase):
    def test_pool_timeout_does_not_repeat_request_with_urlopen(self):
        client = TriForceClient("https://example.invalid", timeout=1)
        pool = MagicMock()
        pool.request.side_effect = TimeoutError("read timed out")
        with (
            patch("aicoder.client._get_pool", return_value=pool),
            patch("aicoder.client.urlopen") as fallback_transport,
            self.assertRaises(ClientError),
        ):
            client._do_request("POST", "https://example.invalid/chat", {}, b"{}", "chat/test")
        fallback_transport.assert_not_called()

    def test_chat_disables_automatic_request_retry(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with patch.object(
            client, "_request", return_value={"response": "OK", "model": "test"}
        ) as request:
            client.chat(message="hi", model="test")
        self.assertEqual(request.call_args.kwargs["_retries"], 0)


class GuiToolModeTests(unittest.TestCase):
    def test_no_tools_run_does_not_discover_tools(self):
        client = MagicMock()
        client.chat.return_value = {"response": "Hello", "model": "test"}
        worker = _AgentWorker(
            client,
            [{"role": "system", "content": "simple"}, {"role": "user", "content": "hi"}],
            "test", "", [], "simple",
            load_tools_on_start=False, quick_chat=True,
        )
        with patch("aicoder.gui.chat_widget.load_tools") as discover:
            worker.run()
        discover.assert_not_called()
        self.assertEqual(worker.tools, [])

    def test_disabled_tool_call_is_blocked_before_execution(self):
        client = MagicMock()
        client.chat.side_effect = [
            {"response": '<tool_call>{"name":"health","arguments":{}}</tool_call>', "model": "test"},
            {"response": "Done", "model": "test"},
        ]
        worker = _AgentWorker(
            client,
            [{"role": "system", "content": "simple"}, {"role": "user", "content": "hi"}],
            "test", "", [], "simple",
            load_tools_on_start=False,
        )
        with patch("aicoder.gui.chat_widget.run_tool") as execute:
            worker.run()
        execute.assert_not_called()


class SettingsRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_model_loader_keeps_backend_default_selected(self):
        with patch.object(settings_widget, "load_session", side_effect=RuntimeError("offline")):
            widget = settings_widget.SettingsWidget()
        widget._on_models_loaded(
            ["ollama/ministral-3:14b", "anthropic/claude-opus-4-8"],
            "enterprise",
        )
        self.assertEqual(widget.model_combo.currentText(), "")
        self.assertGreaterEqual(widget.model_combo.minimumWidth(), 500)
        self.assertGreaterEqual(widget.fallback_combo.minimumWidth(), 500)
        widget.close()


if __name__ == "__main__":
    unittest.main()
