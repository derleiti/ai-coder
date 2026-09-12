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
    GrokAccountTransport,
    MistralAccountTransport,
    account_model_id,
    account_status,
    antigravity_quota_status,
    reroute_account_model_if_unavailable,
    available_account_models,
    is_account_model,
    parse_account_model,
    connect_account,
    ensure_provider_client,
    _external_cli_env,
)
from aicoder.client import ClientError


class ExternalCliEnvironmentTests(unittest.TestCase):
    def test_frozen_binary_restores_original_loader_path(self):
        base = {
            "PATH": "/usr/bin",
            "LD_LIBRARY_PATH": "/tmp/_MEI-bundle",
            "LD_LIBRARY_PATH_ORIG": "/opt/vendor/lib",
        }
        with patch("aicoder.account_providers.sys.frozen", True, create=True):
            env = _external_cli_env(base=base)
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/vendor/lib")
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertIn(str(Path.home() / ".local" / "bin"), env["PATH"])

    def test_frozen_binary_removes_injected_loader_path_when_no_original_exists(self):
        base = {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/tmp/_MEI-bundle", "LD_LIBRARY_PATH_ORIG": ""}
        with patch("aicoder.account_providers.sys.frozen", True, create=True):
            env = _external_cli_env(base=base)
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")


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


class AccountQuotaRoutingTests(unittest.TestCase):
    def test_antigravity_quota_status_reads_active_reset_window(self):
        from datetime import datetime
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "cli-20260911_095719.log"
            log.write_text(
                "I0911 09:57:22.671014 626 run.go:387] Run failed "
                "(RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
                "Please upgrade. Resets in 139h55m52s.)\n"
            )
            status = antigravity_quota_status(
                log_dir=tmp, now=datetime.fromisoformat("2026-09-11T10:00:00+02:00")
            )
        self.assertTrue(status["quota_exhausted"])
        self.assertGreater(status["quota_retry_after_seconds"], 139 * 3600)
        self.assertIn("2026-09-17", status["quota_reset_at"])

    def test_antigravity_quota_status_ignores_expired_window(self):
        from datetime import datetime
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "cli-20260911_095719.log"
            log.write_text(
                "I0911 09:57:22.671014 626 run.go:387] "
                "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 2s.\n"
            )
            status = antigravity_quota_status(
                log_dir=tmp, now=datetime.fromisoformat("2026-09-11T10:00:00+02:00")
            )
        self.assertFalse(status["quota_exhausted"])

    def test_quota_exhausted_gemini_reroutes_to_authenticated_claude(self):
        def status(provider):
            if provider == "gemini":
                return {
                    "provider": provider, "installed": True, "linked": True,
                    "authenticated": True, "quota_exhausted": True,
                    "quota_retry_after_seconds": 3600, "quota_reset_at": "later",
                }
            if provider == "claude":
                return {
                    "provider": provider, "installed": True, "linked": True,
                    "authenticated": True, "quota_exhausted": False,
                }
            return {
                "provider": provider, "installed": False, "linked": False,
                "authenticated": False, "quota_exhausted": False,
            }
        with patch("aicoder.account_providers.account_status", side_effect=status):
            model, info = reroute_account_model_if_unavailable(
                "account:gemini/gemini-3.8-flash-high"
            )
        self.assertEqual(model, "account:claude/sonnet")
        self.assertEqual(info["reason"], "quota_exhausted")
        self.assertEqual(info["from_model"], "account:gemini/gemini-3.8-flash-high")

    def test_non_quota_failure_is_not_hidden_by_reroute(self):
        with patch("aicoder.account_providers.account_status", return_value={
            "provider": "gemini", "installed": True, "linked": False,
            "authenticated": False, "quota_exhausted": False,
        }):
            model, info = reroute_account_model_if_unavailable(
                "account:gemini/gemini-3.8-flash-high"
            )
        self.assertEqual(model, "account:gemini/gemini-3.8-flash-high")
        self.assertIsNone(info)


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


    @patch("aicoder.account_providers.shutil.which", return_value="/usr/bin/claude")
    def test_claude_filters_known_cli_tool_capability_diagnostic(self, _which):
        transport = ClaudeAccountTransport(timeout=30)
        noisy = "OK\nClient.listTools() called but server does not advertise tools capability - returning empty list\n"
        with patch.object(transport, "_run", return_value=(noisy, "")):
            result = transport.chat(model="account:claude/sonnet", message="hello")
        self.assertEqual(result["response"], "OK")

    @patch("aicoder.account_providers.shutil.which", return_value="/usr/bin/vibe")
    def test_mistral_preserves_provider_home_and_disables_tools(self, _which):
        transport = MistralAccountTransport(timeout=30)
        help_result = MagicMock(stdout="usage: vibe --model MODEL\n")
        with patch("aicoder.account_providers.subprocess.run", return_value=help_result), \
             patch("aicoder.account_providers._mistral_authenticated", return_value=True), \
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


    def test_mistral_fast_fails_when_login_is_required(self):
        transport = MistralAccountTransport(timeout=60)
        with patch("aicoder.account_providers._which_executable", return_value="/home/test/.local/bin/vibe"), \
             patch("aicoder.account_providers._mistral_authenticated", return_value=False), \
             patch.object(transport, "_run") as run:
            with self.assertRaisesRegex(ClientError, "^Mistral Vibe login required$"):
                transport.chat(model="account:mistral/mistral-large-latest", message="hello")
        run.assert_not_called()

    @patch("aicoder.account_providers._which", return_value="/home/test/.local/bin/vibe")
    def test_mistral_status_distinguishes_linked_from_authenticated(self, _which):
        with patch("aicoder.account_providers.linked_provider_ids", return_value={"mistral"}), \
             patch("aicoder.account_providers._mistral_authenticated", return_value=False):
            status = account_status("mistral")
        self.assertTrue(status["installed"])
        self.assertTrue(status["linked"])
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["detail"], "Mistral Vibe login required")

    @patch("aicoder.account_providers.shutil.which", return_value="/home/test/.local/bin/agy")
    def test_antigravity_runs_headless_plan_sandbox_with_selected_model(self, _which):
        transport = GeminiAccountTransport(timeout=30)
        with patch("aicoder.account_providers._antigravity_authenticated", return_value=True), \
             patch.object(transport, "_run", return_value=(json.dumps({"response": "OK"}), "")) as run:
            result = transport.chat(model="account:gemini/gemini-3.8-flash-high", message="hello")
        argv = run.call_args.args[0]
        self.assertTrue(argv[0].endswith("agy"))
        self.assertIn("--print", argv)
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-high")
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertIn("--sandbox", argv)
        self.assertIn("--disable-slash-commands", argv)
        self.assertEqual(result["backend"], "account-antigravity")


    def test_antigravity_surfaces_provider_json_error(self):
        transport = GeminiAccountTransport(timeout=30)
        payload = json.dumps({"status": "ERROR", "response": "", "error": "The stream was interrupted."})
        with patch("aicoder.account_providers._which_executable", return_value="/usr/bin/agy"), \
             patch("aicoder.account_providers._antigravity_authenticated", return_value=True), \
             patch.object(transport, "_run", return_value=(payload, "")):
            with self.assertRaisesRegex(ClientError, "Google Antigravity request failed: The stream was interrupted"):
                transport.chat(model="account:gemini/gemini-3.8-flash-high", message="hello")

    def test_antigravity_fast_fails_when_login_is_required(self):
        transport = GeminiAccountTransport(timeout=60)
        with patch("aicoder.account_providers._which_executable", return_value="/home/test/.local/bin/agy"), \
             patch("aicoder.account_providers._antigravity_authenticated", return_value=False), \
             patch.object(transport, "_run") as run:
            with self.assertRaisesRegex(ClientError, "^Antigravity login required$"):
                transport.chat(model="account:gemini/gemini-3.8-flash-high", message="hello")
        run.assert_not_called()



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

    def test_grok_runs_headless_plan_without_tools(self):
        transport = GrokAccountTransport(timeout=30)
        with patch("aicoder.account_providers._which_executable", return_value="/usr/bin/grok"), \
             patch("aicoder.account_providers._grok_authenticated", return_value=True), \
             patch.object(transport, "_run", return_value=("OK\n", "")) as run:
            result = transport.chat(model="account:grok/grok-4.6", message="hello")
        argv = run.call_args.args[0]
        self.assertIn("--single", argv)
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--disable-web-search", argv)
        self.assertIn("--no-subagents", argv)
        self.assertEqual(result["backend"], "account-grok")

    def test_grok_fast_fails_when_login_required(self):
        transport = GrokAccountTransport(timeout=30)
        with patch("aicoder.account_providers._which_executable", return_value="/usr/bin/grok"), \
             patch("aicoder.account_providers._grok_authenticated", return_value=False), \
             patch.object(transport, "_run") as run:
            with self.assertRaisesRegex(ClientError, "^Grok login required$"):
                transport.chat(model="account:grok/grok-4.6", message="hello")
        run.assert_not_called()

    @patch("aicoder.account_providers.account_status", return_value={
        "provider": "grok", "linked": True, "installed": True, "authenticated": True,
    })
    def test_grok_catalog_comes_from_cli_models(self, _status):
        with patch("aicoder.account_providers._which", return_value="/usr/bin/grok"), \
             patch("aicoder.account_providers._grok_models", return_value=[
                 {"model": "grok-4.6", "display": "grok-4.6 (default)"},
                 {"model": "grok-4.5", "display": "grok-4.5"},
             ]):
            models = available_account_models("grok")
        self.assertEqual([m["model"] for m in models], ["grok-4.6", "grok-4.5"])
        self.assertEqual(models[0]["id"], "account:grok/grok-4.6")


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
        self.assertEqual(first.args[1]["sandbox"], "read-only")
        self.assertEqual(first.args[1]["approvalPolicy"], "never")
        self.assertEqual(second.args[0], "turn/start")
        self.assertEqual(second.args[1]["sandboxPolicy"]["type"], "readOnly")
        self.assertFalse(second.args[1]["sandboxPolicy"]["networkAccess"])
        self.assertNotIn("access", second.args[1]["sandboxPolicy"])
        self.assertEqual(result["response"], "OK")
        self.assertEqual(result["backend"], "account-chatgpt")

    def test_codex_failed_turn_surfaces_provider_error(self):
        server = MagicMock()
        server.account_read.return_value = {"account": {"type": "chatgpt"}}
        server._request.side_effect = [{"thread": {"id": "thr1"}}, {"turn": {"id": "turn1"}}, {}]
        server._receive.side_effect = [
            {"method": "error", "params": {"error": {
                "message": "Your workspace is out of credits. Add credits to continue.",
                "codexErrorInfo": "usageLimitExceeded",
            }}},
            {"method": "turn/completed", "params": {"turn": {
                "status": "failed",
                "error": {"message": "Your workspace is out of credits. Add credits to continue.",
                          "codexErrorInfo": "usageLimitExceeded"},
            }}},
        ]
        with patch("aicoder.account_providers.CodexAppServer", return_value=server):
            with self.assertRaisesRegex(ClientError, "out of credits.*usageLimitExceeded"):
                ChatGPTAccountTransport(timeout=30).chat(model="account:chatgpt/gpt-test", message="hello")

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

    def test_claude_account_transport_drops_api_key_precedence(self):
        transport = ClaudeAccountTransport(timeout=30)
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY":"secret", "ANTHROPIC_AUTH_TOKEN":"gateway", "ANTHROPIC_BASE_URL":"https://example.invalid"}, clear=False), \
             patch("aicoder.account_providers._which_executable", return_value="/usr/bin/claude"), \
             patch.object(transport, "_run", return_value=("OK\n", "")) as run:
            result = transport.chat(model="account:claude/claude-opus-5", message="hello")
        env = run.call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertEqual(result["response"], "OK")


