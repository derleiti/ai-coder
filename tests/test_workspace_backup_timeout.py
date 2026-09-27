from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aicoder.workspace_backup import snapshot_workspace


class WorkspaceBackupTimeoutTests(unittest.TestCase):
    def test_timed_out_snapshot_is_removed_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            backup = root / "backups"
            workspace.mkdir()
            (workspace / "data.txt").write_text("data\n", encoding="utf-8")
            with (
                patch.dict("os.environ", {"AILINUX_WORKSPACE_BACKUP_ROOT": str(backup)}, clear=False),
                patch("aicoder.workspace_backup.time.monotonic", side_effect=[0.0, 2.0]),
            ):
                with self.assertRaisesRegex(TimeoutError, "workspace fallback backup timed out"):
                    snapshot_workspace(workspace, source="binary_exec", timeout_s=1)
            action_root = backup / "aicoder"
            self.assertEqual(list(action_root.rglob("metadata.json")), [])
            self.assertEqual(list(action_root.rglob("workspace.tar.gz")), [])


if __name__ == "__main__":
    unittest.main()
