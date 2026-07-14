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
from aicoder.executor import is_simple_chat_message, normalize_tool_calls, parse_tool_calls
from aicoder.gui.chat_widget import _AgentWorker, _select_chat_route
import aicoder.gui.settings_widget as settings_widget
import aicoder.agent as cli_agent
from aicoder.setup import _is_token_expired, run_setup
from aicoder.repl_input import COMMANDS, ReplInput
from aicoder.ui import AgentSpinner
from aicoder.privileges import assess_execution
import aicoder.executor as executor


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
    def test_structured_openai_tool_calls(self):
        self.assertEqual(
            normalize_tool_calls([{
                "id": "call_1",
                "type": "function",
                "function": {"name": "health", "arguments": "{}"},
            }]),
            [{"name": "health", "arguments": {}}],
        )

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

    def test_chat_sends_selected_native_tool_schemas(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        schema = {"name": "health", "inputSchema": {"type": "object"}}
        with patch.object(client, "_request", return_value={"response": "OK"}) as request:
            client.chat(message="check", model="test", tools=[schema])
        payload = request.call_args.args[2]
        self.assertEqual(payload["tools"], [schema])
        self.assertEqual(payload["tool_choice"], "auto")


class GuiToolModeTests(unittest.TestCase):
    def test_native_structured_tool_call_is_executed(self):
        client = MagicMock()
        client.chat.side_effect = [
            {
                "response": "",
                "tool_calls": [{
                    "type": "function",
                    "function": {"name": "health", "arguments": "{}"},
                }],
                "model": "test",
            },
            {"response": "Done", "model": "test"},
        ]
        worker = _AgentWorker(
            client,
            [{"role": "system", "content": "simple"}, {"role": "user", "content": "check"}],
            "test", "", [{"name": "health", "inputSchema": {}}], "simple",
            load_tools_on_start=True,
        )
        with patch("aicoder.gui.chat_widget.run_tool", return_value=("healthy", False)) as execute:
            worker.run()
        execute.assert_called_once()

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
        with (
            patch.object(settings_widget, "load_session", side_effect=RuntimeError("offline")),
            patch.object(settings_widget, "get_state", return_value={"selected_model": ""}),
        ):
            widget = settings_widget.SettingsWidget()
        widget._on_models_loaded(
            ["ollama/ministral-3:14b", "anthropic/claude-opus-4-8"],
            "enterprise",
        )
        self.assertEqual(widget.model_combo.currentText(), "")
        self.assertGreaterEqual(widget.model_combo.minimumWidth(), 500)
        self.assertGreaterEqual(widget.fallback_combo.minimumWidth(), 500)
        widget.close()


class ReplRegressionTests(unittest.TestCase):
    def test_slash_completion_contains_runtime_controls(self):
        for command in ("/clear", "/keys", "/permissions", "/status", "/model", "/exit"):
            self.assertIn(command, COMMANDS)

    def test_basic_input_fallback_remains_usable(self):
        repl = ReplInput.__new__(ReplInput)
        repl._session = None
        with patch("builtins.input", return_value="hello") as basic_input:
            self.assertEqual(repl.read("> "), "hello")
        basic_input.assert_called_once_with("> ")

    def test_spinner_does_not_suppress_task_exceptions(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with AgentSpinner("test", file=io.StringIO()):
                raise RuntimeError("boom")

    def test_spinner_is_silent_when_output_is_redirected(self):
        target = io.StringIO()
        with AgentSpinner("test", file=target):
            pass
        self.assertEqual(target.getvalue(), "")

    def test_cli_greeting_skips_discovery_and_uses_fast_model(self):
        client = MagicMock()
        client.chat.return_value = {
            "response": "Hello",
            "model": "ollama/fast",
            "latency_ms": 20,
        }
        state = {
            "workspace_root": ".",
            "tool_mode": "on_demand",
            "enabled_tools": None,
            "request_timeout": 30,
        }
        session = Session(
            base_url="https://example.invalid",
            token="opaque",
            client_id="test",
            user_id="test@example.invalid",
            tier="registered",
            account_role="user",
        )
        with (
            patch.object(cli_agent, "load_session", return_value=session),
            patch.object(cli_agent, "get_state", return_value=state),
            patch.object(cli_agent, "TriForceClient", return_value=client),
            patch.object(cli_agent, "load_tools") as discover,
            patch.object(cli_agent, "print_header"),
            patch.object(cli_agent, "print_task"),
            patch.object(cli_agent, "print_final"),
            patch.object(cli_agent, "history_record"),
        ):
            result = cli_agent.run_agent("hi", "anthropic/slow", "ollama/fast")
        self.assertEqual(result, 0)
        discover.assert_not_called()
        self.assertEqual(client.chat.call_args.kwargs["model"], "ollama/fast")
        self.assertIsNone(client.chat.call_args.kwargs["fallback_model"])
        self.assertIsNone(client.chat.call_args.kwargs["tools"])


class PrivilegeBrokerTests(unittest.TestCase):
    def test_reads_do_not_need_approval(self):
        risk = assess_execution("file_read", {"command": "cat README.md"})
        self.assertFalse(risk.needs_approval)
        self.assertEqual(risk.level, "read")

    def test_file_creation_and_deletion_need_approval(self):
        create = assess_execution("file_edit", {"command": "touch notes.txt"})
        delete = assess_execution("local_exec", {"command": "rm notes.txt"})
        self.assertTrue(create.needs_approval)
        self.assertTrue(create.mutation)
        self.assertTrue(delete.needs_approval)
        self.assertTrue(delete.deletion)
        self.assertEqual(delete.level, "high")

    def test_sudo_request_preserves_reason_and_protected_scope(self):
        risk = assess_execution("file_edit", {
            "command": "printf enabled > /etc/aicoder.conf",
            "sudo": True,
            "reason": "Aktiviere die lokale Integration",
        })
        self.assertTrue(risk.elevation)
        self.assertTrue(risk.sudo)
        self.assertTrue(risk.protected_path)
        self.assertEqual(risk.user_reason, "Aktiviere die lokale Integration")

    def test_risky_local_tool_is_blocked_without_approval_callback(self):
        with patch.object(executor.audit, "log_tool") as audit_log:
            result, is_error = executor.run_tool(
                MagicMock(), "file_edit", {"command": "touch new.txt"}
            )
        self.assertTrue(is_error)
        self.assertIn("requires explicit approval", result)
        audit_log.assert_called_once()

    def test_sudo_redirect_runs_inside_elevated_shell(self):
        completed = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(executor.subprocess, "run", return_value=completed) as run:
            result, is_error = executor.run_local_exec({
                "command": "printf enabled > /etc/aicoder.conf",
                "sudo": True,
            })
        self.assertFalse(is_error)
        self.assertEqual(result, "(no output)")
        self.assertEqual(
            run.call_args.args[0],
            ["sudo", "--", "sh", "-c", "printf enabled > /etc/aicoder.conf"],
        )
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_cli_sudo_approval_validates_locally_after_consent(self):
        args = {
            "command": "install -m 644 app.conf /etc/app.conf",
            "sudo": True,
            "reason": "Installiere die bestätigte Konfiguration",
        }
        with (
            patch("builtins.input", return_value="j"),
            patch.object(cli_agent, "validate_sudo_session", return_value=(True, "ok")) as validate,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertTrue(cli_agent._cli_approval("file_edit", args))
        validate.assert_called_once_with()

    def test_cli_rejects_unexplained_elevation_request(self):
        with (
            patch("builtins.input") as prompt,
            patch.object(cli_agent, "validate_sudo_session") as validate,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            allowed = cli_agent._cli_approval(
                "local_exec", {"command": "apt update", "sudo": True}
            )
        self.assertFalse(allowed)
        prompt.assert_not_called()
        validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