class ClaudeAccountStatusTests(unittest.TestCase):
    def test_claude_status_uses_official_json_logged_in_flag(self):
        completed = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "loggedIn": True,
                "authMethod": "claude.ai",
                "subscriptionType": "max",
                "email": "user@example.com",
            }),
            stderr="",
        )
        with patch("aicoder.account_providers._which", return_value="/home/test/.local/bin/claude"), \
             patch("aicoder.account_providers.linked_provider_ids", return_value=[]), \
             patch("aicoder.account_providers.subprocess.run", return_value=completed):
            from aicoder.account_providers import account_status
            status = account_status("claude")
        self.assertTrue(status["authenticated"])
        self.assertTrue(status["linked"])
        self.assertIn("claude.ai", status["detail"])
        self.assertEqual(status["subscription"], "max")

    def test_stale_claude_link_is_cleared_when_official_client_is_logged_out(self):
        completed = MagicMock(
            returncode=1,
            stdout=json.dumps({"loggedIn": False}),
            stderr="",
        )
        with patch("aicoder.account_providers._which", return_value="/home/test/.local/bin/claude"), \
             patch("aicoder.account_providers.subprocess.run", return_value=completed), \
             patch("aicoder.account_providers.linked_provider_ids", return_value=["claude"]), \
             patch("aicoder.account_providers.set_provider_linked") as set_linked:
            from aicoder.account_providers import account_status
            status = account_status("claude")
        self.assertFalse(status["authenticated"])
        self.assertFalse(status["linked"])
        self.assertIn("Nicht angemeldet", status["detail"])
        set_linked.assert_called_once_with("claude", False)

    def test_claude_login_keeps_terminal_open_and_polls_official_status(self):
        logged_out = {
            "provider": "claude", "linked": False, "installed": True,
            "authenticated": False, "detail": "Nicht angemeldet",
        }
        logged_in = {
            "provider": "claude", "linked": True, "installed": True,
            "authenticated": True, "detail": "Verbunden · claude.ai",
        }
        with patch("aicoder.account_providers.ensure_provider_client", return_value="/home/test/.local/bin/claude"), \
             patch("aicoder.account_providers._claude_status", side_effect=[logged_out, logged_out, logged_in]), \
             patch("aicoder.account_providers._launch_terminal", return_value=None) as terminal, \
             patch("aicoder.account_providers.time.sleep"), \
             patch("aicoder.account_providers.set_provider_linked") as linked:
            result = connect_account("claude")
        terminal.assert_called_once_with(
            ["/home/test/.local/bin/claude", "auth", "login", "--claudeai"],
            title="AICoder · Claude Login",
            wait=False,
        )
        self.assertTrue(result["authenticated"])
        self.assertTrue(result["started"])
        linked.assert_called_once_with("claude", True)

    def test_authenticated_claude_exposes_latest_alias_models(self):
        with patch("aicoder.account_providers.account_status", return_value={
            "provider": "claude", "linked": True, "installed": True, "authenticated": True,
        }):
            models = available_account_models("claude")
        self.assertEqual([m["model"] for m in models], ["sonnet", "opus", "fable", "haiku"])
        self.assertTrue(all(m["id"].startswith("account:claude/") for m in models))
        self.assertTrue(all("latest" in m["display"].lower() for m in models))


