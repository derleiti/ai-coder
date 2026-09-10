from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.account_providers import (
    AccountRoutingTransport,
    ChatGPTAccountTransport,
    ClaudeAccountTransport,
    GeminiAccountTransport,
    MistralAccountTransport,
    account_model_id,
    available_account_models,
    is_account_model,
    parse_account_model,
)
from aicoder.client import ClientError


class AccountProviderIdTests(unittest.TestCase):
    def test_account_model_ids_are_unambiguous(self):
        model = account_model_id("chatgpt", "gpt-test")
        self.assertEqual(model, "account:chatgpt/gpt-test")
        self.assertTrue(is_account_model(model))
        self.assertEqual(parse_account_model(model), ("chatgpt", "gpt-test"))

    def test_non_account_model_is_not_claimed(self):
        self.assertFalse(is_account_model("openrouter/test"))
        with self.assertRaises(ClientError):
            parse_account_model("openrouter/test")


class AccountRoutingTests(unittest.TestCase):
    def test_non_account_models_pass_through(self):
        default = MagicMock()
        default.timeout = 30
        default.chat.return_value = {"response": "backend"}
        router = AccountRoutingTransport(default)
        result = router.chat(message="x", model="openrouter/test")
        self.assertEqual(result["response"], "backend")
        default.chat.assert_called_once()

    def test_account_failure_never_falls_back_to_default(self):
        default = MagicMock()
        default.timeout = 30
        transport = MagicMock()
        transport.chat.side_effect = ClientError("not logged in")
        router = AccountRoutingTransport(default)
        router._transports["chatgpt"] = transport
        with self.assertRaisesRegex(ClientError, "not logged in"):
            router.chat(message="x", model="account:chatgpt/gpt-test")
        default.chat.assert_not_called()


class ProviderTransportTests(unittest.TestCase):
    @patch("aicoder.account_providers.shutil.which", return_value="/usr/bin/claude")
    def test_claude_runs_as_tool_free_provider_process(self, _which):
        transport = ClaudeAccountTransport(timeout=30)
        with patch.object(transport, "_run", return_value=("OK\n", "")) as run:
            result = transport.chat(
                model="account:claude/sonnet",
                messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}],
                tools=[{"name": "file_read"}],
                request_id="r1",
            )
        argv = run.call_args.args[0]
        self.assertIn("--print", argv)
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--disallowed-tools", argv)
        self.assertEqual(argv[argv.index("--disallowed-tools") + 1], "*")
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(result["response"], "OK")
        self.assertEqual(result["backend"], "account-claude")
        self.assertIn("[user]\nhello", run.call_args.kwargs["stdin"])

    @patch("aicoder.account_providers.shutil.which", return_value="/usr/bin/vibe")
    def test_mistral_preserves_provider_home_and_disables_tools(self, _which):
        transport = MistralAccountTransport(timeout=30)
        help_result = MagicMock(stdout="usage: vibe --model MODEL\n")
        with patch("aicoder.account_providers.subprocess.run", return_value=help_result), \
             patch.object(transport, "_run", return_value=("OK\n", "")) as run:
            result = transport.chat(model="account:mistral/mistral-medium-latest", message="hello")
        argv = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["VIBE_ACTIVE_MODEL"], "mistral-medium-latest")
        self.assertNotIn("VIBE_HOME", {k for k in env if k == "VIBE_HOME" and k not in os.environ})
        self.assertIn("--disabled-tools", argv)
        self.assertEqual(argv[argv.index("--disabled-tools") + 1], "*")
        self.assertIn("--model", argv)
        self.assertEqual(result["backend"], "account-mistral")

    @patch("aicoder.account_providers.shutil.which", return_value="/usr/bin/gemini")
    def test_gemini_injects_headless_global_deny_policy(self, _which):
        transport = GeminiAccountTransport(timeout=30)
        captured = {}

        def fake_run(argv, **kwargs):
            settings_path = Path(kwargs["env"]["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
            settings = json.loads(settings_path.read_text())
            policy = Path(settings["adminPolicyPaths"][0]).read_text()
            captured.update(argv=argv, policy=policy)
            return json.dumps({"response": "OK"}), ""

        with patch.object(transport, "_run", side_effect=fake_run):
            result = transport.chat(model="account:gemini/pro", message="hello")
        self.assertIn('--model', captured['argv'])
        self.assertEqual(captured['argv'][captured['argv'].index('--model') + 1], 'pro')
        self.assertIn('toolName = "*"', captured['policy'])
        self.assertIn('decision = "deny"', captured['policy'])
        self.assertIn('priority = 999', captured['policy'])
        self.assertIn('interactive = false', captured['policy'])
        self.assertEqual(result["backend"], "account-gemini")

    @patch("aicoder.account_providers.account_status", return_value={
        "provider": "chatgpt", "linked": True, "installed": True, "authenticated": True,
    })
    def test_chatgpt_catalog_comes_from_app_server_model_list(self, _status):
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = None
        fake.model_list.return_value = [{
            "id": "gpt-test", "model": "gpt-test", "displayName": "GPT Test", "isDefault": True,
            "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [],
        }]
        with patch("aicoder.account_providers.CodexAppServer", return_value=fake):
            models = available_account_models("chatgpt")
        self.assertEqual(models[0]["id"], "account:chatgpt/gpt-test")
        self.assertEqual(models[0]["display"], "GPT Test")
        self.assertTrue(models[0]["is_default"])


class ChatGPTTransportTests(unittest.TestCase):
    def test_codex_turn_is_read_only_and_refuses_provider_side_tools(self):
        server = MagicMock()
        server.account_read.return_value = {"account": {"type": "chatgpt"}}
        server._request.side_effect = [
            {"thread": {"id": "thr1"}},
            {"turn": {"id": "turn1"}},
            {},
        ]
        server._receive.side_effect = [
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "OK"}}},
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
        ]
        with patch("aicoder.account_providers.CodexAppServer", return_value=server):
            result = ChatGPTAccountTransport(timeout=30).chat(
                model="account:chatgpt/gpt-test", message="hello", request_id="r1"
            )
        first = server._request.call_args_list[0]
        second = server._request.call_args_list[1]
        self.assertEqual(first.args[0], "thread/start")
        self.assertEqual(first.args[1]["sandbox"], "readOnly")
        self.assertEqual(first.args[1]["approvalPolicy"], "never")
        self.assertEqual(second.args[0], "turn/start")
        self.assertEqual(second.args[1]["sandboxPolicy"]["type"], "readOnly")
        self.assertFalse(second.args[1]["sandboxPolicy"]["access"]["includePlatformDefaults"])
        self.assertEqual(result["response"], "OK")
        self.assertEqual(result["backend"], "account-chatgpt")

    def test_codex_provider_side_item_fails_closed(self):
        server = MagicMock()
        server.account_read.return_value = {"account": {"type": "chatgpt"}}
        server._request.side_effect = [{"thread": {"id": "thr1"}}, {"turn": {"id": "turn1"}}, {}]
        server._receive.return_value = {
            "method": "item/started", "params": {"item": {"type": "commandExecution", "id": "x"}}
        }
        with patch("aicoder.account_providers.CodexAppServer", return_value=server):
            with self.assertRaisesRegex(ClientError, "provider-side item"):
                ChatGPTAccountTransport(timeout=30).chat(model="account:chatgpt/gpt-test", message="hello")


if __name__ == "__main__":
    unittest.main()
