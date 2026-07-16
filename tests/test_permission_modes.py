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

    def test_sudo_only_approves_root_but_not_normal_writes_or_deletes(self):
        write = assess_execution("file_edit", {"command": "touch note.txt"})
        sudo = assess_execution("local_exec", {
            "command": "apt update", "sudo": True, "reason": "refresh packages",
        })
        delete = assess_execution("local_exec", {
            "command": "rm /etc/example", "sudo": True, "reason": "remove config",
        })
        self.assertFalse(approval_is_automatic("sudo_only", write))
        self.assertTrue(approval_is_automatic("sudo_only", sudo))
        self.assertFalse(approval_is_automatic("sudo_only", delete))

    def test_all_approves_every_classified_mutation(self):
        for risk in (
            assess_execution("file_edit", {"command": "touch note.txt"}),
            assess_execution("local_exec", {"command": "rm note.txt"}),
            assess_execution("local_exec", {
                "command": "apt update", "sudo": True, "reason": "refresh packages",
            }),
        ):
            self.assertTrue(approval_is_automatic("all", risk))


class GuiSudoApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_gui_root_request_uses_local_terminal_authentication(self):
        widget = ChatWidget.__new__(ChatWidget)
        widget._worker = MagicMock()
        widget._append_msg = MagicMock()
        args = {
            "command": "apt update",
            "sudo": True,
            "reason": "refresh package metadata",
        }
        with (
            patch("aicoder.gui.chat_widget.get_state", return_value={"approval_mode": "sudo_only"}),
            patch("aicoder.gui.chat_widget.validate_sudo_session_gui", return_value=(True, "ok")) as auth,
            patch.object(QMessageBox, "question") as question,
        ):
            ChatWidget._on_approval_needed(widget, "local_exec", args)
        auth.assert_called_once_with()
        question.assert_not_called()
        widget._worker.set_approval.assert_called_once_with(True)

    def test_gui_rejects_root_request_without_reason_before_auth(self):
        widget = ChatWidget.__new__(ChatWidget)
        widget._worker = MagicMock()
        widget._append_msg = MagicMock()
        with (
            patch("aicoder.gui.chat_widget.get_state", return_value={"approval_mode": "all"}),
            patch("aicoder.gui.chat_widget.validate_sudo_session_gui") as auth,
            patch.object(QMessageBox, "warning"),
        ):
            ChatWidget._on_approval_needed(widget, "local_exec", {
                "command": "apt update", "sudo": True,
            })
        auth.assert_not_called()
        widget._worker.set_approval.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
