from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aicoder.session_state as session_state
from aicoder.cli import build_parser, cmd_tool_mode
from aicoder.executor import is_tool_relevant_message, should_load_tools


class ToolDemandPolicyTests(unittest.TestCase):
    def test_general_chat_and_concept_questions_skip_tools(self):
        for prompt in (
            "Hallo",
            "Was ist Dependency Injection?",
            "Erkläre mir den Unterschied zwischen einer Klasse und Funktion.",
            "Why is immutability useful?",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(is_tool_relevant_message(prompt))
                self.assertFalse(should_load_tools("on_demand", prompt))

    def test_workspace_and_action_prompts_load_tools(self):
        for prompt in (
            "Welche Dateien sind hier im Projekt?",
            "Prüfe den Parser und finde den Fehler.",
            "Warum schlagen die Tests in diesem Repo fehl?",
            "Schau in ./aicoder/agent.py nach.",
            "I have a traceback in this project",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(is_tool_relevant_message(prompt))
                self.assertTrue(should_load_tools("on_demand", prompt))

    def test_modes_override_heuristic_and_resume_forces_on_demand(self):
        self.assertFalse(should_load_tools("off", "Prüfe dieses Repo"))
        self.assertTrue(should_load_tools("always", "Hallo"))
        self.assertTrue(should_load_tools("on_demand", "continue", resume=True))


class ToolModeCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp.name) / "state.json"
        self.patch_state = patch.object(session_state, "STATE_FILE", self.state_file)
        self.patch_state.start()
        session_state._cache = None
        session_state._cache_stamp = None

    def tearDown(self):
        session_state._cache = None
        session_state._cache_stamp = None
        self.patch_state.stop()
        self.temp.cleanup()

    def test_cli_tool_mode_always_persists(self):
        with patch("aicoder.cli.set_tool_mode", side_effect=session_state.set_tool_mode):
            rc = cmd_tool_mode(argparse.Namespace(value="always"))
        self.assertEqual(rc, 0)
        self.assertEqual(session_state.get_state()["tool_mode"], "always")

    def test_parser_exposes_tool_mode(self):
        args = build_parser().parse_args(["tool-mode", "always"])
        self.assertEqual(args.command, "tool-mode")
        self.assertEqual(args.value, "always")


if __name__ == "__main__":
    unittest.main()
