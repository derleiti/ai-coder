from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from aicoder import cli, settings
from aicoder.settings import SettingsStore


class SettingsCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.store = SettingsStore(lambda: self.path)
        self.store.save(dict(settings.DEFAULTS))
        self.store_patch = patch.object(cli.settings_core, "STORE", self.store)
        self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()
        self.temp.cleanup()

    def run_cmd(self, **kwargs):
        args = argparse.Namespace(**kwargs)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.cmd_settings(args)
        return rc, out.getvalue(), err.getvalue()

    def test_list_json_is_complete_and_deterministic(self):
        rc, out, _ = self.run_cmd(settings_action="list", json_out=True)
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertEqual({row["key"] for row in rows}, set(settings.REGISTRY))
        self.assertEqual(
            [row["key"] for row in rows],
            sorted(settings.REGISTRY, key=lambda k: (settings.REGISTRY[k].group, k)),
        )

    def test_set_get_and_reset_use_registry_coercion(self):
        rc, _, _ = self.run_cmd(settings_action="set", key="timeout", value="180")
        self.assertEqual(rc, 0)
        self.assertEqual(self.store.get("request_timeout"), 180)
        rc, out, _ = self.run_cmd(settings_action="get", key="request_timeout", json_out=True)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"key": "request_timeout", "value": 180})
        rc, _, _ = self.run_cmd(settings_action="reset", key="timeout", all=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.store.get("request_timeout"), settings.REGISTRY["request_timeout"].default)

    def test_enabled_tools_has_unambiguous_all_none_and_csv_forms(self):
        for raw, expected in (("none", []), ("git,file_read,git", ["file_read", "git"]), ("all", None)):
            with self.subTest(raw=raw):
                rc, _, _ = self.run_cmd(settings_action="set", key="enabled_tools", value=raw)
                self.assertEqual(rc, 0)
                self.assertEqual(self.store.get("enabled_tools"), expected)

    def test_invalid_value_returns_error_without_changing_disk(self):
        before = self.store.get("tool_mode")
        rc, _, err = self.run_cmd(settings_action="set", key="tool_mode", value="bogus")
        self.assertEqual(rc, 2)
        self.assertIn("Allowed", err)
        self.assertEqual(self.store.get("tool_mode"), before)

    def test_doctor_reports_private_permissions(self):
        rc, out, _ = self.run_cmd(settings_action="doctor", json_out=True)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["permissions"], "0o600")

    def test_parser_exposes_all_required_settings_commands(self):
        parser = cli.build_parser()
        for argv in (
            ["settings"], ["settings", "list"], ["settings", "get", "tool_mode"],
            ["settings", "set", "tool_mode", "always"], ["settings", "reset", "tool_mode"],
            ["settings", "reset", "--all"], ["settings", "explain", "tool_mode"],
            ["settings", "schema", "--json"], ["settings", "doctor"],
        ):
            with self.subTest(argv=argv):
                parsed = parser.parse_args(argv)
                self.assertIs(parsed.func, cli.cmd_settings)


if __name__ == "__main__":
    unittest.main()
