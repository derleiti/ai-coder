from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aicoder import executor, settings, settings_tools
from aicoder.privileges import approval_is_automatic, assess_execution
from aicoder.settings import SettingsStore


class DummyClient:
    base_url = "https://example.invalid"
    token = "test"


class SettingsToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.store = SettingsStore(lambda: self.path)
        self.store.save(dict(settings.DEFAULTS))
        self.patch = patch.object(settings_tools.settings, "STORE", self.store)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_plan_patch_does_not_write_and_flags_security(self):
        before = self.path.read_text(encoding="utf-8")
        plan = settings_tools.plan_patch({"request_timeout": 180, "approval_mode": "all"})
        self.assertTrue(plan["security_confirmation_required"])
        self.assertEqual(before, self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.store.get("request_timeout"), 120)

    def test_apply_patch_round_trips_and_verifies(self):
        text, is_error = settings_tools.run(
            "settings_apply_patch", {"patch": {"request_timeout": 180, "tool_mode": "always"}}
        )
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(payload["verified"]["request_timeout"], 180)
        self.assertEqual(self.store.get("tool_mode"), "always")

    def test_executor_marks_security_change_for_host_approval(self):
        seen = {}

        def approve(name, args):
            seen.update(args)
            return False

        result, is_error = executor.run_tool(
            DummyClient(),
            "settings_apply_patch",
            {"patch": {"approval_mode": "all"}, "reason": "user requested it"},
            approval_fn=approve,
            allowed_tools={"settings_apply_patch"},
        )
        self.assertTrue(is_error)
        self.assertIn("aborted by user", result)
        self.assertTrue(seen.get("_security_change"))
        self.assertTrue(seen.get("_mutating"))
        self.assertEqual(self.store.get("approval_mode"), "ask")

    def test_security_change_is_never_automatic(self):
        risk = assess_execution(
            "settings_apply_patch",
            {"_mutating": True, "_security_change": True, "reason": "change policy"},
        )
        self.assertTrue(risk.security_change)
        for mode in ("ask", "autopilot", "all"):
            with self.subTest(mode=mode):
                self.assertFalse(approval_is_automatic(mode, risk))

    def test_read_tools_need_no_approval(self):
        called = False

        def approve(_name, _args):
            nonlocal called
            called = True
            return False

        text, is_error = executor.run_tool(
            DummyClient(), "settings_get", {"key": "tool_mode"},
            approval_fn=approve, allowed_tools={"settings_get"},
        )
        self.assertFalse(is_error)
        self.assertFalse(called)
        self.assertEqual(json.loads(text)["value"], "on_demand")


if __name__ == "__main__":
    unittest.main()
