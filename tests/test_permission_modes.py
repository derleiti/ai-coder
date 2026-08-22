from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from aicoder.gui.chat_widget import ChatWidget
from aicoder.privileges import approval_is_automatic, assess_execution


class ApprovalModeTests(unittest.TestCase):
    def test_ask_never_auto_approves_mutations(self):
        risk = assess_execution("file_edit", {"command": "touch note.txt"})
        self.assertFalse(approval_is_automatic("ask", risk))

    def test_autopilot_only_approves_non_elevated_non_destructive_writes(self):
        write = assess_execution("file_edit", {"command": "touch note.txt"})
        delete = assess_execution("local_exec", {"command": "rm note.txt"})
        sudo = assess_execution("local_exec", {
            "command": "apt update", "sudo": True, "reason": "refresh packages",
        })
        self.assertTrue(approval_is_automatic("autopilot", write))
        self.assertFalse(approval_is_automatic("autopilot", delete))
        self.assertFalse(approval_is_automatic("autopilot", sudo))

    def test_legacy_sudo_only_mode_approves_nothing(self):
        write = assess_execution("file_edit", {"command": "touch note.txt"})
        sudo = assess_execution("local_exec", {
            "command": "apt update", "sudo": True, "reason": "refresh packages",
        })
        delete = assess_execution("local_exec", {
            "command": "rm /etc/example", "sudo": True, "reason": "remove config",
        })
        self.assertFalse(approval_is_automatic("sudo_only", write))
        self.assertFalse(approval_is_automatic("sudo_only", sudo))
        self.assertFalse(approval_is_automatic("sudo_only", delete))

    def test_all_approves_workspace_writes_but_not_delete_or_root(self):
        write = assess_execution("file_edit", {"command": "touch note.txt"})
        delete = assess_execution("local_exec", {"command": "rm note.txt"})
        self.assertTrue(approval_is_automatic("all", write))
        self.assertFalse(approval_is_automatic("all", delete))
        root = assess_execution("local_exec", {
            "command": "apt update", "sudo": True, "reason": "refresh packages",
        })
        self.assertFalse(approval_is_automatic("all", root))


class GuiSudoApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_gui_root_request_requires_manual_yes_even_in_all_mode(self):
        widget = ChatWidget.__new__(ChatWidget)
        widget._worker = MagicMock()
        widget._append_msg = MagicMock()
        args = {"command": "sudo true", "reason": "privileged diagnostic"}
        with (
            patch("aicoder.gui.chat_widget.get_state", return_value={"approval_mode": "all"}),
            patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as question,
            patch("aicoder.gui.chat_widget.PrivilegeBroker.gui_elevation_available", return_value=(True, "ok")),
        ):
            ChatWidget._on_approval_needed(widget, "shell", args)
        question.assert_called_once()
        widget._worker.set_approval.assert_called_once_with(True, "pkexec")

    def test_gui_root_request_can_be_rejected(self):
        widget = ChatWidget.__new__(ChatWidget)
        widget._worker = MagicMock()
        widget._append_msg = MagicMock()
        with (
            patch("aicoder.gui.chat_widget.get_state", return_value={"approval_mode": "autopilot"}),
            patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question,
        ):
            ChatWidget._on_approval_needed(widget, "shell", {"command": "sudo true"})
        question.assert_called_once()
        widget._worker.set_approval.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
