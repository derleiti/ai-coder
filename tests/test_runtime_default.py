from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aicoder.session_state as state


class RuntimeDefaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_file = self.root / "state.json"
        self.patcher = patch.object(state, "STATE_FILE", self.state_file)
        self.patcher.start()
        state._cache = None
        state._cache_stamp = None

    def tearDown(self):
        state._cache = None
        state._cache_stamp = None
        self.patcher.stop()
        self.temp.cleanup()

    def test_fresh_state_defaults_to_native_light(self):
        self.assertEqual(state.get_state()["runtime_mode"], "native-light")

    def test_old_state_without_runtime_migrates_to_native_light(self):
        self.state_file.write_text(json.dumps({"selected_model": "x"}), encoding="utf-8")
        self.assertEqual(state.get_state()["runtime_mode"], "native-light")

    def test_explicit_classic_is_preserved(self):
        self.state_file.write_text(json.dumps({"runtime_mode": "classic"}), encoding="utf-8")
        self.assertEqual(state.get_state()["runtime_mode"], "classic")


    def test_run_agent_without_runtime_field_uses_native_engine(self):
        from unittest.mock import MagicMock, patch
        import aicoder.agent as agent

        state_without_runtime = {
            "workspace_root": ".",
            "tool_mode": "off",
            "enabled_tools": [],
            "request_timeout": 30,
        }
        with (
            patch.object(agent, "get_state", return_value=state_without_runtime),
            patch.object(agent, "_run_native_light_agent", return_value=0) as native,
        ):
            rc = agent.run_agent("hello", "test/model", None)
        self.assertEqual(rc, 0)
        native.assert_called_once()
        self.assertTrue(native.call_args.kwargs["persistent_plan"])

    def test_explicit_classic_uses_shared_runtime_without_persistent_plan(self):
        import aicoder.agent as agent

        classic_state = {
            "runtime_mode": "classic",
            "workspace_root": ".",
            "tool_mode": "off",
            "enabled_tools": [],
            "request_timeout": 30,
        }
        with (
            patch.object(agent, "get_state", return_value=classic_state),
            patch.object(agent, "_run_native_light_agent", return_value=0) as shared_runtime,
        ):
            rc = agent.run_agent("hello", "test/model", None)
        self.assertEqual(rc, 0)
        shared_runtime.assert_called_once()
        self.assertFalse(shared_runtime.call_args.kwargs["persistent_plan"])

    def test_set_runtime_can_switch_back_and_forth(self):
        state.set_runtime_mode("classic")
        self.assertEqual(state.get_state()["runtime_mode"], "classic")
        state.set_runtime_mode("native-light")
        self.assertEqual(state.get_state()["runtime_mode"], "native-light")


if __name__ == "__main__":
    unittest.main()
