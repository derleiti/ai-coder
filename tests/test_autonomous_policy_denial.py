from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aicoder.executor import run_tool
from aicoder.team_orchestrator import _candidate_approval


class AutonomousPolicyDenialTests(unittest.TestCase):
    def test_candidate_policy_denial_is_not_reported_as_user_abort(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            result, is_error = run_tool(
                MagicMock(),
                "test",
                {"command": "python -m pytest tests/ -v --tb=short 2>&1 | head -200", "cwd": str(workspace)},
                approval_fn=_candidate_approval,
                allowed_tools={"test"},
                workspace_root=workspace,
            )

        self.assertTrue(is_error)
        self.assertEqual(result, "test: blocked by autonomous policy")
        self.assertNotIn("aborted by user", result)

    def test_explicit_approval_rejection_keeps_user_abort_semantics(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            result, is_error = run_tool(
                MagicMock(),
                "test",
                {"command": "python -m pytest tests/ -v", "cwd": str(workspace)},
                approval_fn=lambda _name, _args: False,
                allowed_tools={"test"},
                workspace_root=workspace,
            )

        self.assertTrue(is_error)
        self.assertEqual(result, "test: aborted by user")


if __name__ == "__main__":
    unittest.main()
