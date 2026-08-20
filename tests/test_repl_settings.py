from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from aicoder import settings, setup
from aicoder.repl_input import COMMANDS
from aicoder.settings import SettingsStore


class ReplSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.store = SettingsStore(lambda: self.path)
        self.store.save(dict(settings.DEFAULTS))
        self.store_patch = patch.object(setup.settings_core, "STORE", self.store)
        self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()
        self.temp.cleanup()

    def run_cmd(self, value: str):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = setup._repl_settings_command(value)
        return rc, output.getvalue()

    def test_completion_exposes_settings(self):
        self.assertIn("/settings", COMMANDS)

    def test_set_get_reset_share_registry_coercion(self):
        rc, _ = self.run_cmd("set timeout 180")
        self.assertEqual(rc, 0)
        self.assertEqual(self.store.get("request_timeout"), 180)
        rc, out = self.run_cmd("get timeout")
        self.assertEqual(rc, 0)
        self.assertIn("request_timeout = 180", out)
        rc, _ = self.run_cmd("reset timeout")
        self.assertEqual(rc, 0)
        self.assertEqual(self.store.get("request_timeout"), settings.REGISTRY["request_timeout"].default)

    def test_list_contains_complete_registry(self):
        rc, out = self.run_cmd("list")
        self.assertEqual(rc, 0)
        for key in settings.REGISTRY:
            self.assertIn(key, out)

    def test_invalid_setting_does_not_write(self):
        before = self.path.read_text(encoding="utf-8")
        rc, out = self.run_cmd("set tool_mode nonsense")
        self.assertEqual(rc, 2)
        self.assertIn("Fehler", out)
        self.assertEqual(before, self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
