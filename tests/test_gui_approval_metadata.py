import unittest
from unittest.mock import MagicMock

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


if __name__ == "__main__":
    unittest.main()
