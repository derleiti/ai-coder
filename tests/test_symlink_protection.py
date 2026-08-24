from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from aicoder.workspace import path_within_workspace


class SymlinkProtectionTests(unittest.TestCase):
    """Tests for symlink protection in workspace path resolution.
    
    Ensures that symlinks pointing outside the workspace are properly rejected,
    and only symlinks within the workspace are allowed.
    """

    def setUp(self):
        """Create a temporary directory structure for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.workspace_root = Path(self.temp_dir) / "workspace"
        self.workspace_root.mkdir()
        
        # Create an external directory outside the workspace
        self.external_dir = Path(self.temp_dir) / "external"
        self.external_dir.mkdir()
        
        # Create a file in the external directory
        (self.external_dir / "external_file.txt").write_text("external content")

    def tearDown(self):
        """Clean up temporary directories."""
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_symlink_outside_workspace_is_rejected(self):
        """Symlinks pointing outside the workspace should be rejected."""
        # Create a symlink outside the workspace pointing to external file
        symlink_path = Path(self.temp_dir) / "external_link.txt"
        try:
            symlink_path.symlink_to(self.external_dir / "external_file.txt")
        except OSError:
            self.skip("Cannot create symlinks on this system")
            return
        
        # Try to resolve the symlink within the workspace context
        resolved, is_within = path_within_workspace(str(symlink_path), str(self.workspace_root))
        
        # Should not be within workspace
        self.assertFalse(is_within, "Symlink outside workspace should be rejected")

    def test_symlink_inside_workspace_is_allowed(self):
        """Symlinks pointing within the workspace should be allowed."""
        # Create a file inside the workspace
        workspace_file = self.workspace_root / "workspace_file.txt"
        workspace_file.write_text("workspace content")
        
        # Create a symlink inside the workspace pointing to the workspace file
        symlink_path = self.workspace_root / "symlink_to_workspace_file.txt"
        try:
            symlink_path.symlink_to(workspace_file)
        except OSError:
            self.skip("Cannot create symlinks on this system")
            return
        
        # Try to resolve the symlink within the workspace context
        resolved, is_within = path_within_workspace(str(symlink_path), str(self.workspace_root))
        
        # Should be within workspace
        self.assertTrue(is_within, "Symlink inside workspace should be allowed")
        # Should resolve to the target file
        self.assertTrue(resolved.exists() or resolved.is_symlink())

    def test_absolute_path_outside_workspace_is_rejected(self):
        """Absolute paths outside the workspace should be rejected."""
        resolved, is_within = path_within_workspace(
            str(self.external_dir / "external_file.txt"),
            str(self.workspace_root)
        )
        
        self.assertFalse(is_within, "Absolute path outside workspace should be rejected")

    def test_relative_path_outside_workspace_is_rejected(self):
        """Relative paths that escape the workspace should be rejected."""
        # Try to access parent directory
        resolved, is_within = path_within_workspace(
            "../external/external_file.txt",
            str(self.workspace_root)
        )
        
        self.assertFalse(is_within, "Relative path escaping workspace should be rejected")

    def test_regular_file_inside_workspace_is_allowed(self):
        """Regular files inside the workspace should be allowed."""
        workspace_file = self.workspace_root / "regular_file.txt"
        workspace_file.write_text("regular content")
        
        resolved, is_within = path_within_workspace(
            str(workspace_file),
            str(self.workspace_root)
        )
        
        self.assertTrue(is_within, "Regular file inside workspace should be allowed")
        self.assertTrue(resolved.exists())

    def test_nested_symlink_within_workspace_is_allowed(self):
        """Nested symlinks within the workspace should be allowed."""
        # Create nested structure
        nested_dir = self.workspace_root / "nested" / "deep"
        nested_dir.mkdir(parents=True)
        
        nested_file = nested_dir / "nested_file.txt"
        nested_file.write_text("nested content")
        
        # Create symlink in workspace root pointing to nested file
        symlink_path = self.workspace_root / "link_to_nested.txt"
        try:
            symlink_path.symlink_to(nested_file)
        except OSError:
            self.skip("Cannot create symlinks on this system")
            return
        
        resolved, is_within = path_within_workspace(
            str(symlink_path),
            str(self.workspace_root)
        )
        
        self.assertTrue(is_within, "Nested symlink within workspace should be allowed")

    def test_workspace_path_resolution_with_symlinks(self):
        """Test workspace path resolution handles symlinks correctly."""
        # Create a file in the workspace
        workspace_file = self.workspace_root / "test_file.txt"
        workspace_file.write_text("test content")
        
        # Create a symlink in the workspace
        symlink_path = self.workspace_root / "test_link.txt"
        try:
            symlink_path.symlink_to(workspace_file)
        except OSError:
            self.skip("Cannot create symlinks on this system")
            return
        
        # Test various path forms
        test_cases = [
            (str(symlink_path), True, "symlink by absolute path"),
            ("test_link.txt", True, "symlink by relative path"),
            ("./test_link.txt", True, "symlink by relative path with ./"),
        ]
        
        for path, expected_within, description in test_cases:
            with self.subTest(description):
                resolved, is_within = path_within_workspace(
                    path,
                    str(self.workspace_root)
                )
                self.assertEqual(
                    is_within, expected_within,
                    f"Path '{path}' should {'be within' if expected_within else 'not be within'} workspace"
                )


if __name__ == "__main__":
    unittest.main()
