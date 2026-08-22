import unittest
from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QApplication

from aicoder.gui.chat_widget import ChatWidget, _AgentWorker


class GuiApprovalMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_worker_emits_complete_security_enriched_arguments(self):
        worker = _AgentWorker(
            MagicMock(), [], "test", "", [], "",
            load_tools_on_start=False,
        )
        received = []

        def approve(tool_name, arguments):
            received.append((tool_name, arguments))
            worker.set_approval(True)

        worker.approval_needed.connect(approve)
        arguments = {
            "path": "/tmp/example",
            "content": "safe",
            "_mutating": True,
            "_destructive": False,
        }

        self.assertTrue(worker._gui_approval("code_edit", arguments))
        self.assertEqual(received, [("code_edit", arguments)])

    def test_approval_preview_redacts_secrets_and_internal_metadata(self):
        preview = ChatWidget._approval_preview({
            "path": "/tmp/example",
            "token": "top-secret",
            "nested": {"api_key": "also-secret"},
            "_mutating": True,
        })

        self.assertIn("/tmp/example", preview)
        self.assertNotIn("top-secret", preview)
        self.assertNotIn("also-secret", preview)
        self.assertNotIn("_mutating", preview)
        self.assertGreaterEqual(preview.count("<redacted>"), 2)


    def test_approval_replies_to_signal_sender_not_mutable_current_worker(self):
        requester = _AgentWorker(MagicMock(), [], "test", "", [], "", load_tools_on_start=False)
        current = _AgentWorker(MagicMock(), [], "test", "", [], "", load_tools_on_start=False)
        requester.set_approval = MagicMock()
        current.set_approval = MagicMock()
        widget = MagicMock()
        widget.sender.return_value = requester
        widget._worker = current
        widget._approval_preview.return_value = "preview"
        with patch("aicoder.gui.chat_widget.get_state", return_value={"approval_mode": "autopilot"}):
            ChatWidget._on_approval_needed(widget, "file_edit", {"path": "x", "_mutating": True})
        requester.set_approval.assert_called_once_with(True, "")
        current.set_approval.assert_not_called()

    def test_duplicate_send_is_ignored_while_worker_is_running(self):
        widget = MagicMock()
        widget._worker.isRunning.return_value = True
        ChatWidget._send(widget)
        widget._append_msg.assert_called_once_with(
            "system", "Agent already running; duplicate send ignored.", ""
        )
        widget.input.toPlainText.assert_not_called()



if __name__ == "__main__":
    unittest.main()
