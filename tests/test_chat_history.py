from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aicoder import chat_history


class ChatToolEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "chat_history.db"
        self.patch = patch.object(chat_history, "DB_PATH", self.db)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_tool_evidence_persists_metadata_only(self):
        sid = chat_history.create_session("evidence")
        chat_history.save_tool_event(sid, "file_read", "ok", iteration=3, plan_id="plan-1")
        rows = chat_history.load_tool_events(sid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "file_read")
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(rows[0]["iteration"], 3)
        self.assertEqual(rows[0]["plan_id"], "plan-1")
        self.assertNotIn("arguments", rows[0])
        self.assertNotIn("result", rows[0])

    def test_render_tool_evidence_is_compact_and_output_free(self):
        sid = chat_history.create_session("evidence")
        chat_history.save_tool_event(sid, "test", "error", iteration=7, plan_id="p")
        rendered = chat_history.render_tool_evidence(sid)
        self.assertIn("test · error · step 7", rendered)
        self.assertIn("metadata only", rendered)
        self.assertIn("Re-inspect current workspace state", rendered)

    def test_delete_session_removes_tool_events(self):
        sid = chat_history.create_session("evidence")
        chat_history.save_tool_event(sid, "file_tree", "ok")
        chat_history.delete_session(sid)
        self.assertEqual(chat_history.load_tool_events(sid), [])


    def test_clear_history_removes_all_sessions_messages_and_tool_events(self):
        first = chat_history.create_session("one")
        second = chat_history.create_session("two")
        chat_history.save_message(first, "user", "hello")
        chat_history.save_message(second, "assistant", "world")
        chat_history.save_tool_event(first, "file_read", "ok")
        chat_history.clear_history()
        self.assertEqual(chat_history.list_sessions(), [])
        self.assertEqual(chat_history.load_messages(first), [])
        self.assertEqual(chat_history.load_messages(second), [])
        self.assertEqual(chat_history.load_tool_events(first), [])

    def test_invalid_status_is_ignored(self):
        sid = chat_history.create_session("evidence")
        chat_history.save_tool_event(sid, "file_read", "maybe")
        self.assertEqual(chat_history.load_tool_events(sid), [])

    def test_api_history_preserves_first_user_message(self):
        sid = chat_history.create_session("history")
        chat_history.save_message(sid, "user", "first question")
        chat_history.save_message(sid, "assistant", "first answer")
        chat_history.save_message(sid, "user", "follow up")
        rows = chat_history.get_session_messages_for_api(sid)
        self.assertEqual(rows[0], {"role": "user", "content": "first question"})
        self.assertEqual(rows[-1], {"role": "user", "content": "follow up"})


if __name__ == "__main__":
    unittest.main()
