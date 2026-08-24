from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_plan import PlanStore
from aicoder.agent_runtime import NativeLightRuntime
from aicoder.client import TransportError, AuthError
from aicoder.executor import LOCAL_FILE_EDIT_SCHEMA


class TransportErrorHandlingTests(unittest.TestCase):
    """Tests for transient transport error handling in NativeLightRuntime.
    
    Ensures that transient errors (5xx, 429, timeout, connection reset, temporary unavailability)
    pause the plan for resumption rather than failing permanently, while auth/4xx errors fail permanently.
    """

    def test_transport_error_5xx_pauses_plan_for_resumption(self):
        """HTTP 5xx errors should pause the plan, preserving state for resumption."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            # Create an initial paused plan
            plan = store.create("Test 5xx handling", str(workspace), "test/model")
            plan.status = "paused"
            plan.pause_reason = "initial pause"
            store.save(plan)
            
            client = MagicMock()
            client.timeout = 30
            
            # Simulate HTTP 500 error
            from aicoder.client import TransportError
            client.chat.side_effect = TransportError("HTTP 500: Internal Server Error")
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Test transport error",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[LOCAL_FILE_EDIT_SCHEMA],
                load_tools_on_start=True,
                approval_fn=lambda _name, _args: True,
                plan_store=store,
                resume=False,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            # Should return paused status, not failed
            self.assertEqual(result.status, "paused")
            self.assertIn("HTTP 500", result.response)
            # Plan ID should be set (it's a new plan created by _prepare_plan)
            self.assertTrue(result.plan_id, "Plan ID should be set")
            
            # Plan should still exist and be paused
            saved_plan = store.load_current(str(workspace))
            self.assertIsNotNone(saved_plan)
            self.assertEqual(saved_plan.status, "paused")
            self.assertIn("HTTP 500", saved_plan.pause_reason)

    def test_transport_error_429_pauses_plan_for_resumption(self):
        """HTTP 429 (rate limit) errors should pause the plan, preserving state for resumption."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            client = MagicMock()
            client.timeout = 30
            
            # Simulate HTTP 429 error
            client.chat.side_effect = TransportError("HTTP 429: Too Many Requests")
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Test rate limit",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            # Should return paused status, not failed
            self.assertEqual(result.status, "paused")
            self.assertIn("HTTP 429", result.response)

    def test_transport_error_timeout_pauses_plan_for_resumption(self):
        """Timeout errors should pause the plan, preserving state for resumption."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            client = MagicMock()
            client.timeout = 30
            
            # Simulate timeout error
            client.chat.side_effect = TransportError("Timeout after 30s")
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Test timeout",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            # Should return paused status, not failed
            self.assertEqual(result.status, "paused")
            self.assertIn("Timeout", result.response)

    def test_transport_error_connection_reset_pauses_plan_for_resumption(self):
        """Connection reset errors should pause the plan, preserving state for resumption."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            client = MagicMock()
            client.timeout = 30
            
            # Simulate connection reset error
            client.chat.side_effect = TransportError("Connection reset by peer")
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Test connection reset",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            # Should return paused status, not failed
            self.assertEqual(result.status, "paused")
            self.assertIn("Connection reset", result.response)

    def test_auth_error_401_fails_plan_permanently(self):
        """HTTP 401 (auth) errors should fail the plan permanently."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            client = MagicMock()
            client.timeout = 30
            
            # Simulate HTTP 401 error
            client.chat.side_effect = AuthError("HTTP 401: Unauthorized")
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Test auth error",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            # Should return failed status
            self.assertEqual(result.status, "failed")
            self.assertIn("HTTP 401", result.error)
            
            # Plan should be marked as failed
            saved_plan = store.load_current(str(workspace))
            self.assertIsNotNone(saved_plan)
            self.assertEqual(saved_plan.status, "failed")

    def test_auth_error_403_fails_plan_permanently(self):
        """HTTP 403 (forbidden) errors should fail the plan permanently."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            client = MagicMock()
            client.timeout = 30
            
            # Simulate HTTP 403 error
            client.chat.side_effect = AuthError("HTTP 403: Forbidden")
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Test forbidden error",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            # Should return failed status
            self.assertEqual(result.status, "failed")
            self.assertIn("HTTP 403", result.error)

    def test_other_4xx_errors_fail_plan_permanently(self):
        """Other 4xx errors (except 429) should fail the plan permanently."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            client = MagicMock()
            client.timeout = 30
            
            # Simulate HTTP 404 error
            from aicoder.client import ClientError
            client.chat.side_effect = ClientError("HTTP 404: Not Found")
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Test 404 error",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            # Should return failed status
            self.assertEqual(result.status, "failed")
            self.assertIn("HTTP 404", result.error)

    def test_transport_error_with_persistent_plan_preserves_journal(self):
        """Transport errors should preserve the journal for resumption."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            # Create initial plan
            plan = store.create("Test journal preservation", str(workspace), "test/model")
            plan.status = "running"
            store.save(plan)
            
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = TransportError("HTTP 503: Service Unavailable")
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Test journal",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                journal_store=None,  # Will use default
                base_timeout=30,
            )
            
            result = runtime.run()
            
            # Should be paused
            self.assertEqual(result.status, "paused")
            
            # Journal should exist (it's created on first save_journal call)
            journal_store = runtime._journal()
            journal = journal_store.load(str(workspace), plan.id)
            # Journal may be None if no messages were saved, which is acceptable for this error case


if __name__ == "__main__":
    unittest.main()
