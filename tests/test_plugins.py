from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aicoder.plugins import (
    PluginManifestError,
    SettingsToolProvider,
    ToolProvider,
    discover_plugins,
    load_manifest,
    plugin_catalog_stamp,
    set_plugin_enabled,
)


def _write_manifest(root: Path, plugin_id: str, *, trusted: bool = False, provider: str | None = None, extra: str = "") -> Path:
    plugin = root / plugin_id
    plugin.mkdir(parents=True, exist_ok=True)
    provider_line = f'\ntool_provider = "{provider}"' if provider else ""
    path = plugin / "plugin.toml"
    path.write_text(
        "\n".join([
            "[plugin]",
            f'id = "{plugin_id}"',
            f'name = "{plugin_id.title()}"',
            'version = "1.0.0"',
            'api_version = "1"',
            'description = "test plugin"',
            "",
            "[capabilities]",
            'groups = ["test", "code.read"]',
            "",
            "[security]",
            f"trusted_builtin = {'true' if trusted else 'false'}",
            "",
            "[contributions]" + provider_line,
            extra,
            "",
        ]),
        encoding="utf-8",
    )
    return path


class PluginManifestTests(unittest.TestCase):
    def test_manifest_is_strict_and_external_cannot_self_assert_builtin_trust(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_manifest(Path(temp), "safe", trusted=True, provider="safe.provider:create")
            manifest = load_manifest(path, scope="workspace")
            self.assertFalse(manifest.trusted_builtin)
            self.assertEqual(manifest.tool_provider, "safe.provider:create")
            self.assertEqual(manifest.capability_groups, ("test", "code.read"))

    def test_unknown_manifest_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_manifest(Path(temp), "bad", extra='unexpected = "value"')
            with self.assertRaises(PluginManifestError):
                load_manifest(path, scope="user")

    def test_declarative_paths_cannot_escape_plugin_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_manifest(Path(temp), "escape")
            text = path.read_text().replace("[contributions]", '[contributions]\nskills_dir = "../../outside"')
            path.write_text(text)
            with self.assertRaises(PluginManifestError):
                load_manifest(path, scope="workspace")

    def test_wrong_api_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _write_manifest(Path(temp), "bad-api")
            text = path.read_text().replace('api_version = "1"', 'api_version = "999"')
            path.write_text(text)
            with self.assertRaises(PluginManifestError):
                load_manifest(path, scope="workspace")


class PluginDiscoveryTests(unittest.TestCase):
    def test_workspace_overrides_user_and_builtin_and_conflict_is_visible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config"
            workspace = root / "workspace"
            workspace.mkdir()
            _write_manifest(config / "plugins", "settings")
            _write_manifest(workspace / ".aicoder" / "plugins", "settings")

            registry = discover_plugins(workspace, config_dir=config)
            record = registry.get("settings")
            self.assertIsNotNone(record)
            self.assertEqual(record.scope, "workspace")
            self.assertFalse(record.executable)
            self.assertEqual(len(registry.shadowed), 2)
            self.assertTrue(record.conflicts)
            self.assertEqual(registry.tool_schemas(), [])

    def test_symlink_plugin_directory_is_not_discovered(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config"
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            path = _write_manifest(outside, "escape")
            plugins = workspace / ".aicoder" / "plugins"
            plugins.mkdir(parents=True)
            try:
                (plugins / "escape").symlink_to(path.parent, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            registry = discover_plugins(workspace, config_dir=config)
            self.assertIsNone(registry.get("escape"))

    def test_external_provider_is_discovered_but_never_imported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config"
            workspace = root / "workspace"
            workspace.mkdir()
            _write_manifest(config / "plugins", "external", provider="evil.module:factory")
            registry = discover_plugins(workspace, config_dir=config)
            record = registry.get("external")
            self.assertIsNotNone(record)
            self.assertFalse(record.executable)
            self.assertIsNone(record.provider)
            self.assertTrue(any("not loaded" in item for item in record.diagnostics))

    def test_plugin_state_changes_catalog_stamp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config"
            workspace = root / "workspace"
            workspace.mkdir()
            before = plugin_catalog_stamp(workspace, config_dir=config)
            set_plugin_enabled("settings", False, config_dir=config)
            after = plugin_catalog_stamp(workspace, config_dir=config)
            self.assertNotEqual(before, after)

    def test_enable_disable_state_is_private_and_applied_on_next_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config"
            workspace = root / "workspace"
            workspace.mkdir()
            set_plugin_enabled("settings", False, config_dir=config)
            registry = discover_plugins(workspace, config_dir=config)
            self.assertFalse(registry.get("settings").enabled)
            self.assertEqual(registry.tool_schemas(), [])
            state_file = config / "plugins.json"
            self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)
            self.assertFalse(json.loads(state_file.read_text())["enabled"]["settings"])


class ToolProviderTests(unittest.TestCase):
    def test_settings_provider_matches_toolprovider_contract(self):
        provider = SettingsToolProvider()
        self.assertIsInstance(provider, ToolProvider)
        names = {tool.name for tool in provider.tools()}
        self.assertIn("settings_list", names)
        self.assertIn("settings_apply_patch", names)

    def test_unknown_tool_security_defaults_to_mutating(self):
        security = SettingsToolProvider().security_for("future_tool", {})
        self.assertTrue(security.mutating)
        self.assertFalse(security.read_only)
        self.assertTrue(security.external_side_effect)


if __name__ == "__main__":
    unittest.main()
