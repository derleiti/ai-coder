from __future__ import annotations

import os
import sys
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
                patch("aicoder.workspace.detect_git_root", return_value=None),
            ):
                os.environ.pop(ACTIVE_WORKSPACE_ENV, None)
                rc = cli.cmd_workspace(type("Args", (), {"path": str(root)})())
            self.assertEqual(rc, 0)
            save.assert_called_once_with(str(root.resolve()))
            payload = output.call_args.args[0]
            self.assertEqual(payload["cwd"], str(root.resolve()))
            self.assertFalse(payload["is_git_repo"])


class WorkspaceEscapeTests(unittest.TestCase):
    def test_explicit_scope_root_is_not_overridden_by_process_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            process_root = base / "process"
            explicit_root = base / "explicit"
            process_root.mkdir(); explicit_root.mkdir()
            target = explicit_root / "inside.txt"
            target.write_text("inside", encoding="utf-8")
            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(process_root)}):
                from aicoder.workspace import path_within_workspace
                resolved, inside = path_within_workspace("inside.txt", explicit_root)
            self.assertTrue(inside)
            self.assertEqual(resolved, target.resolve())

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

    def test_transactional_runtime_hard_blocks_original_source_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            execution = base / "ram"
            source.mkdir(); execution.mkdir()
            target = source / "protected.txt"
            target.write_text("original", encoding="utf-8")
            approvals = []

            result, is_error = run_tool(
                MagicMock(), "file_edit",
                {"path": str(target), "operation": "replace", "old": "original", "new": "changed"},
                approval_fn=lambda name, args: approvals.append((name, dict(args))) or True,
                allowed_tools={"file_edit"},
                workspace_root=execution,
                protected_workspace_root=source,
            )
            self.assertTrue(is_error)
            self.assertIn("source workspace is protected", result)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertEqual(approvals, [], "protected source must be blocked before approval")

    def test_headless_never_silently_escapes_workspace(self):
        args = {
            "path": "/outside/file.txt",
            "_workspace_root": "/workspace",
            "_workspace_escape": "/outside/file.txt",
        }
        with patch("aicoder.agent.get_state", return_value={"approval_mode": "all"}):
            self.assertFalse(_headless_approval("file_read", args))


    def test_local_binary_exec_runs_in_workspace_and_reports_exit_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            approvals = []
            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(root)}):
                result, is_error = run_tool(
                    MagicMock(), "binary_exec",
                    {"program": sys.executable, "arguments": ["-c", "print('local-ok')"], "work_dir": "."},
                    approval_fn=lambda name, args: approvals.append((name, dict(args))) or True,
                    allowed_tools={"binary_exec"},
                )
            self.assertFalse(is_error, result)
            self.assertIn("local-ok", result)
            self.assertIn("exit_code=0", result)
            self.assertEqual(approvals[0][0], "binary_exec")

    def test_local_execution_outside_workspace_requires_scope_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "workspace"
            outside = base / "outside"
            root.mkdir(); outside.mkdir()
            approvals = []
            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(root)}):
                result, is_error = run_tool(
                    MagicMock(), "binary_exec",
                    {"program": sys.executable, "arguments": ["-c", "import os; print(os.getcwd())"], "work_dir": str(outside)},
                    approval_fn=lambda name, args: approvals.append((name, dict(args))) or True,
                    allowed_tools={"binary_exec"},
                )
            self.assertFalse(is_error, result)
            self.assertIn(str(outside.resolve()), result)
            self.assertEqual(approvals[0][1]["_workspace_escape"], str(outside.resolve()))

    def test_binary_file_read_is_rejected_without_dumping_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "module.so"
            target.write_bytes(b"\x7fELF\x00secret-binary-data")
            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(root)}):
                result, is_error = run_tool(
                    MagicMock(), "file_read", {"path": "module.so"},
                    approval_fn=lambda *_: True, allowed_tools={"file_read"},
                )
            self.assertTrue(is_error)
            self.assertIn("binary file", result)
            self.assertNotIn("secret-binary-data", result)

    def test_projects_container_is_not_a_valid_team_project(self):
        from aicoder.workspace import validate_project_workspace

        with tempfile.TemporaryDirectory() as temp:
            projects = Path(temp) / "workspace"
            project = projects / "demo"
            project.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "projects container"):
                validate_project_workspace(projects, projects)
            self.assertEqual(validate_project_workspace(project, projects), project.resolve())

    def test_set_workspace_synchronizes_process_workspace(self):
        from aicoder import session_state
        from aicoder.workspace import ACTIVE_WORKSPACE_ENV

        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as config:
            state_file = Path(config) / "state.json"
            previous = os.environ.pop(ACTIVE_WORKSPACE_ENV, None)
            try:
                with patch.object(session_state, "STATE_FILE", state_file):
                    session_state._cache = None
                    session_state._cache_stamp = None
                    session_state.set_workspace(temp)
                    self.assertEqual(session_state.get_state()["workspace_root"], str(Path(temp).resolve()))
                    self.assertEqual(Path(os.environ[ACTIVE_WORKSPACE_ENV]), Path(temp).resolve())
            finally:
                session_state._cache = None
                session_state._cache_stamp = None
                if previous is None:
                    os.environ.pop(ACTIVE_WORKSPACE_ENV, None)
                else:
                    os.environ[ACTIVE_WORKSPACE_ENV] = previous

    def test_gui_startup_honors_persisted_workspace_instead_of_launcher_cwd(self):
        import os
        from aicoder import cli
        from aicoder.workspace import ACTIVE_WORKSPACE_ENV

        with tempfile.TemporaryDirectory() as configured, tempfile.TemporaryDirectory() as launcher:
            previous = os.environ.pop(ACTIVE_WORKSPACE_ENV, None)
            old_cwd = os.getcwd()
            try:
                os.chdir(launcher)
                with patch("aicoder.cli.get_state", return_value={"workspace_root": configured}):
                    root = cli._activate_startup_workspace(["aicoder", "gui"])
                self.assertEqual(root, Path(configured).resolve())
                self.assertEqual(Path(os.environ[ACTIVE_WORKSPACE_ENV]), Path(configured).resolve())
            finally:
                os.chdir(old_cwd)
                if previous is None:
                    os.environ.pop(ACTIVE_WORKSPACE_ENV, None)
                else:
                    os.environ[ACTIVE_WORKSPACE_ENV] = previous

    def test_cli_startup_keeps_explicit_launch_cwd(self):
        import os
        from aicoder import cli
        from aicoder.workspace import ACTIVE_WORKSPACE_ENV

        with tempfile.TemporaryDirectory() as launcher:
            previous = os.environ.pop(ACTIVE_WORKSPACE_ENV, None)
            old_cwd = os.getcwd()
            try:
                os.chdir(launcher)
                root = cli._activate_startup_workspace(["aicoder", "agent"])
                self.assertEqual(root, Path(launcher).resolve())
            finally:
                os.chdir(old_cwd)
                if previous is None:
                    os.environ.pop(ACTIVE_WORKSPACE_ENV, None)
                else:
                    os.environ[ACTIVE_WORKSPACE_ENV] = previous


    def test_symlink_inside_workspace_pointing_outside_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            workspace = base / "workspace"
            outside = base / "outside"
            workspace.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
            link = workspace / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not available on this platform")
            from aicoder.workspace import path_within_workspace
            resolved, allowed = path_within_workspace("escape/secret.txt", root=workspace)
            self.assertFalse(allowed)
            self.assertEqual(resolved, (outside / "secret.txt").resolve())



