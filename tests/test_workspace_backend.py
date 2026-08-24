from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aicoder.workspace_backend import (
    DiskWorkspace, RamWorkspace, WorkspaceConflict,
    create_workspace_backend, open_workspace_for_run,
)


class DiskWorkspaceTests(unittest.TestCase):
    def test_disk_backend_preserves_existing_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            backend = DiskWorkspace(root)
            self.assertEqual(backend.prepare(), root)
            self.assertEqual(backend.info.mode, "disk")
            self.assertFalse(backend.info.volatile)
            self.assertFalse(backend.info.transactional)
            backend.finalize(verified=True)
            backend.abort()

    def test_disk_backend_rejects_missing_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing"
            with self.assertRaises(ValueError):
                DiskWorkspace(missing)


class RamWorkspaceTests(unittest.TestCase):
    def test_ram_workspace_is_isolated_until_verified_finalize(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            root = Path(temp)
            (root / "app.py").write_text("old\n", encoding="utf-8")
            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            (execution / "app.py").write_text("new\n", encoding="utf-8")
            (execution / "created.txt").write_text("created\n", encoding="utf-8")

            self.assertEqual((root / "app.py").read_text(), "old\n")
            self.assertFalse((root / "created.txt").exists())

            backend.finalize(verified=True)
            self.assertEqual((root / "app.py").read_text(), "new\n")
            self.assertEqual((root / "created.txt").read_text(), "created\n")
            self.assertFalse(execution.exists())

    def test_ram_workspace_deletion_is_committed(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            root = Path(temp)
            (root / "remove.txt").write_text("x", encoding="utf-8")
            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            (execution / "remove.txt").unlink()
            backend.finalize(verified=True)
            self.assertFalse((root / "remove.txt").exists())

    def test_external_change_blocks_commit(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            root = Path(temp)
            target = root / "app.py"
            target.write_text("base", encoding="utf-8")
            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            (execution / "app.py").write_text("agent", encoding="utf-8")
            target.write_text("human", encoding="utf-8")
            with self.assertRaises(WorkspaceConflict):
                backend.finalize(verified=True)
            self.assertEqual(target.read_text(), "human")
            backend.abort()

    def test_checkpoint_restores_only_ram_delta(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            root = Path(temp)
            (root / "a.txt").write_text("a", encoding="utf-8")
            first = RamWorkspace(root, ram_root=ram)
            execution = first.prepare()
            (execution / "a.txt").write_text("changed", encoding="utf-8")
            (execution / "b.txt").write_text("new", encoding="utf-8")
            checkpoint = first.checkpoint("plan-1")
            self.assertIsNotNone(checkpoint)
            first.abort()
            self.assertEqual((root / "a.txt").read_text(), "a")

            second = RamWorkspace(root, ram_root=ram, checkpoint_id="plan-1")
            restored = second.prepare()
            self.assertTrue(second.info.restored_checkpoint)
            self.assertEqual((restored / "a.txt").read_text(), "changed")
            self.assertEqual((restored / "b.txt").read_text(), "new")
            second.clear_checkpoint("plan-1")
            second.abort()

    def test_git_metadata_is_private_in_ram(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            root = Path(temp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "x.txt").write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "x.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
            source_head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            git_entry = execution / ".git"
            self.assertTrue(git_entry.is_dir(), "RAM clone must own private git metadata")
            subprocess.run(["git", "-C", str(execution), "config", "user.email", "ram@example.com"], check=True)
            subprocess.run(["git", "-C", str(execution), "config", "user.name", "RAM"], check=True)
            (execution / "x.txt").write_text("ram", encoding="utf-8")
            subprocess.run(["git", "-C", str(execution), "add", "x.txt"], check=True)
            subprocess.run(["git", "-C", str(execution), "commit", "-qm", "ram-only"], check=True)

            self.assertEqual(subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(), source_head)
            backend.abort()

    def test_auto_falls_back_to_disk_when_safe_budget_is_too_small(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "big.bin").write_bytes(b"x" * 1024)
            with (
                patch("aicoder.workspace_backend._select_ram_root", return_value=(Path(temp), 1024)),
                patch("aicoder.workspace_backend._mem_available_bytes", return_value=1024),
            ):
                backend = create_workspace_backend(root, "auto")
            self.assertEqual(backend.info.mode, "disk")
            self.assertEqual(backend.info.requested_mode, "auto")
            self.assertTrue(backend.info.fallback_reason)


if __name__ == "__main__":
    unittest.main()
