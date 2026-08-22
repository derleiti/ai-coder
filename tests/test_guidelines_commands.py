from __future__ import annotations

import os
import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aicoder.cli import cmd_command
from aicoder.commands import discover_commands, expand_command, read_command
from aicoder.executor import build_system_prompt
from aicoder.guidelines import load_guidelines, render_guidelines


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class GuidelineTests(unittest.TestCase):
    def test_guidelines_load_low_to_high_precedence_and_render_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            config = root / "config"
            workspace.mkdir()
            _write(config / "GUIDELINES.md", "GLOBAL GUIDE")
            _write(workspace / ".agents" / "GUIDELINES.md", "AGENTS GUIDE")
            _write(workspace / ".aicoder" / "GUIDELINES.md", "AICODER GUIDE")

            rows = load_guidelines(workspace, config_dir=config)
            self.assertEqual([scope for scope, _ in rows], [
                "global", "workspace-agents", "workspace-aicoder",
            ])
            rendered = render_guidelines(workspace, config_dir=config)
            self.assertLessEqual(len(rendered), 12000)
            self.assertLess(rendered.index("GLOBAL GUIDE"), rendered.index("AICODER GUIDE"))

    def test_guideline_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            outside = _write(root / "secret.txt", "DO_NOT_LOAD")
            target = workspace / ".aicoder" / "GUIDELINES.md"
            target.parent.mkdir(parents=True)
            try:
                target.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks unavailable")
            self.assertEqual(load_guidelines(workspace), [])

    def test_system_prompt_includes_guidelines_and_agents_with_agents_later(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            _write(workspace / ".aicoder" / "GUIDELINES.md", "GUIDE_MARKER")
            _write(workspace / "AGENTS.md", "AGENTS_MARKER")
            prompt = build_system_prompt([], str(workspace))
            self.assertIn("GUIDE_MARKER", prompt)
            self.assertIn("AGENTS_MARKER", prompt)
            self.assertLess(prompt.index("GUIDE_MARKER"), prompt.index("AGENTS_MARKER"))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._saved_active_workspace = os.environ.pop("AICODER_ACTIVE_WORKSPACE", None)

    def tearDown(self):
        if self._saved_active_workspace is not None:
            os.environ["AICODER_ACTIVE_WORKSPACE"] = self._saved_active_workspace
        else:
            os.environ.pop("AICODER_ACTIVE_WORKSPACE", None)

    def test_workspace_command_precedence_and_expansion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            config = root / "config"
            workspace.mkdir()
            _write(config / "commands" / "check.md", "global $ARGUMENTS")
            _write(workspace / ".agents" / "commands" / "check.md", "agents $ARGUMENTS")
            _write(
                workspace / ".aicoder" / "commands" / "check.md",
                "---\ndescription: Native check\n---\nnative $ARGUMENTS",
            )
            catalog = discover_commands(workspace, config_dir=config)
            self.assertEqual(len(catalog), 1)
            self.assertEqual(catalog[0].scope, "workspace-aicoder")
            self.assertEqual(catalog[0].description, "Native check")
            expanded, is_error = expand_command(workspace, "check", "src tests", config_dir=config)
            self.assertFalse(is_error)
            self.assertEqual(expanded, "native src tests")

    def test_command_without_placeholder_appends_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            _write(workspace / ".aicoder" / "commands" / "review.md", "Review current changes.")
            expanded, is_error = expand_command(workspace, "review", "focus security")
            self.assertFalse(is_error)
            self.assertIn("Review current changes.", expanded)
            self.assertIn("Arguments:\nfocus security", expanded)

    def test_command_rejects_path_lookup_and_symlink_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            text, is_error = read_command(workspace, "../../secret")
            self.assertTrue(is_error)
            self.assertIn("invalid command name", text)

            outside = root / "outside"
            _write(outside / "leak.md", "DO_NOT_LOAD")
            commands_root = workspace / ".aicoder" / "commands"
            commands_root.parent.mkdir(parents=True)
            try:
                commands_root.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            self.assertEqual(discover_commands(workspace), [])

    def test_cli_command_dispatches_expanded_prompt_to_native_light(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            _write(workspace / ".aicoder" / "commands" / "fix.md", "Fix $ARGUMENTS and verify.")
            args = argparse.Namespace(name="fix", arguments=["parser", "tests"], model=None, verbose=False)
            state = {
                "workspace_root": str(workspace),
                "selected_model": "test/model",
                "fallback_model": "fallback/model",
            }
            with (
                patch("aicoder.cli.get_state", return_value=state),
                patch("aicoder.agent.run_agent", return_value=0) as run_agent,
            ):
                rc = cmd_command(args)
            self.assertEqual(rc, 0)
            self.assertEqual(run_agent.call_args.kwargs["initial_prompt"], "Fix parser tests and verify.")
            self.assertEqual(run_agent.call_args.kwargs["runtime_mode"], "native-light")
            self.assertEqual(run_agent.call_args.kwargs["model"], "test/model")


if __name__ == "__main__":
    unittest.main()
