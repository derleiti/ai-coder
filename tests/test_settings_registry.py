from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from aicoder import settings
from aicoder.settings import SettingsError, SettingsStore


class RegistrySchemaTests(unittest.TestCase):
    def test_every_default_matches_its_spec(self):
        for key, spec in settings.REGISTRY.items():
            self.assertEqual(settings.DEFAULTS[key], spec.default, key)

    def test_aliases_resolve_to_canonical_keys(self):
        for alias, expected in (
            ("model", "selected_model"),
            ("fallback", "fallback_model"),
            ("tool-mode", "tool_mode"),
            ("tool_mode", "tool_mode"),
            ("TIMEOUT", "request_timeout"),
            ("approval", "approval_mode"),
        ):
            with self.subTest(alias=alias):
                self.assertEqual(settings.resolve_key(alias), expected)

    def test_unknown_key_is_rejected_with_a_listing(self):
        with self.assertRaises(SettingsError) as ctx:
            settings.resolve_key("nope")
        self.assertIn("selected_model", str(ctx.exception))

    def test_approval_mode_is_flagged_security_impacting(self):
        # The policy layer keys off this flag to demand explicit confirmation,
        # so a regression here would silently let the model widen its own reach.
        self.assertTrue(settings.REGISTRY["approval_mode"].security_impact)

    def test_schema_ordering_is_deterministic(self):
        # Stable ordering keeps prompt prefixes cache-friendly (see §34.2).
        first = [entry["key"] for entry in settings.schema()]
        second = [entry["key"] for entry in settings.schema()]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first, key=lambda k: (settings.REGISTRY[k].group, k)))


class CoercionTests(unittest.TestCase):
    def test_enum_rejects_unknown_value_and_names_the_allowed_ones(self):
        with self.assertRaises(SettingsError) as ctx:
            settings.coerce("tool_mode", "sometimes")
        self.assertIn("on_demand", str(ctx.exception))

    def test_int_bounds_are_enforced(self):
        self.assertEqual(settings.coerce("request_timeout", "120"), 120)
        for bad in (5, 9999):
            with self.subTest(bad=bad), self.assertRaises(SettingsError):
                settings.coerce("request_timeout", bad)

    def test_list_syntax_variants(self):
        self.assertEqual(settings.coerce("enabled_tools", "code_read, git ,code_read"),
                         ["code_read", "git"])
        self.assertIsNone(settings.coerce("enabled_tools", "all"))
        self.assertEqual(settings.coerce("enabled_tools", "none"), [])
        self.assertEqual(settings.coerce("enabled_tools", ["b", "a"]), ["a", "b"])

    def test_non_nullable_setting_rejects_none(self):
        with self.assertRaises(SettingsError):
            settings.coerce("tool_mode", None)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.store = SettingsStore(lambda: self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_file_yields_defaults(self):
        self.assertEqual(self.store.get("tool_mode"), "on_demand")
        self.assertFalse(self.store.get("native_openrouter_tool_calling"))
        self.assertEqual(self.store.get("runtime_mode"), "native-light")

    def test_write_is_owner_only_and_versioned(self):
        self.store.set("swarm_mode", "auto")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["_schema_version"], settings.SCHEMA_VERSION)
        self.assertEqual(raw["swarm_mode"], "auto")

    def test_fallback_invariant_is_enforced_by_the_store(self):
        self.store.set("fallback_model", "prov/m")
        self.store.set("selected_model", "prov/m")
        self.assertEqual(self.store.get("fallback_model"), "")

    def test_invalid_value_is_rejected_before_it_reaches_disk(self):
        self.store.set("tool_mode", "always")
        self.store.set("native_openrouter_tool_calling", True)
        self.assertTrue(self.store.get("native_openrouter_tool_calling"))
        with self.assertRaises(SettingsError):
            self.store.set("tool_mode", "bogus")
        self.assertEqual(self.store.get("tool_mode"), "always")

    def test_corrupt_state_is_quarantined_not_destroyed(self):
        self.path.write_text("{not json at all", encoding="utf-8")
        data = self.store.load()
        self.assertEqual(data["tool_mode"], "on_demand")
        leftovers = list(self.path.parent.glob("state.json.corrupt-*"))
        self.assertEqual(len(leftovers), 1, "broken state.json must be preserved for diagnosis")
        self.assertIn("not json", leftovers[0].read_text(encoding="utf-8"))

    def test_unknown_persisted_value_falls_back_without_losing_the_rest(self):
        self.path.write_text(json.dumps({"tool_mode": "nonsense", "swarm_mode": "auto"}),
                             encoding="utf-8")
        data = self.store.load()
        self.assertEqual(data["tool_mode"], "on_demand")
        self.assertEqual(data["swarm_mode"], "auto")

    def test_reset_restores_a_single_default(self):
        self.store.set("swarm_mode", "on")
        self.store.reset("swarm")
        self.assertEqual(self.store.get("swarm_mode"), "off")

    def test_second_process_sees_an_external_change(self):
        # Simulates GUI + CLI: two stores, same file, independent caches.
        other = SettingsStore(lambda: self.path)
        self.assertEqual(other.get("swarm_mode"), "off")   # primes the cache
        self.store.set("swarm_mode", "review")
        self.assertEqual(other.get("swarm_mode"), "review")

    def test_concurrent_updates_do_not_lose_each_other(self):
        other = SettingsStore(lambda: self.path)
        self.store.set("swarm_mode", "auto")
        other.set("tool_mode", "always")
        merged = SettingsStore(lambda: self.path).load()
        self.assertEqual(merged["swarm_mode"], "auto")
        self.assertEqual(merged["tool_mode"], "always")


if __name__ == "__main__":
    unittest.main()
