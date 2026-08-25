from __future__ import annotations

import gc
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
    def test_project_contents_are_materialized_at_candidate_root_not_parent_container(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            container = Path(temp) / "workspace"
            project = container / "aicoder-experimental"
            sibling = container / "other-project"
            project.mkdir(parents=True)
            sibling.mkdir()
            (project / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            (project / "README.md").write_text("project", encoding="utf-8")
            (sibling / "secret.txt").write_text("sibling", encoding="utf-8")

            backend = RamWorkspace(project, ram_root=ram)
            execution = backend.prepare()
            try:
                self.assertTrue((execution / "pyproject.toml").is_file())
                self.assertTrue((execution / "README.md").is_file())
                self.assertFalse((execution / "aicoder-experimental").exists())
                self.assertFalse((execution / "other-project").exists())
                self.assertEqual(backend.info.source_root, project.resolve())
            finally:
                backend.abort()

    def test_ram_workspace_excludes_generated_dependencies_and_language_caches(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            root = Path(temp)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            generated_dirs = (
                ".venv", "node_modules", "target", ".gradle", "obj",
                "dist", "build", "coverage", ".cache", "vendor", "DerivedData",
            )
            for name in generated_dirs:
                path = root / name
                path.mkdir(parents=True)
                (path / "generated.bin").write_bytes(b"x" * 64 * 1024)
            for name in ("module.pyc", "native.o", "debug.log", "scratch.tmp", ".DS_Store"):
                (root / name).write_bytes(b"generated")

            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            try:
                self.assertTrue((execution / "app.py").is_file())
                for name in generated_dirs:
                    self.assertFalse((execution / name).exists(), name)
                for name in ("module.pyc", "native.o", "debug.log", "scratch.tmp", ".DS_Store"):
                    self.assertFalse((execution / name).exists(), name)
                self.assertLess(backend.info.estimated_bytes, 64 * 1024)
            finally:
                backend.abort()


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

    def test_ram_workspace_finalizer_removes_orphan_execution_tree(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            root = Path(temp)
            (root / "x.txt").write_text("x", encoding="utf-8")
            backend = RamWorkspace(root, ram_root=ram)
            execution = backend.prepare()
            self.assertTrue(execution.exists())
            del backend
            gc.collect()
            self.assertFalse(execution.exists())

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

class WorkspaceNativeDiffTests(unittest.TestCase):
    def test_ram_delta_diff_does_not_require_git_metadata(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as ram:
            source = Path(temp)
            (source / "one.txt").write_text("old\n", encoding="utf-8")
            backend = RamWorkspace(source, ram_root=ram)
            root = backend.prepare()
            try:
                (root / "one.txt").write_text("new\n", encoding="utf-8")
                (root / "two.txt").write_text("added\n", encoding="utf-8")
                diff = backend.delta_diff()
                self.assertIn("-old", diff)
                self.assertIn("+new", diff)
                self.assertIn("two.txt", diff)
            finally:
                backend.abort()
