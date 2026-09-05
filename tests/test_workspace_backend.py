from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aicoder.workspace_backend import (
    DiskWorkspace, RamWorkspace, WorkspaceConflict, WorkspaceError,
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

    def test_nested_new_file_survives_the_complete_commit(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            root = Path(temp)
            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            nested = execution / "src" / "feature" / "generated.py"
            nested.parent.mkdir(parents=True)
            nested.write_text("CREATED = True\n", encoding="utf-8")

            backend.finalize(verified=True)

            committed = root / "src" / "feature" / "generated.py"
            self.assertTrue(committed.is_file())
            self.assertEqual(committed.read_text(encoding="utf-8"), "CREATED = True\n")

    def test_directory_replacing_external_symlink_cannot_escape_workspace(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as ram,
            tempfile.TemporaryDirectory() as outside_temp,
        ):
            root = Path(temp)
            outside = Path(outside_temp)
            (root / "pkg").symlink_to(outside, target_is_directory=True)
            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            (execution / "pkg").unlink()
            (execution / "pkg").mkdir()
            (execution / "pkg" / "generated.py").write_text("SAFE = True\n", encoding="utf-8")

            backend.finalize(verified=True)

            self.assertFalse((root / "pkg").is_symlink())
            self.assertEqual((root / "pkg" / "generated.py").read_text(), "SAFE = True\n")
            self.assertFalse((outside / "generated.py").exists())

    def test_new_symlink_outside_workspace_is_rejected_and_rolled_back(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as ram,
            tempfile.TemporaryDirectory() as outside_temp,
        ):
            root = Path(temp)
            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            (execution / "escape").symlink_to(Path(outside_temp), target_is_directory=True)

            with self.assertRaisesRegex(WorkspaceError, "symlink outside the workspace"):
                backend.finalize(verified=True)

            self.assertFalse((root / "escape").exists())
            self.assertFalse((root / "escape").is_symlink())
            backend.abort()

    def test_failed_atomic_commit_restores_all_files_and_cleans_transaction(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            parent = Path(temp)
            root = parent / "source"
            root.mkdir()
            (root / "existing.txt").write_text("original\n", encoding="utf-8")
            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            (execution / "existing.txt").write_text("changed\n", encoding="utf-8")
            (execution / "new.txt").write_text("new\n", encoding="utf-8")
            original_install = RamWorkspace._atomic_install

            def fail_new_candidate(source, target, *, root=None):
                if Path(source).is_relative_to(execution) and Path(target).name == "new.txt":
                    raise OSError("simulated atomic install failure")
                return original_install(source, target, root=root)

            with patch.object(RamWorkspace, "_atomic_install", side_effect=fail_new_candidate):
                with self.assertRaisesRegex(WorkspaceError, "was rolled back"):
                    backend.finalize(verified=True)

            self.assertEqual((root / "existing.txt").read_text(), "original\n")
            self.assertFalse((root / "new.txt").exists())
            self.assertEqual(list(parent.glob(".aicoder-txn-*")), [])
            backend.abort()

    def test_cancelled_atomic_commit_rolls_back_and_cleans_transaction(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            parent = Path(temp)
            root = parent / "source"
            root.mkdir()
            (root / "a.txt").write_text("original\n", encoding="utf-8")
            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            (execution / "a.txt").write_text("changed\n", encoding="utf-8")
            (execution / "z.txt").write_text("new\n", encoding="utf-8")
            original_install = RamWorkspace._atomic_install

            def cancel_second_install(source, target, *, root=None):
                if Path(source).is_relative_to(execution) and Path(target).name == "z.txt":
                    raise KeyboardInterrupt
                return original_install(source, target, root=root)

            with patch.object(RamWorkspace, "_atomic_install", side_effect=cancel_second_install):
                with self.assertRaises(KeyboardInterrupt):
                    backend.finalize(verified=True)

            self.assertEqual((root / "a.txt").read_text(), "original\n")
            self.assertFalse((root / "z.txt").exists())
            self.assertEqual(list(parent.glob(".aicoder-txn-*")), [])
            backend.abort()

    def test_ram_workspace_excludes_backups_and_transient_caches(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            root = Path(temp)
            (root / "app.py").write_text("ok\n", encoding="utf-8")
            for name in (".backups", ".pytest_cache", ".ruff_cache", "__pycache__"):
                directory = root / name
                directory.mkdir()
                (directory / "heavy.bin").write_bytes(b"x" * 1024)
            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            self.assertTrue((execution / "app.py").exists())
            for name in (".backups", ".pytest_cache", ".ruff_cache", "__pycache__"):
                self.assertFalse((execution / name).exists(), name)
            backend.abort()

    def test_delta_summary_classifies_added_modified_and_deleted_files(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            root = Path(temp)
            (root / "keep.py").write_text("old\n", encoding="utf-8")
            (root / "remove.py").write_text("remove\n", encoding="utf-8")
            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()

            (execution / "keep.py").write_text("new\n", encoding="utf-8")
            (execution / "remove.py").unlink()
            (execution / "pkg").mkdir()
            (execution / "pkg" / "created.py").write_text("created = True\n", encoding="utf-8")

            delta = backend.delta_summary()
            self.assertEqual(delta["added_files"], ["pkg/created.py"])
            self.assertEqual(delta["modified_files"], ["keep.py"])
            self.assertEqual(delta["deleted_files"], ["remove.py"])
            self.assertIn("pkg", delta["added"])
            backend.abort()

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



class TeamWorkspaceBudgetTests(unittest.TestCase):
    def test_team_plan_accounts_for_all_candidates_plus_integration(self):
        from aicoder.workspace_backend import team_workspace_plan
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data.bin").write_bytes(b"x" * 1024)
            with (
                patch("aicoder.workspace_backend._mem_available_bytes", return_value=16 * 1024**3),
                patch("aicoder.workspace_backend._select_ram_root", return_value=(root, 16 * 1024**3)),
            ):
                plan = team_workspace_plan(root, 4, "auto")
            self.assertEqual(plan.candidate_count, 4)
            self.assertEqual(plan.total_candidate_bytes, plan.per_workspace_bytes * 5)
            self.assertEqual(plan.backend_mode, "ram")

    def test_low_ram_falls_back_for_whole_team_not_individual_candidates(self):
        from aicoder.workspace_backend import team_workspace_plan
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data.bin").write_bytes(b"x" * 1024)
            with (
                patch("aicoder.workspace_backend._mem_available_bytes", return_value=700 * 1024**2),
                patch("aicoder.workspace_backend._select_ram_root", return_value=(root, 700 * 1024**2)),
            ):
                plan = team_workspace_plan(root, 4, "auto")
            self.assertEqual(plan.backend_mode, "disk-isolated")
            self.assertTrue(plan.reason)

if __name__ == "__main__":
    unittest.main()
