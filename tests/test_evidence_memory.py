from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aicoder.evidence_memory import ProjectEvidenceStore


class ProjectEvidenceStoreTests(unittest.TestCase):
    def test_file_record_is_hot_cached_and_detects_unchanged_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "main.py"
            target.write_text("print('one')\n", encoding="utf-8")
            db = root / "evidence.db"
            store = ProjectEvidenceStore(str(root), db)
            first, unchanged_first = store.inspect_file("main.py", 1, 20)
            second, unchanged_second = store.inspect_file("main.py", 1, 20)
            self.assertFalse(unchanged_first)
            self.assertTrue(unchanged_second)
            self.assertEqual(first.content_hash, second.content_hash)
            self.assertEqual(len(store.recent_files()), 1)

    def test_changed_file_gets_new_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "main.py"
            target.write_text("x = 1\n", encoding="utf-8")
            store = ProjectEvidenceStore(str(root), root / "evidence.db")
            first, _ = store.inspect_file("main.py")
            target.write_text("x = 22\n", encoding="utf-8")
            os.utime(target, None)
            second, unchanged = store.inspect_file("main.py")
            self.assertFalse(unchanged)
            self.assertNotEqual(first.content_hash, second.content_hash)

    def test_file_metadata_survives_new_store_instance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "main.py"
            target.write_text("x = 1\n", encoding="utf-8")
            db = root / "evidence.db"
            ProjectEvidenceStore(str(root), db).inspect_file("main.py", 3, 9)
            restored = ProjectEvidenceStore(str(root), db)
            item, unchanged = restored.inspect_file("main.py", 3, 9)
            self.assertTrue(unchanged)
            self.assertEqual(item.start_line, 3)
            self.assertEqual(item.end_line, 9)

    def test_health_reports_hot_and_persisted_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "main.py"
            target.write_text("x = 1\n", encoding="utf-8")
            store = ProjectEvidenceStore(str(root), root / "evidence.db")
            store.inspect_file("main.py")
            health = store.health()
            self.assertEqual(health["status"], "ready")
            self.assertEqual(health["hot_files"], 1)
            self.assertEqual(health["persisted_files"], 1)

    def test_failure_persistence_keeps_only_hash_not_raw_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "evidence.db"
            secretish = "environment:ImportError token=do-not-store-this"
            store = ProjectEvidenceStore(str(root), db)
            store.remember_failure("environment", secretish, 2)
            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT category,signature_hash,occurrence_count FROM failure_evidence"
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], "environment")
            self.assertEqual(row[2], 2)
            self.assertNotIn("do-not-store-this", row[1])
            self.assertNotIn(b"do-not-store-this", db.read_bytes())


if __name__ == "__main__":
    unittest.main()

class RuntimeEvidenceRecallTests(unittest.TestCase):
    def test_runtime_skips_duplicate_unchanged_file_read_in_same_run(self):
        from unittest.mock import MagicMock, patch
        from aicoder.agent_runtime import NativeLightRuntime
        from aicoder.executor import LOCAL_FILE_READ_SCHEMA

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "main.py"
            target.write_text("important = 42\n", encoding="utf-8")
            evidence = ProjectEvidenceStore(str(root), root / "evidence.db")
            client = MagicMock()
            client.timeout = 30
            client.list_models.return_value = [
                {"id": "test/model", "capabilities": ["chat", "function_calling"]}
            ]
            client.chat.side_effect = [
                {"response": '<tool_call>{"name":"file_read","arguments":{"path":"main.py"}}</tool_call>', "model": "test/model"},
                {"response": '<tool_call>{"name":"file_read","arguments":{"path":"main.py"}}</tool_call>', "model": "test/model"},
                {"response": "DONE: recalled", "model": "test/model"},
            ]
            runtime = NativeLightRuntime(
                client=client, initial_prompt="Inspect main.py and explain it",
                model="test/model", fallback_model=None, workspace_root=str(root),
                tools=[LOCAL_FILE_READ_SCHEMA], persistent_plan=False, base_timeout=30,
            )
            with (
                patch("aicoder.agent_runtime.ProjectEvidenceStore", return_value=evidence),
                patch("aicoder.agent_runtime.run_tool", return_value=("important = 42", False)) as run_tool,
            ):
                result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(run_tool.call_count, 1)
            self.assertTrue(any(
                "Duplicate tool call blocked before execution" in str(message.get("content", ""))
                for message in result.messages
            ))


class EvidenceForcedRecheckTests(unittest.TestCase):
    def test_force_hash_detects_same_size_content_with_restored_mtime(self):
        import os
        import tempfile
        from pathlib import Path
        from aicoder.evidence_memory import ProjectEvidenceStore
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "probe.txt"
            target.write_text("alpha", encoding="utf-8")
            store = ProjectEvidenceStore(str(root), db_path=root / "evidence.db")
            first, _ = store.inspect_file(str(target))
            stat = target.stat()
            target.write_text("bravo", encoding="utf-8")
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            _, fast_unchanged = store.inspect_file(str(target))
            self.assertTrue(fast_unchanged)
            forced, forced_unchanged = store.inspect_file(str(target), force_hash=True)
            self.assertFalse(forced_unchanged)
            self.assertNotEqual(forced.content_hash, first.content_hash)
