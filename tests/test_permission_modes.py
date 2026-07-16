from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from aicoder.gui.chat_widget import ChatWidget
import aicoder.executor as executor
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
            patch.object(QMessageBox, "question") as question,
        ):
            ChatWidget._on_approval_needed(widget, "local_exec", args)
        question.assert_not_called()
        widget._worker.set_approval.assert_called_once_with(True)

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


class GuiRootExecutionTests(unittest.TestCase):
    def test_graphical_root_command_uses_pkexec(self):
        completed = MagicMock(returncode=0, stdout="ok\n", stderr="")
        with (
            patch.object(executor.sys.stdin, "isatty", return_value=False),
            patch.dict(os.environ, {"DISPLAY": ":0"}, clear=False),
            patch.object(executor.shutil, "which", return_value="/usr/bin/pkexec"),
            patch.object(executor.subprocess, "run", return_value=completed) as run,
        ):
            result, is_error = executor.run_local_exec({
                "command": "apt update", "sudo": True,
            })
        self.assertFalse(is_error)
        self.assertEqual(result, "ok\n")
        self.assertEqual(run.call_args.args[0], ["pkexec", "apt", "update"])

    def test_graphical_root_redirect_runs_in_pkexec_shell(self):
        completed = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch.object(executor.sys.stdin, "isatty", return_value=False),
            patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=False),
            patch.object(executor.shutil, "which", return_value="/usr/bin/pkexec"),
            patch.object(executor.subprocess, "run", return_value=completed) as run,
        ):
            result, is_error = executor.run_local_exec({
                "command": "printf enabled > /etc/aicoder.conf", "sudo": True,
            })
        self.assertFalse(is_error)
        self.assertEqual(result, "(no output)")
        self.assertEqual(
            run.call_args.args[0],
            ["pkexec", "sh", "-c", "printf enabled > /etc/aicoder.conf"],
        )

    def test_terminal_root_command_keeps_sudo(self):
        completed = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch.object(executor.sys.stdin, "isatty", return_value=True),
            patch.object(executor.subprocess, "run", return_value=completed) as run,
        ):
            executor.run_local_exec({"command": "id", "sudo": True})
        self.assertEqual(run.call_args.args[0], ["sudo", "--", "id"])


if __name__ == "__main__":
    unittest.main()
