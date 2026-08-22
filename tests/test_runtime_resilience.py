from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_journal import ContinuationJournalStore
from aicoder.agent_plan import PlanStore
from aicoder.agent_runtime import NativeLightRuntime
from aicoder.client import ClientError, TriForceClient


class RuntimeResilienceTests(unittest.TestCase):
    def test_transient_model_failure_pauses_and_persists_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            config = Path(temp) / "config"
            store = PlanStore(config / "plans")
            journal_store = ContinuationJournalStore(config / "journals")
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = ClientError("HTTP 503: Service Unavailable")
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Inspect and debug failing tests",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                journal_store=journal_store,
                persistent_plan=True,
            )
            result = runtime.run()
            self.assertEqual(result.status, "paused")
            self.assertIn("Transient model/backend failure", result.response)
            plan = store.load(str(workspace), result.plan_id)
            self.assertIsNotNone(plan)
            self.assertEqual(plan.status, "paused")
            journal = journal_store.load(str(workspace), result.plan_id)
            self.assertIsNotNone(journal)
            self.assertIn("Inspect and debug failing tests", journal.pending_input)

    def test_permanent_client_error_still_fails(self):
        client = MagicMock()
        client.timeout = 30
        client.chat.side_effect = ClientError("HTTP 401: Unauthorized")
        runtime = NativeLightRuntime(
            client=client,
            initial_prompt="Inspect workspace",
            model="test/model",
            fallback_model=None,
            workspace_root=".",
            tools=[],
            load_tools_on_start=False,
            persistent_plan=False,
        )
        result = runtime.run()
        self.assertEqual(result.status, "failed")
        self.assertIn("HTTP 401", result.error)

    def test_client_retries_429_once_before_returning_success(self):
        client = TriForceClient("http://example.invalid", timeout=1)
        with patch.object(
            client,
            "_do_request",
            side_effect=[ClientError("HTTP 429: Too Many Requests"), {"ok": True}],
        ) as request, patch("aicoder.client.time.sleep"):
            result = client._request("GET", "/x", _retries=1)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(request.call_count, 2)

    def test_client_does_not_retry_401(self):
        client = TriForceClient("http://example.invalid", timeout=1)
        with patch.object(
            client,
            "_do_request",
            side_effect=ClientError("HTTP 401: Unauthorized"),
        ) as request, patch("aicoder.client.time.sleep"):
            with self.assertRaises(ClientError):
                client._request("GET", "/x", _retries=1)
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
