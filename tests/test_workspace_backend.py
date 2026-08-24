from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicoder.workspace_backend import DiskWorkspace


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


if __name__ == "__main__":
    unittest.main()
