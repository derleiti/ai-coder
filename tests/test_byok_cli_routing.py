from __future__ import annotations

import argparse
import unittest
from unittest.mock import MagicMock, patch

from aicoder import cli


class ByokCliRoutingTests(unittest.TestCase):
    @patch("aicoder.model_transport.native_model_transport_from_env")
    @patch("aicoder.cli.TriForceClient")
    @patch("aicoder.cli.load_session")
    @patch("aicoder.cli.get_state", return_value={"selected_model": "openrouter/test/model", "workspace_root": "."})
    @patch("aicoder.cli.read_agents_md", return_value=None)
    def test_ask_uses_provider_routing_transport(self, _agents, _state, session, client_cls, wrap):
        session.return_value = MagicMock(base_url="https://example.invalid", token="t")
        base = MagicMock(); client_cls.return_value = base
        routed = MagicMock(); routed.chat.return_value = {"response": "ok", "model": "openrouter/test/model"}
        wrap.return_value = (routed, "openrouter/test/model")
        args = argparse.Namespace(prompt=["hello"], timeout=30, model=None, no_agents=True, temperature=0.1, max_tokens=32)
        with patch("aicoder.cli._print_header"), patch("aicoder.cli._print_response"):
            self.assertEqual(cli.cmd_ask(args), 0)
        routed.chat.assert_called_once()
        base.chat.assert_not_called()

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
