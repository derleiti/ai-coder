from __future__ import annotations

import argparse
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aicoder import cli


class UnifiedCliRuntimeTests(unittest.TestCase):
    @patch("aicoder.agent.run_agent", return_value=0)
    @patch("aicoder.cli.get_state", return_value={"selected_model": "mistral/mistral-vibe-cli-with-tools"})
    def test_ask_uses_shared_agent_runtime(self, _state, run_agent):
        args = argparse.Namespace(
            prompt=["inspect", "repo"], timeout=30, model=None,
            no_agents=True, temperature=0.1, max_tokens=32,
        )
        self.assertEqual(cli.cmd_ask(args), 0)
        kwargs = run_agent.call_args.kwargs
        self.assertEqual(kwargs["initial_prompt"], "inspect repo")
        self.assertEqual(kwargs["model"], "mistral/mistral-vibe-cli-with-tools")
        self.assertEqual(kwargs["runtime_mode"], "classic")
        self.assertEqual(kwargs["team_overrides"], {"team_runtime_mode": "off"})
        self.assertFalse(kwargs["include_agents"])
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertEqual(kwargs["max_output_tokens"], 32)
        self.assertEqual(kwargs["request_timeout"], 30)
        self.assertEqual(kwargs["history_kind"], "ask")

    @patch("aicoder.agent.run_agent")
    @patch("aicoder.cli.get_state", return_value={"selected_model": "mistral/mistral-vibe-cli-with-tools"})
    def test_chat_uses_shared_runtime_and_preserves_conversation(self, _state, run_agent):
        def fake_run_agent(**kwargs):
            callback = kwargs.get("result_callback")
            if callback:
                callback(SimpleNamespace(response="assistant reply"))
            return 0

        run_agent.side_effect = fake_run_agent
        args = argparse.Namespace(model=None, no_agents=False)
        with patch("builtins.input", side_effect=["inspect pyproject", "/exit"]):
            self.assertEqual(cli.cmd_chat(args), 0)
        kwargs = run_agent.call_args_list[0].kwargs
        self.assertEqual(kwargs["conversation"], [])
        self.assertEqual(kwargs["runtime_mode"], "classic")
        self.assertEqual(kwargs["history_kind"], "chat")
        self.assertTrue(kwargs["include_agents"])

    @patch("aicoder.agent.run_agent", return_value=0)
    @patch("aicoder.cli.get_state", return_value={"selected_model": "mistral/mistral-vibe-cli-with-tools"})
    def test_task_read_only_without_apply_uses_shared_runtime(self, _state, run_agent):
        args = argparse.Namespace(
            task=["analyze", "routing"], files=["aicoder/cli.py"],
            model=None, apply=False, dry_run=False, no_agents=False,
            temperature=0.3, timeout=45,
        )
        self.assertEqual(cli.cmd_task(args), 0)
        kwargs = run_agent.call_args.kwargs
        self.assertTrue(kwargs["read_only"])
        self.assertEqual(kwargs["history_kind"], "task")
        self.assertIn("aicoder/cli.py", kwargs["initial_prompt"])

    @patch("aicoder.agent.run_agent", return_value=0)
    @patch("aicoder.cli.get_state", return_value={"selected_model": "mistral/mistral-vibe-cli-with-tools"})
    def test_task_apply_enables_runtime_mutation_path(self, _state, run_agent):
        args = argparse.Namespace(
            task=["fix", "routing"], files=["aicoder/cli.py"],
            model=None, apply=True, dry_run=False, no_agents=False,
            temperature=0.3, timeout=45,
        )
        self.assertEqual(cli.cmd_task(args), 0)
        self.assertFalse(run_agent.call_args.kwargs["read_only"])

    @patch("aicoder.agent.run_agent", return_value=0)
    @patch("aicoder.cli.get_state", return_value={"selected_model": "mistral/mistral-vibe-cli-with-tools"})
    def test_review_is_shared_runtime_and_read_only(self, _state, run_agent):
        args = argparse.Namespace(files=["aicoder/cli.py"], model=None, no_agents=False)
        self.assertEqual(cli.cmd_review(args), 0)
        kwargs = run_agent.call_args.kwargs
        self.assertTrue(kwargs["read_only"])
        self.assertEqual(kwargs["history_kind"], "review")
        self.assertIn("aicoder/cli.py", kwargs["initial_prompt"])


class CredentialCliTests(unittest.TestCase):
    @patch("aicoder.provider_credentials.set_provider_key")
    @patch("getpass.getpass", return_value="secret")
    def test_credentials_set_uses_keyring_store(self, _getpass, store):
        args = argparse.Namespace(credentials_action="set", provider="openrouter")
        self.assertEqual(cli.cmd_credentials(args), 0)
        store.assert_called_once_with("openrouter", "secret")

    @patch("aicoder.provider_credentials.delete_provider_key", return_value=True)
    def test_credentials_delete_uses_keyring_store(self, delete):
        args = argparse.Namespace(credentials_action="delete", provider="openrouter")
        self.assertEqual(cli.cmd_credentials(args), 0)
        delete.assert_called_once_with("openrouter")
