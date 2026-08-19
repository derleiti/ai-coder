from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder import cli
from aicoder.agent import _cli_approval, _headless_approval
from aicoder.executor import run_tool
from aicoder.workspace import ACTIVE_WORKSPACE_ENV, activate_workspace, active_workspace


class ActiveWorkspaceTests(unittest.TestCase):
    def test_launch_workspace_overrides_persisted_workspace_for_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            persisted = root / "persisted"
            launched = root / "launched"
            persisted.mkdir()
            launched.mkdir()
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(ACTIVE_WORKSPACE_ENV, None)
                self.assertEqual(active_workspace(str(persisted)), persisted.resolve())
                activate_workspace(launched)
                self.assertEqual(active_workspace(str(persisted)), launched.resolve())

    def test_workspace_command_accepts_non_git_directory_and_persists_exact_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "photos"
            root.mkdir()
            with (
                patch.dict(os.environ, {}, clear=False),
                patch.object(cli, "set_workspace") as save,
                patch.object(cli, "print_json") as output,
            ):
                os.environ.pop(ACTIVE_WORKSPACE_ENV, None)
                rc = cli.cmd_workspace(type("Args", (), {"path": str(root)})())
            self.assertEqual(rc, 0)
            save.assert_called_once_with(str(root.resolve()))
            payload = output.call_args.args[0]
            self.assertEqual(payload["cwd"], str(root.resolve()))
            self.assertFalse(payload["is_git_repo"])


class WorkspaceEscapeTests(unittest.TestCase):
    def test_inside_workspace_read_needs_no_scope_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "inside.txt"
            target.write_text("inside", encoding="utf-8")
            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(root)}):
                result, is_error = run_tool(
                    MagicMock(), "file_read", {"path": "inside.txt"},
                    approval_fn=lambda *_: self.fail("inside read must not ask for scope approval"),
                    allowed_tools={"file_read"},
                )
            self.assertFalse(is_error)
            self.assertEqual(result, "inside")

    def test_directory_create_inside_workspace_uses_normal_write_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            approvals = []

            def approve(name, args):
                approvals.append((name, dict(args)))
                return True

            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(root)}):
                result, is_error = run_tool(
                    MagicMock(), "directory_create", {"path": "pac-man"},
                    approval_fn=approve,
                    allowed_tools={"directory_create"},
                )
            self.assertFalse(is_error, result)
            self.assertTrue((root / "pac-man").is_dir())
            self.assertEqual(len(approvals), 1)
            self.assertNotIn("_workspace_escape", approvals[0][1])

    def test_directory_create_outside_workspace_requires_scope_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "workspace"
            root.mkdir()
            outside = base / "pac-man"
            approvals = []

            def approve(name, args):
                approvals.append((name, dict(args)))
                return True

            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(root)}):
                result, is_error = run_tool(
                    MagicMock(), "directory_create", {"path": str(outside)},
                    approval_fn=approve,
                    allowed_tools={"directory_create"},
                )
            self.assertFalse(is_error, result)
            self.assertTrue(outside.is_dir())
            self.assertEqual(approvals[0][1]["_workspace_escape"], str(outside.resolve()))

    def test_outside_read_requires_explicit_approval_then_runs_once(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "workspace"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            target = outside / "note.txt"
            target.write_text("outside-data", encoding="utf-8")
            approvals = []

            def approve(name, args):
                approvals.append((name, dict(args)))
                return True

            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(root)}):
                result, is_error = run_tool(
                    MagicMock(), "file_read", {"path": str(target)},
                    approval_fn=approve,
                    allowed_tools={"file_read"},
                )
            self.assertFalse(is_error)
            self.assertEqual(result, "outside-data")
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0][1]["_workspace_root"], str(root.resolve()))
            self.assertEqual(approvals[0][1]["_workspace_escape"], str(target.resolve()))

    def test_outside_read_is_blocked_without_local_approval_broker(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "workspace"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            target = outside / "note.txt"
            target.write_text("outside-data", encoding="utf-8")
            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(root)}):
                result, is_error = run_tool(
                    MagicMock(), "file_read", {"path": str(target)},
                    approval_fn=None,
                    allowed_tools={"file_read"},
                )
            self.assertTrue(is_error)
            self.assertIn("workspace escape requires explicit approval", result)

    def test_autopilot_does_not_auto_approve_scope_escape(self):
        args = {
            "path": "/outside/file.txt",
            "_workspace_root": "/workspace",
            "_workspace_escape": "/outside/file.txt",
        }
        with (
            patch("aicoder.agent.get_state", return_value={"approval_mode": "all"}),
            patch("builtins.input", return_value="n") as prompt,
        ):
            self.assertFalse(_cli_approval("file_read", args))
        prompt.assert_called_once()

    def test_headless_never_silently_escapes_workspace(self):
        args = {
            "path": "/outside/file.txt",
            "_workspace_root": "/workspace",
            "_workspace_escape": "/outside/file.txt",
        }
        with patch("aicoder.agent.get_state", return_value={"approval_mode": "all"}):
            self.assertFalse(_headless_approval("file_read", args))


if __name__ == "__main__":
    unittest.main()
