from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication

from aicoder.gui.chat_widget import ChatWidget
from aicoder.gui import settings_widget


class ChatModelRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_chat_page_has_no_model_or_fallback_selectors(self):
        widget = ChatWidget(settings_ref=None)
        self.addCleanup(widget.close)
        self.assertFalse(hasattr(widget, "model_combo"))
        self.assertFalse(hasattr(widget, "fallback_combo"))

    def test_settings_page_owns_base_and_team_model_selection(self):
        with (
            patch.object(settings_widget, "load_session", side_effect=RuntimeError("offline")),
            patch.object(settings_widget, "get_state", return_value={}),
        ):
            widget = settings_widget.SettingsWidget()
        self.addCleanup(widget.close)
        self.assertTrue(hasattr(widget, "model_combo"))
        self.assertEqual(len(widget._team_model_combos), 12)
        self.assertFalse(hasattr(widget, "fallback_combo"))


if __name__ == "__main__":
    unittest.main()
