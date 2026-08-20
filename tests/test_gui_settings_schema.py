from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from aicoder import settings
from aicoder.gui import settings_widget
from aicoder.settings import SettingsStore


class GuiSettingsSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.store = SettingsStore(lambda: self.path)
        self.store.save(dict(settings.DEFAULTS))
        self.patches = [
            patch.object(settings_widget, "load_session", side_effect=RuntimeError("offline")),
            patch.object(settings_widget.settings_core, "STORE", self.store),
            patch.object(settings_widget, "get_state", side_effect=lambda: self.store.load()),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def make_widget(self):
        widget = settings_widget.SettingsWidget()
        self.addCleanup(widget.close)
        return widget

    def test_enum_choices_and_timeout_bounds_come_from_registry(self):
        widget = self.make_widget()
        self.assertEqual(
            [widget.swarm_combo.itemText(i) for i in range(widget.swarm_combo.count())],
            settings.REGISTRY["swarm_mode"].choice_list(),
        )
        self.assertEqual(
            [widget.tool_mode_combo.itemData(i) for i in range(widget.tool_mode_combo.count())],
            settings.REGISTRY["tool_mode"].choice_list(),
        )
        self.assertEqual(
            [widget.approval_mode_combo.itemData(i) for i in range(widget.approval_mode_combo.count())],
            settings.REGISTRY["approval_mode"].choice_list(),
        )
        self.assertEqual(widget.timeout_spin.minimum(), settings.REGISTRY["request_timeout"].minimum)
        self.assertEqual(widget.timeout_spin.maximum(), settings.REGISTRY["request_timeout"].maximum)
        self.assertEqual(widget.timeout_spin.toolTip(), settings.REGISTRY["request_timeout"].description)

    def test_initial_population_does_not_write_via_change_signals(self):
        with (
            patch.object(settings_widget, "set_tool_mode") as set_tool_mode,
            patch.object(settings_widget, "set_approval_mode") as set_approval_mode,
        ):
            widget = self.make_widget()
            self.assertFalse(widget._loading_settings)
        set_tool_mode.assert_not_called()
        set_approval_mode.assert_not_called()

    def test_external_store_change_refreshes_running_widget(self):
        widget = self.make_widget()
        self.store.update(
            selected_model="provider/new-model",
            request_timeout=180,
            tool_mode="always",
            approval_mode="autopilot",
            runtime_mode="classic",
        )
        widget._refresh_external_settings()
        self.assertEqual(widget.model_combo.currentText(), "provider/new-model")
        self.assertEqual(widget.timeout_spin.value(), 180)
        self.assertEqual(widget.tool_mode_combo.currentData(), "always")
        self.assertEqual(widget.approval_mode_combo.currentData(), "autopilot")
        self.assertEqual(widget._schema_widgets["runtime_mode"].currentData(), "classic")

    def test_unhandled_settings_are_schema_generated_and_saved_through_store(self):
        widget = self.make_widget()
        for key in ("runtime_mode", "max_output_tokens", "workspace_root"):
            self.assertIn(key, widget._schema_widgets)
        runtime = widget._schema_widgets["runtime_mode"]
        runtime.setCurrentIndex(runtime.findData("classic"))
        widget._schema_widgets["max_output_tokens"].setValue(8192)
        widget._schema_widgets["workspace_root"].setText("/tmp/example-workspace")
        widget._save_schema_settings()
        state = self.store.load()
        self.assertEqual(state["runtime_mode"], "classic")
        self.assertEqual(state["max_output_tokens"], 8192)
        self.assertEqual(state["workspace_root"], "/tmp/example-workspace")


if __name__ == "__main__":
    unittest.main()
