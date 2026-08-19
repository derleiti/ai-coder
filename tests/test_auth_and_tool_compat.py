from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from aicoder.client import ClientError, TriForceClient, _decode_jwt_exp, _normalize_chat_response
from aicoder.config import Session
from aicoder.executor import (
    is_action_request, is_simple_chat_message, merge_tool_calls, normalize_tool_calls, parse_tool_calls,
)
from aicoder.gui.chat_widget import _AgentWorker, _select_chat_route
import aicoder.gui.settings_widget as settings_widget
import aicoder.agent as cli_agent
from aicoder.setup import _is_token_expired, run_setup
from aicoder.repl_input import COMMANDS, ReplInput
from aicoder.ui import AgentSpinner
from aicoder.privileges import assess_execution
import aicoder.executor as executor
import aicoder.repl_input as repl_input_module


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
    def test_structured_openai_compatible_tool_calls_preserve_id(self):
        self.assertEqual(
            normalize_tool_calls([{
                "id": "call_1",
                "type": "function",
                "function": {"name": "health", "arguments": "{}"},
            }]),
            [{"name": "health", "arguments": {}, "id": "call_1", "raw_type": "function"}],
        )

    def test_openai_responses_function_call_preserves_call_id(self):
        normalized = _normalize_chat_response({
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "Checking"}]},
                {"type": "function_call", "call_id": "call_resp_1", "name": "health", "arguments": "{}"},
            ]
        })
        self.assertEqual(normalized["response"], "Checking")
        self.assertEqual(
            normalize_tool_calls(normalized["tool_calls"]),
            [{
                "name": "health", "arguments": {}, "id": "call_resp_1",
                "provider": "openai", "raw_type": "function_call",
            }],
        )

    def test_anthropic_tool_use_preserves_id(self):
        normalized = _normalize_chat_response({
            "content": [
                {"type": "text", "text": "Checking"},
                {"type": "tool_use", "id": "toolu_1", "name": "health", "input": {}},
            ]
        })
        self.assertEqual(normalized["response"], "Checking")
        self.assertEqual(
            normalize_tool_calls(normalized["tool_calls"]),
            [{
                "name": "health", "arguments": {}, "id": "toolu_1",
                "provider": "anthropic", "raw_type": "tool_use",
            }],
        )

    def test_gemini_function_call_preserves_id_and_signature(self):
        normalized = _normalize_chat_response({
            "candidates": [{"content": {"parts": [
                {"text": "Checking"},
                {
                    "functionCall": {"id": "gem_1", "name": "health", "args": {}},
                    "thoughtSignature": "opaque-signature",
                },
            ]}}]
        })
        self.assertEqual(normalized["response"], "Checking")
        self.assertEqual(
            normalize_tool_calls(normalized["tool_calls"]),
            [{
                "name": "health", "arguments": {}, "id": "gem_1",
                "provider": "gemini",
                "metadata": {"thoughtSignature": "opaque-signature"},
            }],
        )

    def test_ollama_message_tool_calls_are_normalized(self):
        normalized = _normalize_chat_response({
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "health", "arguments": {}}}],
            }
        })
        self.assertEqual(normalized["response"], "")
        self.assertEqual(
            normalize_tool_calls(normalized["tool_calls"]),
            [{"name": "health", "arguments": {}}],
        )

    def test_native_and_text_tool_call_are_deduplicated_without_losing_id(self):
        native = normalize_tool_calls([{
            "id": "call_1", "type": "function",
            "function": {"name": "health", "arguments": "{}"},
        }])
        textual = parse_tool_calls('<tool_call>{"name":"health","arguments":{}}</tool_call>')
        merged = merge_tool_calls(native, textual)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "call_1")

    def test_parallel_tool_calls_preserve_order_and_ids(self):
        calls = normalize_tool_calls([
            {"id": "call_a", "type": "function", "function": {"name": "health", "arguments": "{}"}},
            {"id": "call_b", "type": "function", "function": {"name": "code_read", "arguments": "{\"path\":\"README.md\"}"}},
        ])
        self.assertEqual([call["name"] for call in calls], ["health", "code_read"])
        self.assertEqual([call["id"] for call in calls], ["call_a", "call_b"])

    def test_native_openai_shape(self):
        text = '<tool_call>{"type":"function","function":{"name":"code_read","arguments":"{\\"path\\":\\"README.md\\"}"}}</tool_call>'
        self.assertEqual(
            parse_tool_calls(text),
            [{
                "name": "code_read", "arguments": {"path": "README.md"},
                "raw_type": "function",
            }],
        )

    def test_mistral_shape(self):
        text = '[TOOL_CALLS] [{"name":"code_search","arguments":{"query":"oauth"}}]'
        self.assertEqual(
            parse_tool_calls(text),
            [{"name": "code_search", "arguments": {"query": "oauth"}}],
        )

    def test_tool_call_repairs_missing_outer_brace(self):
        # Observed from mistral-code-agent: complete XML envelope, but the JSON
        # closes arguments and omits only the final outer object brace.
        text = (
            "<tool_call>\n"
            "{\"name\":\"file_edit\",\"arguments\":{\"command\":\"cat > ~/x << 'EOF'\\nhello\\nEOF\"}\n"
            "</tool_call>"
        )
        self.assertEqual(
            parse_tool_calls(text),
            [{"name": "file_edit", "arguments": {"command": "cat > ~/x << 'EOF'\nhello\nEOF"}}],
        )

    def test_truncated_unclosed_tool_call_is_not_guessed(self):
        text = "<tool_call>\n{\"name\":\"file_edit\",\"arguments\":{\"command\":\"cat > ~/x << 'EOF'\\nhello"
        self.assertEqual(parse_tool_calls(text), [])

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

    def test_greeting_preserves_primary_and_fallback(self):
        expected = ("anthropic/slow", "ollama/llama3.2:latest", False)
        self.assertEqual(
            _select_chat_route("anthropic/slow", "ollama/llama3.2:latest", True),
            expected,
        )
        self.assertEqual(
            _select_chat_route("anthropic/slow", "ollama/llama3.2:latest", False),
            expected,
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
        for command in ("/clear", "/keys", "/new", "/permissions", "/status", "/model", "/exit"):
            self.assertIn(command, COMMANDS)

    def test_operational_request_is_classified_for_tool_followup(self):
        self.assertTrue(is_action_request("Sortiere meine Dokumente unter ~/Documents"))
        self.assertTrue(is_action_request("Prüfe bitte den Docker Socket"))
        self.assertFalse(is_action_request("Erkläre mir den Unterschied zwischen Listen und Tupeln"))
        self.assertFalse(is_action_request("Welches Testwort solltest du dir merken?"))

    def test_agent_budget_no_longer_stops_at_thirty_steps(self):
        self.assertGreaterEqual(executor.MAX_ITERATIONS, 60)
        self.assertEqual(executor.AGENT_CHECKPOINT_INTERVAL, 30)

    def test_loop_guard_distinguishes_stagnation_from_progress(self):
        guard = executor.AgentLoopGuard()
        calls = [{"name": "file_read", "arguments": {"command": "pwd"}}]
        repeats = [guard.observe(calls, ["same result"]) for _ in range(6)]
        self.assertEqual(repeats, [1, 2, 3, 4, 5, 6])
        guard.reset()
        self.assertEqual(guard.observe(calls, ["new result"]), 1)

    def test_loop_guard_ignores_provider_call_id_churn(self):
        guard = executor.AgentLoopGuard()
        first = [{"name": "health", "arguments": {}, "id": "call_1"}]
        second = [{"name": "health", "arguments": {}, "id": "call_2"}]
        self.assertEqual(guard.observe(first, ["same result"]), 1)
        self.assertEqual(guard.observe(second, ["same result"]), 2)

    def test_basic_input_fallback_remains_usable(self):
        repl = ReplInput.__new__(ReplInput)
        repl._session = None
        with patch("builtins.input", return_value="hello") as basic_input:
            self.assertEqual(repl.read("> "), "hello")
        basic_input.assert_called_once_with("> ")

    def test_enhanced_input_falls_back_to_memory_when_history_is_read_only(self):
        if repl_input_module.PromptSession is None:
            repl = ReplInput(Path("/read-only/history"), lambda: "")
            self.assertFalse(repl.enhanced)
            self.assertFalse(repl.persistent_history)
            return
        with patch.object(Path, "open", side_effect=OSError("read only")):
            repl = ReplInput(Path("/read-only/history"), lambda: "")
        self.assertTrue(repl.enhanced)
        self.assertFalse(repl.persistent_history)
        self.assertEqual(type(repl._session.history).__name__, "InMemoryHistory")

    def test_spinner_does_not_suppress_task_exceptions(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with AgentSpinner("test", file=io.StringIO()):
                raise RuntimeError("boom")

    def test_spinner_is_silent_when_output_is_redirected(self):
        target = io.StringIO()
        with AgentSpinner("test", file=target):
            pass
        self.assertEqual(target.getvalue(), "")

    def test_cli_greeting_skips_discovery_and_preserves_primary(self):
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
        self.assertEqual(client.chat.call_args.kwargs["model"], "anthropic/slow")
        self.assertEqual(client.chat.call_args.kwargs["fallback_model"], "ollama/fast")
        self.assertIsNone(client.chat.call_args.kwargs["tools"])

    def test_repl_conversation_is_reused_and_action_gets_one_tool_nudge(self):
        client = MagicMock()
        client.chat.side_effect = [
            {"response": "Ich würde zuerst die Ordner ansehen.", "model": "test"},
            {
                "response": '<tool_call>{"name":"file_tree","arguments":{"command":"ls -la ~/Documents"}}</tool_call>',
                "model": "test",
            },
            {"response": "DONE: Dokumente geprüft.", "model": "test"},
        ]
        state = {
            "workspace_root": ".",
            "tool_mode": "always",
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
        conversation = [
            {"role": "user", "content": "Wir arbeiten in meinem Home-Verzeichnis."},
            {"role": "assistant", "content": "Verstanden."},
        ]
        with (
            patch.object(cli_agent, "load_session", return_value=session),
            patch.object(cli_agent, "get_state", return_value=state),
            patch.object(cli_agent, "TriForceClient", return_value=client),
            patch.object(cli_agent, "load_tools", return_value=[executor.LOCAL_FILE_TREE_SCHEMA]),
            patch.object(cli_agent, "run_tool", return_value=("documents", False)) as execute,
            patch.object(cli_agent, "print_header"),
            patch.object(cli_agent, "print_task"),
            patch.object(cli_agent, "print_thought"),
            patch.object(cli_agent, "print_tool_call"),
            patch.object(cli_agent, "print_tool_result"),
            patch.object(cli_agent, "print_final"),
            patch.object(cli_agent, "history_record"),
        ):
            result = cli_agent.run_agent(
                "Sortiere meine Dokumente", "test", None, conversation=conversation,
            )
        self.assertEqual(result, 0)
        self.assertEqual(client.chat.call_count, 3)
        self.assertTrue(any(
            "No tool has been used yet" in m["content"] for m in conversation
        ))
        self.assertEqual(execute.call_count, 1)
        self.assertTrue(any(m["content"] == "Wir arbeiten in meinem Home-Verzeichnis." for m in conversation))
        self.assertEqual(conversation[-1]["content"], "DONE: Dokumente geprüft.")

    def test_gui_switches_to_fallback_after_repeated_tool_loop(self):
        client = MagicMock()
        repeated = {
            "response": '<tool_call>{"name":"health","arguments":{}}</tool_call>',
            "model": "operator",
        }
        client.chat.side_effect = [repeated.copy() for _ in range(6)] + [
            {"response": "DONE: recovered", "model": "fallback"},
        ]
        worker = _AgentWorker(
            client,
            [{"role": "system", "content": "simple"}, {"role": "user", "content": "check"}],
            "operator", "fallback", [{"name": "health", "inputSchema": {}}], "simple",
            load_tools_on_start=True,
        )
        with patch("aicoder.gui.chat_widget.run_tool", return_value=("same result", False)):
            worker.run()
        self.assertEqual(client.chat.call_count, 7)
        self.assertEqual(client.chat.call_args_list[6].kwargs["model"], "fallback")
        self.assertIsNone(client.chat.call_args_list[6].kwargs["fallback_model"])


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

    def test_mutating_mcp_tool_is_blocked_without_approval_callback(self):
        client = MagicMock()
        with patch.object(executor.audit, "log_tool") as audit_log:
            result, is_error = executor.run_tool(
                client, "code_patch", {"patch": "--- a/x\n+++ b/x\n"}
            )
        self.assertTrue(is_error)
        self.assertIn("requires explicit approval", result)
        client.mcp_call.assert_not_called()
        audit_log.assert_called_once()

    def test_mutating_mcp_tool_runs_after_local_approval(self):
        client = MagicMock()
        client.mcp_call.return_value = {
            "result": {"content": [{"type": "text", "text": "patched"}]}
        }
        approval = MagicMock(return_value=True)
        with patch.object(executor.audit, "log_tool"):
            result, is_error = executor.run_tool(
                client, "code_patch", {"patch": "--- a/x\n+++ b/x\n"},
                approval_fn=approval,
            )
        self.assertFalse(is_error)
        self.assertEqual(result, "patched")
        approval.assert_called_once()
        client.mcp_call.assert_called_once()

    def test_cli_sudo_request_is_always_rejected(self):
        args = {
            "command": "install -m 644 app.conf /etc/app.conf",
            "sudo": True,
            "reason": "Installiere die bestätigte Konfiguration",
        }
        with (
            patch("builtins.input") as prompt,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertFalse(cli_agent._cli_approval("file_edit", args))
        prompt.assert_not_called()

    def test_cli_rejects_unexplained_elevation_request(self):
        with (
            patch("builtins.input") as prompt,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            allowed = cli_agent._cli_approval(
                "local_exec", {"command": "apt update", "sudo": True}
            )
        self.assertFalse(allowed)
        prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ToolSecurityHardeningTests(unittest.TestCase):
    def test_namespaced_mutating_tool_requires_approval(self):
        for name in ("mcp.code_edit", "server/code_patch", "plugin:memory_clear"):
            with self.subTest(name=name):
                self.assertTrue(assess_execution(name, {}).needs_approval)

    def test_dynamic_mutating_hint_requires_approval(self):
        self.assertTrue(assess_execution("future_tool", {"_mutating": True}).needs_approval)
        self.assertFalse(assess_execution("future_tool", {"_mutating": False}).needs_approval)

    def test_tool_cache_is_scoped_per_authenticated_client(self):
        class FakeClient:
            def __init__(self, base_url, token, tool_name):
                self.base_url = base_url
                self.token = token
                self.tool_name = tool_name
                self.calls = 0

            def _request(self, *args, **kwargs):
                self.calls += 1
                return {"result": {"tools": [{
                    "name": self.tool_name,
                    "description": "test",
                    "inputSchema": {"type": "object", "properties": {}},
                }]}}

        saved = (
            executor.AGENT_TOOLS, executor._tool_cache, executor._tool_cache_ts,
            executor._tool_cache_key, executor._tool_security_hints,
        )
        try:
            executor.AGENT_TOOLS = {"health", "status"}
            executor._tool_cache = None
            executor._tool_cache_ts = 0
            executor._tool_cache_key = None
            executor._tool_security_hints = {}
            first = FakeClient("https://one.invalid", "token-a", "health")
            second = FakeClient("https://one.invalid", "token-b", "status")
            first_tools = executor.load_tools(first)
            second_tools = executor.load_tools(second)
            self.assertIn("health", {tool["name"] for tool in first_tools})
            self.assertIn("status", {tool["name"] for tool in second_tools})
            self.assertEqual(first.calls, 1)
            self.assertEqual(second.calls, 1)
        finally:
            (
                executor.AGENT_TOOLS, executor._tool_cache, executor._tool_cache_ts,
                executor._tool_cache_key, executor._tool_security_hints,
            ) = saved
