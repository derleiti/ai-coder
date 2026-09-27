from __future__ import annotations

import unittest
from unittest.mock import patch

from aicoder.subprocess_env import external_system_env


class ExternalSystemEnvTests(unittest.TestCase):
    def test_restores_original_loader_path_for_frozen_binary(self):
        base = {
            "PATH": "/usr/bin",
            "LD_LIBRARY_PATH": "/tmp/_MEI-bundle",
            "LD_LIBRARY_PATH_ORIG": "/opt/system/lib",
        }
        with patch("aicoder.subprocess_env.sys.frozen", True, create=True):
            env = external_system_env(base=base)
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/system/lib")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", env)
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_removes_bundle_loader_path_without_original(self):
        base = {
            "PATH": "/usr/bin",
            "LD_LIBRARY_PATH": "/tmp/_MEI-bundle",
            "LD_LIBRARY_PATH_ORIG": "",
        }
        with patch("aicoder.subprocess_env.sys.frozen", True, create=True):
            env = external_system_env(base=base)
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", env)
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_preserves_normal_environment_when_not_frozen(self):
        base = {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/opt/custom/lib"}
        with patch("aicoder.subprocess_env.sys.frozen", False, create=True):
            env = external_system_env(base=base)
        self.assertEqual(env, base)


if __name__ == "__main__":
    unittest.main()