class LocalCodeToolRoutingTests(unittest.TestCase):
    def test_code_tools_execute_locally_and_never_call_mcp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nested = root / "pkg"
            nested.mkdir()
            (root / "main.py").write_text("from pkg.mod import VALUE\n", encoding="utf-8")
            (nested / "mod.py").write_text("VALUE = 42\nneedle = 'local-code-search'\n", encoding="utf-8")
            client = MagicMock()
            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(root)}):
                read, read_error = run_tool(
                    client, "code_read", {"path": "pkg/mod.py", "start_line": 1, "end_line": 1},
                    approval_fn=lambda *_: True, allowed_tools={"code_read"},
                )
                tree, tree_error = run_tool(
                    client, "code_tree", {"path": ".", "depth": 3},
                    approval_fn=lambda *_: True, allowed_tools={"code_tree"},
                )
                search, search_error = run_tool(
                    client, "code_search", {"query": "local-code-search", "path": ".", "file_pattern": "*.py"},
                    approval_fn=lambda *_: True, allowed_tools={"code_search"},
                )
            self.assertFalse(read_error, read)
            self.assertIn("1: VALUE = 42", read)
            self.assertFalse(tree_error, tree)
            self.assertIn("mod.py", tree)
            self.assertFalse(search_error, search)
            self.assertIn("pkg/mod.py:2", search)
            client.mcp_call.assert_not_called()

    def test_code_tools_support_explicit_project_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("print('root-ok')\n", encoding="utf-8")
            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(root)}):
                result, is_error = run_tool(
                    MagicMock(), "code_read", {"root": str(project), "path": "app.py"},
                    approval_fn=lambda *_: True, allowed_tools={"code_read"},
                )
            self.assertFalse(is_error, result)
            self.assertIn("root-ok", result)

    def test_code_tool_invalid_target_is_reported_without_runtime_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(root)}):
                result, is_error = run_tool(
                    MagicMock(), "code_search", {"query": "needle", "target": "mars"},
                    approval_fn=lambda *_: True, allowed_tools={"code_search"},
                )
            self.assertTrue(is_error)
            self.assertIn("remote code targets are disabled", result)

    def test_code_tool_project_outside_workspace_needs_scope_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            workspace = base / "workspace"
            project = base / "project"
            workspace.mkdir(); project.mkdir()
            (project / "app.py").write_text("outside = True\n", encoding="utf-8")
            approvals = []
            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(workspace)}):
                result, is_error = run_tool(
                    MagicMock(), "code_read", {"root": str(project), "path": "app.py"},
                    approval_fn=lambda name, args: approvals.append((name, dict(args))) or True,
                    allowed_tools={"code_read"},
                )
            self.assertFalse(is_error, result)
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0][1]["_workspace_escape"], str((project / "app.py").resolve()))


class RuntimeWorkspaceOverrideTests(unittest.TestCase):
    def test_run_tool_uses_explicit_runtime_workspace_for_relative_write(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            process_root = base / "process"
            runtime_root = base / "runtime"
            process_root.mkdir(); runtime_root.mkdir()
            with patch.dict(os.environ, {ACTIVE_WORKSPACE_ENV: str(process_root)}):
                result, is_error = run_tool(
                    MagicMock(), "file_edit",
                    {"path": "probe.txt", "operation": "create", "content": "ok"},
                    approval_fn=lambda *_: True,
                    allowed_tools={"file_edit"},
                    workspace_root=runtime_root,
                )
            self.assertFalse(is_error, result)
            self.assertEqual((runtime_root / "probe.txt").read_text(encoding="utf-8"), "ok")
            self.assertFalse((process_root / "probe.txt").exists())



if __name__ == "__main__":
    unittest.main()
