from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QListWidgetItem
from PyQt6.QtCore import Qt

from aicoder import cli, session_state
from aicoder.gui import settings_widget


class SharedSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "state.json"
        self.old_state_file = session_state.STATE_FILE
        session_state.STATE_FILE = self.state_file
        session_state._cache = None
        session_state._cache_stamp = None
        session_state._save_raw(dict(session_state._DEFAULTS))

    def tearDown(self):
        session_state.STATE_FILE = self.old_state_file
        session_state._cache = None
        session_state._cache_stamp = None
        self.tmp.cleanup()

    def test_registry_covers_every_persisted_runtime_setting(self):
        self.assertEqual(set(session_state.SETTINGS), set(session_state._DEFAULTS))

    def test_cli_can_set_every_setting_type(self):
        cases = {
            "selected_model": "provider/model-a",
            "fallback_model": "provider/model-b",
            "swarm_mode": "review",
            "workspace_root": "/tmp/workspace-a",
            "tool_mode": "always",
            "enabled_tools": "git,file_read",
            "request_timeout": "180",
            "approval_mode": "autopilot",
            "runtime_mode": "classic",
        }
        for key, raw in cases.items():
            with self.subTest(key=key):
                out = io.StringIO()
                err = io.StringIO()
                args = argparse.Namespace(settings_action="set", key=key, value=raw)
                with redirect_stdout(out), redirect_stderr(err):
                    rc = cli.cmd_settings(args)
                self.assertEqual(rc, 0, err.getvalue())
        state = session_state.get_state()
        self.assertEqual(state["selected_model"], "provider/model-a")
        self.assertEqual(state["fallback_model"], "provider/model-b")
        self.assertEqual(state["swarm_mode"], "review")
        self.assertEqual(state["workspace_root"], "/tmp/workspace-a")
        self.assertEqual(state["tool_mode"], "always")
        self.assertEqual(state["enabled_tools"], ["file_read", "git"])
        self.assertEqual(state["request_timeout"], 180)
        self.assertEqual(state["approval_mode"], "autopilot")
        self.assertEqual(state["runtime_mode"], "classic")

    def test_cli_all_and_none_tool_syntax_is_unambiguous(self):
        session_state.set_setting("enabled_tools", "none")
        self.assertEqual(session_state.get_state()["enabled_tools"], [])
        session_state.set_setting("enabled_tools", "all")
        self.assertIsNone(session_state.get_state()["enabled_tools"])

    def test_gui_write_is_immediately_visible_to_terminal_store(self):
        with patch.object(settings_widget, "load_session", side_effect=RuntimeError("offline")):
            widget = settings_widget.SettingsWidget()
        self.addCleanup(widget.close)

        widget.model_combo.setCurrentText("provider/gui-model")
        widget.fallback_combo.setCurrentText("provider/gui-fallback")
        widget.swarm_combo.setCurrentText("review")
        widget.timeout_spin.setValue(180)
        widget._save_model_config()

        widget.approval_mode_combo.setCurrentIndex(widget.approval_mode_combo.findData("autopilot"))
        widget._save_permission_config()

        widget.runtime_combo.setCurrentIndex(widget.runtime_combo.findData("classic"))
        widget.workspace_edit.setText("/tmp/gui-workspace")
        widget._save_runtime_config()

        widget.tool_mode_combo.setCurrentIndex(widget.tool_mode_combo.findData("always"))
        widget._tools = [{"name": "git"}, {"name": "file_read"}]
        widget.tool_list.clear()
        for name, checked in (("git", True), ("file_read", False)):
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            widget.tool_list.addItem(item)
        widget._save_tool_config()

        state = session_state.get_state()
        self.assertEqual(state["selected_model"], "provider/gui-model")
        self.assertEqual(state["fallback_model"], "provider/gui-fallback")
        self.assertEqual(state["swarm_mode"], "review")
        self.assertEqual(state["request_timeout"], 180)
        self.assertEqual(state["approval_mode"], "autopilot")
        self.assertEqual(state["runtime_mode"], "classic")
        self.assertEqual(state["workspace_root"], "/tmp/gui-workspace")
        self.assertEqual(state["tool_mode"], "always")
        self.assertEqual(state["enabled_tools"], ["git"])

    def test_terminal_change_refreshes_running_gui(self):
        with patch.object(settings_widget, "load_session", side_effect=RuntimeError("offline")):
            widget = settings_widget.SettingsWidget()
        self.addCleanup(widget.close)

        session_state.set_setting("selected_model", "provider/cli-model")
        session_state.set_setting("fallback_model", "provider/cli-fallback")
        session_state.set_setting("swarm_mode", "on")
        session_state.set_setting("request_timeout", 240)
        session_state.set_setting("approval_mode", "autopilot")
        session_state.set_setting("runtime_mode", "classic")
        session_state.set_setting("workspace_root", "/tmp/cli-workspace")
        session_state.set_setting("tool_mode", "always")
        session_state.set_setting("enabled_tools", ["git"])

        widget.tool_list.clear()
        for name in ("git", "file_read"):
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            widget.tool_list.addItem(item)

        widget._refresh_external_state()

        self.assertEqual(widget.model_combo.currentText(), "provider/cli-model")
        self.assertEqual(widget.fallback_combo.currentText(), "provider/cli-fallback")
        self.assertEqual(widget.swarm_combo.currentText(), "on")
        self.assertEqual(widget.timeout_spin.value(), 240)
        self.assertEqual(widget.approval_mode_combo.currentData(), "autopilot")
        self.assertEqual(widget.runtime_combo.currentData(), "classic")
        self.assertEqual(widget.workspace_edit.text(), "/tmp/cli-workspace")
        self.assertEqual(widget.tool_mode_combo.currentData(), "always")
        self.assertEqual(widget.tool_list.item(0).checkState(), Qt.CheckState.Checked)
        self.assertEqual(widget.tool_list.item(1).checkState(), Qt.CheckState.Unchecked)