class AccountInstallAndLoginTests(unittest.TestCase):
    def test_missing_codex_is_installed_user_local_with_official_package(self):
        calls = []
        def fake_which(name, path=None):
            calls.append((name, path))
            if name == "npm":
                return "/usr/bin/npm"
            if name == "codex" and len([c for c in calls if c[0] == "codex"]) > 1:
                return str(Path.home() / ".local/bin/codex")
            return None
        completed = MagicMock(returncode=0, stdout="", stderr="")
        with patch("aicoder.account_providers.shutil.which", side_effect=fake_which), \
             patch("aicoder.account_providers.subprocess.run", return_value=completed) as run:
            path = ensure_provider_client("chatgpt")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["/usr/bin/npm", "install", "-g"])
        self.assertIn("--prefix", argv)
        self.assertEqual(argv[argv.index("--prefix") + 1], str(Path.home() / ".local"))
        self.assertEqual(argv[-1], "@openai/codex@latest")
        self.assertTrue(path.endswith("/.local/bin/codex"))

    def test_missing_gemini_uses_official_antigravity_installer(self):
        seen = {"agy": 0}
        def fake_which(name, path=None):
            if name == "curl": return "/usr/bin/curl"
            if name == "bash": return "/usr/bin/bash"
            if name == "agy":
                seen["agy"] += 1
                return None if seen["agy"] == 1 else str(Path.home() / ".local/bin/agy")
            return None
        download = MagicMock(returncode=0, stdout=b"#!/bin/sh\nexit 0\n", stderr=b"")
        install = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with patch("aicoder.account_providers.shutil.which", side_effect=fake_which), \
             patch("aicoder.account_providers.subprocess.run", side_effect=[download, install]) as run:
            path = ensure_provider_client("gemini")
        first_argv = run.call_args_list[0].args[0]
        second_argv = run.call_args_list[1].args[0]
        self.assertEqual(first_argv, ["/usr/bin/curl", "-fsSL", "https://antigravity.google/cli/install.sh"])
        self.assertEqual(second_argv, ["/usr/bin/bash"])
        self.assertTrue(path.endswith("/.local/bin/agy"))


    def test_chatgpt_app_server_failure_falls_back_to_official_device_auth(self):
        first = MagicMock()
        first.__enter__.return_value = first
        first.__exit__.return_value = None
        first.account_read.return_value = {"account": None}
        second = MagicMock()
        second.__enter__.return_value = second
        second.__exit__.return_value = None
        second.login_chatgpt.side_effect = ClientError("browser callback failed")
        third = MagicMock()
        third.__enter__.return_value = third
        third.__exit__.return_value = None
        third.account_read.return_value = {"account": {"type": "chatgpt", "planType": "plus"}}
        with patch("aicoder.account_providers.ensure_provider_client", return_value="/home/test/.local/bin/codex"), \
             patch("aicoder.account_providers.CodexAppServer", side_effect=[first, second, third]), \
             patch("aicoder.account_providers._launch_terminal", return_value=0) as terminal, \
             patch("aicoder.account_providers.set_provider_linked") as linked:
            result = connect_account("chatgpt")
        terminal.assert_called_once_with(
            ["/home/test/.local/bin/codex", "login", "--device-auth"],
            title="AICoder · ChatGPT Device Login", wait=True,
        )
        linked.assert_called_once_with("chatgpt", True)
        self.assertTrue(result["authenticated"])

    def test_antigravity_models_are_dynamic_from_agy_models(self):
        completed = MagicMock(
            returncode=0,
            stdout="Fetching available models...\ngemini-3.8-flash-high Gemini 3.8 Flash (High)\nclaude-sonnet-4-6 Claude Sonnet 4.6 (Thinking)\n",
            stderr="",
        )
        with patch("aicoder.account_providers.account_status", return_value={
            "provider": "gemini", "linked": True, "installed": True, "authenticated": None,
        }), patch("aicoder.account_providers._which", return_value="/home/test/.local/bin/agy"), \
             patch("aicoder.account_providers.subprocess.run", return_value=completed):
            models = available_account_models("gemini")
        self.assertEqual([m["model"] for m in models], ["gemini-3.8-flash-high", "claude-sonnet-4-6"])
        self.assertEqual(models[0]["id"], "account:gemini/gemini-3.8-flash-high")


    def test_gemini_authentication_self_heals_persisted_link(self):
        with patch("aicoder.account_providers._which", return_value="/home/test/.local/bin/agy"), \
             patch("aicoder.account_providers.linked_provider_ids", return_value=[]), \
             patch("aicoder.account_providers._antigravity_authenticated", return_value=True), \
             patch("aicoder.account_providers.set_provider_linked") as set_linked:
            status = account_status("gemini")
        self.assertTrue(status["linked"])
        self.assertTrue(status["authenticated"])
        set_linked.assert_called_once_with("gemini", True)

    def test_stale_gemini_link_is_cleared_when_antigravity_is_logged_out(self):
        with patch("aicoder.account_providers._which", return_value="/home/test/.local/bin/agy"), \
             patch("aicoder.account_providers.linked_provider_ids", return_value=["gemini"]), \
             patch("aicoder.account_providers._antigravity_authenticated", return_value=False), \
             patch("aicoder.account_providers.set_provider_linked") as set_linked:
            status = account_status("gemini")
        self.assertFalse(status["linked"])
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["detail"], "Nicht verbunden · Mit Antigravity verbinden")
        set_linked.assert_called_once_with("gemini", False)

    def test_gemini_is_linked_only_after_login_verification(self):
        with patch("aicoder.account_providers.ensure_provider_client", return_value="/home/test/.local/bin/agy"), \
             patch("aicoder.account_providers._launch_terminal", return_value=None) as terminal, \
             patch("aicoder.account_providers._antigravity_authenticated", side_effect=[False, True]), \
             patch("aicoder.account_providers.set_provider_linked") as linked:
            result = connect_account("gemini")
        terminal.assert_called_once_with(
            ["/home/test/.local/bin/agy"], title="AICoder · Google Antigravity Login", wait=False
        )
        linked.assert_called_once_with("gemini", True)
        self.assertTrue(result["authenticated"])

    def test_gemini_login_does_not_wait_for_long_lived_tui_to_exit(self):
        with patch("aicoder.account_providers.ensure_provider_client", return_value="/home/test/.local/bin/agy"), \
             patch("aicoder.account_providers._launch_terminal", return_value=None) as terminal, \
             patch("aicoder.account_providers._antigravity_authenticated", side_effect=[False, False, True]), \
             patch("aicoder.account_providers.time.sleep"), \
             patch("aicoder.account_providers.set_provider_linked"):
            result = connect_account("gemini")
        terminal.assert_called_once_with(
            ["/home/test/.local/bin/agy"], title="AICoder · Google Antigravity Login", wait=False
        )
        self.assertTrue(result["authenticated"])



if __name__ == "__main__":
    unittest.main()
