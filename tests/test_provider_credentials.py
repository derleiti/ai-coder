from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from aicoder.provider_credentials import (
    SERVICE_NAME,
    canonical_provider,
    credential_summary,
    delete_provider_key,
    provider_api_key,
    provider_for_model,
    set_provider_key,
    transport_model_id,
)


class ProviderCredentialTests(unittest.TestCase):
    def _backend(self):
        backend = MagicMock()
        backend.priority = 1
        return backend

    @patch("aicoder.provider_credentials.keyring")
    def test_secret_is_kept_in_keyring_and_never_returned_by_summary(self, kr):
        kr.get_keyring.return_value = self._backend()
        kr.get_password.return_value = "SUPER-SECRET"
        set_provider_key("gemini", "SUPER-SECRET")
        kr.set_password.assert_called_once_with(SERVICE_NAME, "google", "SUPER-SECRET")
        summary = credential_summary("google", environ={})
        self.assertTrue(summary["configured"])
        self.assertEqual(summary["source"], "keyring")
        self.assertNotIn("SUPER-SECRET", repr(summary))
        self.assertFalse(summary["credential_value_exposed"])

    @patch("aicoder.provider_credentials.keyring")
    def test_keyring_takes_precedence_over_environment(self, kr):
        kr.get_keyring.return_value = self._backend()
        kr.get_password.return_value = "stored-secret"
        secret, source = provider_api_key("google", environ={"GOOGLE_API_KEY": "env-secret"})
        self.assertEqual(secret, "stored-secret")
        self.assertEqual(source, "keyring")

    @patch("aicoder.provider_credentials.keyring")
    def test_environment_remains_supported_when_no_stored_key(self, kr):
        kr.get_keyring.return_value = self._backend()
        kr.get_password.return_value = None
        secret, source = provider_api_key("openrouter", environ={"OPENROUTER_API_KEY": "env-secret"})
        self.assertEqual(secret, "env-secret")
        self.assertEqual(source, "environment:OPENROUTER_API_KEY")

    @patch("aicoder.provider_credentials.keyring")
    def test_delete_uses_canonical_provider_without_exposing_secret(self, kr):
        kr.get_keyring.return_value = self._backend()
        kr.get_password.return_value = "secret"
        self.assertTrue(delete_provider_key("gemini"))
        kr.delete_password.assert_called_once_with(SERVICE_NAME, "google")

    def test_provider_alias_and_transport_model_resolution(self):
        self.assertEqual(canonical_provider("gemini"), "google")
        self.assertEqual(provider_for_model("gemini/gemini-2.5-flash"), "google")
        self.assertEqual(transport_model_id("gemini/gemini-2.5-flash", "google"), "gemini-2.5-flash")
        self.assertEqual(transport_model_id("openrouter/nvidia/nemotron:free", "openrouter"), "nvidia/nemotron:free")


if __name__ == "__main__":
    unittest.main()

class ProviderCredentialDirectSupportTests(unittest.TestCase):
    def test_anthropic_is_direct_supported(self):
        from aicoder.provider_credentials import credential_summary, direct_provider_spec
        spec = direct_provider_spec("anthropic")
        self.assertIsNotNone(spec)
        self.assertTrue(spec.direct_supported)
        self.assertEqual(spec.base_url, "https://api.anthropic.com/v1")
        with patch("aicoder.provider_credentials.get_stored_provider_key", return_value="secret"):
            summary = credential_summary("anthropic", environ={})
        self.assertTrue(summary["direct_supported"])
        self.assertNotIn("secret", repr(summary))
