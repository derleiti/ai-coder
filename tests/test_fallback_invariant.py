from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aicoder import session_state, settings


class RemovedFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "state.json"
        session_state._cache = None
        self.patch = patch.object(session_state, "STATE_FILE", self.state_file)
        self.patch.start()

    def tearDown(self):
        session_state._cache = None
        self.patch.stop()
        self.tmp.cleanup()

    def test_fallback_is_not_a_setting_anymore(self):
        self.assertNotIn("fallback_model", settings.REGISTRY)
        with self.assertRaises(settings.SettingsError):
            settings.resolve_key("fallback")

    def test_legacy_set_fallback_is_a_noop(self):
        session_state.set_model("provider/primary")
        session_state.set_fallback("provider/other")
        state = session_state.get_state()
        self.assertEqual(state["selected_model"], "provider/primary")
        self.assertNotIn("fallback_model", state)


if __name__ == "__main__":
    unittest.main()
