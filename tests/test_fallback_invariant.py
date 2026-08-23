import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from aicoder import cli, session_state


class FallbackInvariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name)
        self.state_file = self.config_dir / "state.json"
        session_state._cache = None
        self.patches = [
            patch.object(session_state, "CONFIG_DIR", self.config_dir),
            patch.object(session_state, "STATE_FILE", self.state_file),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        session_state._cache = None
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_identical_fallback_is_disabled(self):
        session_state.set_model("provider/model")
        session_state.set_fallback("provider/model")
        self.assertEqual(session_state.get_state()["fallback_model"], "")

    def test_setting_primary_clears_identical_existing_fallback(self):
        session_state.set_fallback("provider/model")
        session_state.set_model("provider/model")
        self.assertEqual(session_state.get_state()["fallback_model"], "")

    def test_distinct_fallback_is_preserved(self):
        session_state.set_model("provider/primary")
        session_state.set_fallback("provider/fallback")
        self.assertEqual(session_state.get_state()["fallback_model"], "provider/fallback")

    def test_cli_reports_disabled_for_identical_fallback(self):
        session_state.set_model("provider/model")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_fallback(argparse.Namespace(value="provider/model"))
        self.assertEqual(rc, 0)
        self.assertIn("disabled (same as operator)", buf.getvalue())
        self.assertEqual(session_state.get_state()["fallback_model"], "")


if __name__ == "__main__":
    unittest.main()
