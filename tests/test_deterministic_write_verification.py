from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aicoder.executor import run_directory_create, run_file_edit


class DeterministicWriteVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = patch("aicoder.executor._workspace_root", return_value=self.root)
        self.workspace.start()

    def tearDown(self):
        self.workspace.stop()
        self.temp.cleanup()

    def test_file_edit_reports_exact_readback_verification(self):
        result, is_error = run_file_edit({
            "path": "probe.txt", "operation": "create", "content": "alpha"
        })
        self.assertFalse(is_error, result)
        self.assertIn("verified exact content", result)
        self.assertEqual((self.root / "probe.txt").read_text(encoding="utf-8"), "alpha")

    def test_readback_mismatch_rolls_back_new_file(self):
        target = self.root / "probe.txt"
        with patch.object(Path, "read_text", return_value="wrong"):
            result, is_error = run_file_edit({
                "path": "probe.txt", "operation": "create", "content": "alpha"
            })
        self.assertTrue(is_error)
        self.assertIn("read-back verification mismatch", result)
        self.assertFalse(target.exists())

    def test_directory_create_reports_verification(self):
        result, is_error = run_directory_create({"path": "sub"})
        self.assertFalse(is_error, result)
        self.assertIn("verified directory exists", result)
        self.assertTrue((self.root / "sub").is_dir())


if __name__ == "__main__":
    unittest.main()