if __name__ == "__main__":
    unittest.main()


class SettingsTabExperienceTests(SharedSettingsTests):
    def test_apply_all_persists_every_runtime_setting_in_one_action(self):
        with patch.object(settings_widget, "load_session", side_effect=RuntimeError("offline")):
            widget = settings_widget.SettingsWidget()
        self.addCleanup(widget.close)

        widget.model_combo.setCurrentText("provider/apply-model")
        widget.fallback_combo.setCurrentText("provider/apply-fallback")
        widget.swarm_combo.setCurrentText("review")
        widget.timeout_spin.setValue(210)
        widget.approval_mode_combo.setCurrentIndex(widget.approval_mode_combo.findData("autopilot"))
        widget.runtime_combo.setCurrentIndex(widget.runtime_combo.findData("classic"))
        widget.workspace_edit.setText("/tmp/apply-workspace")
        widget.tool_mode_combo.setCurrentIndex(widget.tool_mode_combo.findData("always"))
        widget.tool_list.clear()
        for name, checked in (("git", True), ("file_read", False)):
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            widget.tool_list.addItem(item)

        widget._apply_all_settings()
        state = session_state.get_state()
        self.assertEqual(state["selected_model"], "provider/apply-model")
        self.assertEqual(state["fallback_model"], "provider/apply-fallback")
        self.assertEqual(state["swarm_mode"], "review")
        self.assertEqual(state["request_timeout"], 210)
        self.assertEqual(state["approval_mode"], "autopilot")
        self.assertEqual(state["runtime_mode"], "classic")
        self.assertEqual(state["workspace_root"], "/tmp/apply-workspace")
        self.assertEqual(state["tool_mode"], "always")
        self.assertEqual(state["enabled_tools"], ["git"])
        self.assertIn("no restart required", widget.apply_status.text())

    def test_unsaved_combo_changes_do_not_change_persisted_state(self):
        with patch.object(settings_widget, "load_session", side_effect=RuntimeError("offline")):
            widget = settings_widget.SettingsWidget()
        self.addCleanup(widget.close)
        before = session_state.get_state()
        widget.approval_mode_combo.setCurrentIndex(widget.approval_mode_combo.findData("all"))
        widget.tool_mode_combo.setCurrentIndex(widget.tool_mode_combo.findData("always"))
        after = session_state.get_state()
        self.assertEqual(after["approval_mode"], before["approval_mode"])
        self.assertEqual(after["tool_mode"], before["tool_mode"])

    def test_chat_page_has_no_model_or_fallback_selector(self):
        from aicoder.gui.chat_widget import ChatWidget
        with patch.object(settings_widget, "load_session", side_effect=RuntimeError("offline")):
            settings = settings_widget.SettingsWidget()
        self.addCleanup(settings.close)
        chat = ChatWidget(settings_ref=settings)
        self.addCleanup(chat.close)
        self.assertFalse(hasattr(chat, "model_combo"))
        self.assertFalse(hasattr(chat, "fallback_combo"))


class SettingsStoreHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "state.json"
        self.old_state_file = session_state.STATE_FILE
        session_state.STATE_FILE = self.state_file
        session_state._cache = None
        session_state._cache_stamp = None

    def tearDown(self):
        session_state.STATE_FILE = self.old_state_file
        session_state._cache = None
        session_state._cache_stamp = None
        self.tmp.cleanup()

    def test_store_writes_revision_schema_and_private_mode(self):
        session_state.set_setting("request_timeout", 180)
        raw = __import__("json").loads(self.state_file.read_text())
        self.assertEqual(raw["_schema_version"], session_state.STATE_SCHEMA_VERSION)
        self.assertGreaterEqual(raw["_revision"], 1)
        self.assertEqual(self.state_file.stat().st_mode & 0o777, 0o600)

    def test_corrupt_state_is_preserved_once_and_defaults_are_returned(self):
        self.state_file.write_text("{broken", encoding="utf-8")
        first = session_state.get_state()
        session_state._cache = None
        session_state._cache_stamp = None
        second = session_state.get_state()
        backups = list(self.state_file.parent.glob("state.json.corrupt-*"))
        self.assertEqual(first, second)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "{broken")

    def test_security_downgrade_requires_explicit_confirmation(self):
        session_state.set_setting("approval_mode", "ask")
        plan = session_state.plan_settings_patch({"approval_mode": "all"})
        self.assertTrue(plan["requires_confirmation"])
        with self.assertRaises(PermissionError):
            session_state.apply_settings_patch({"approval_mode": "all"})
        applied = session_state.apply_settings_patch({"approval_mode": "all"}, security_confirmed=True)
        self.assertTrue(applied["verified"])
        self.assertEqual(session_state.get_state()["approval_mode"], "all")

    def test_safe_patch_applies_without_security_confirmation(self):
        applied = session_state.apply_settings_patch({"request_timeout": 180})
        self.assertTrue(applied["verified"])
        self.assertEqual(session_state.get_state()["request_timeout"], 180)

    def test_plugin_cli_surface_is_exposed(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["plugin", "list"]).plugin_action, "list")
        self.assertEqual(parser.parse_args(["plugin", "info", "settings"]).plugin_id, "settings")
        self.assertEqual(parser.parse_args(["plugin", "enable", "settings"]).plugin_action, "enable")
        self.assertEqual(parser.parse_args(["plugin", "disable", "settings"]).plugin_action, "disable")
        self.assertEqual(parser.parse_args(["plugin", "doctor", "settings"]).plugin_action, "doctor")
        self.assertEqual(parser.parse_args(["plugin", "paths"]).plugin_action, "paths")

    def test_cli_schema_doctor_and_reset_all_are_exposed(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["settings", "schema", "--json"]).settings_action, "schema")
        self.assertEqual(parser.parse_args(["settings", "doctor"]).settings_action, "doctor")
        reset = parser.parse_args(["settings", "reset", "--all"])
        self.assertTrue(reset.reset_all)


class LlmSettingsToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "state.json"
        self.old_state_file = session_state.STATE_FILE
        session_state.STATE_FILE = self.state_file
        session_state._cache = None
        session_state._cache_stamp = None
        session_state.set_settings({"approval_mode": "ask", "request_timeout": 300})

    def tearDown(self):
        session_state.STATE_FILE = self.old_state_file
        session_state._cache = None
        session_state._cache_stamp = None
        self.tmp.cleanup()

    def test_settings_tools_are_local_and_discoverable(self):
        from aicoder.plugins import discover_plugins
        registry = discover_plugins(Path(self.tmp.name))
        names = {tool["name"] for tool in registry.tool_schemas()}
        self.assertTrue({"settings_list", "settings_describe", "settings_get", "settings_plan_patch", "settings_apply_patch", "settings_reset"}.issubset(names))
        self.assertIsNotNone(registry.provider_for_tool("settings_apply_patch"))

    def test_safe_settings_patch_uses_normal_mutation_approval(self):
        from aicoder import executor
        approval = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(return_value=True)
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(executor.audit, "log_tool"):
            result, is_error = executor.run_tool(
                __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(),
                "settings_apply_patch",
                {"changes": {"request_timeout": 180}, "reason": "user asked"},
                approval_fn=approval,
            )
        self.assertFalse(is_error, result)
        self.assertEqual(session_state.get_state()["request_timeout"], 180)
        self.assertFalse(approval.call_args.args[1].get("_security_boundary", False))

    def test_security_settings_patch_cannot_be_auto_approved_by_mode(self):
        from aicoder import executor
        session_state.set_setting("approval_mode", "all")
        approval_args = []
        def reject_security(name, args):
            approval_args.append(args)
            return False
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(executor.audit, "log_tool"):
            result, is_error = executor.run_tool(
                __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(),
                "settings_apply_patch",
                {"changes": {"workspace_root": "/tmp/other"}, "reason": "move boundary"},
                approval_fn=reject_security,
            )
        self.assertTrue(is_error)
        self.assertTrue(approval_args[0]["_security_boundary"])
        self.assertNotEqual(session_state.get_state()["workspace_root"], "/tmp/other")
