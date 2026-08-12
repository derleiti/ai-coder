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

    def test_gui_rejects_root_request_even_with_reason(self):
        widget = ChatWidget.__new__(ChatWidget)
        widget._worker = MagicMock()
        widget._append_msg = MagicMock()
        args = {
            "command": "apt update",
            "sudo": True,
            "reason": "refresh package metadata",
        }
        with (
            patch("aicoder.gui.chat_widget.get_state", return_value={"approval_mode": "all"}),
            patch.object(QMessageBox, "warning") as warning,
        ):
            ChatWidget._on_approval_needed(widget, "local_exec", args)
        warning.assert_called_once()
        widget._worker.set_approval.assert_called_once_with(False)

    def test_gui_rejects_root_request_without_reason_before_auth(self):
        widget = ChatWidget.__new__(ChatWidget)
        widget._worker = MagicMock()
        widget._append_msg = MagicMock()
        with (
            patch("aicoder.gui.chat_widget.get_state", return_value={"approval_mode": "all"}),
            patch.object(QMessageBox, "warning"),
        ):
            ChatWidget._on_approval_needed(widget, "local_exec", {
                "command": "apt update", "sudo": True,
            })
        widget._worker.set_approval.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
