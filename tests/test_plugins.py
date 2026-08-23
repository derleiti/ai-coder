from __future__ import annotations
import json, tempfile, unittest
from pathlib import Path
from aicoder.plugins import PluginManifestError, SettingsToolProvider, ToolProvider, discover_plugins, load_manifest, set_plugin_enabled


def manifest(root: Path, plugin_id: str, extra: str = "", provider: str | None = None) -> Path:
    d=root/plugin_id; d.mkdir(parents=True, exist_ok=True); p=d/'plugin.toml'
    provider_line=f'\ntool_provider = "{provider}"' if provider else ''
    p.write_text(f'''[plugin]\nid = "{plugin_id}"\nname = "{plugin_id}"\nversion = "1"\napi_version = "1"\ndescription = "test"\n[capabilities]\ngroups = ["test"]\n[security]\ntrusted_builtin = true\n[contributions]{provider_line}\n{extra}\n''')
    return p

class Plugins(unittest.TestCase):
    def test_external_cannot_self_trust(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(load_manifest(manifest(Path(t),'x'), scope='workspace').trusted_builtin)
    def test_unknown_field_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(PluginManifestError): load_manifest(manifest(Path(t),'x','bad = 1'), scope='user')
    def test_escape_path_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(PluginManifestError): load_manifest(manifest(Path(t),'x','skills_dir = "../../x"'), scope='user')
    def test_scope_precedence_and_no_external_import(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); ws=root/'ws'; ws.mkdir(); config=root/'cfg'
            manifest(config/'plugins','settings',provider='evil.module:create')
            manifest(ws/'.aicoder/plugins','settings',provider='other.module:create')
            reg=discover_plugins(ws, config_dir=config); rec=reg.get('settings')
            self.assertEqual(rec.scope,'workspace'); self.assertFalse(rec.executable); self.assertIsNone(rec.provider)
            self.assertEqual(len(reg.shadowed),2)
    def test_symlink_directory_ignored(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); ws=root/'ws'; ws.mkdir(); outside=root/'outside'; manifest(outside,'x')
            plugins=ws/'.aicoder/plugins'; plugins.mkdir(parents=True)
            try: (plugins/'x').symlink_to(outside/'x', target_is_directory=True)
            except OSError: self.skipTest('symlink unavailable')
            self.assertIsNone(discover_plugins(ws, config_dir=root/'cfg').get('x'))
    def test_enable_state_private(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); ws=root/'ws'; ws.mkdir(); config=root/'cfg'
            set_plugin_enabled('settings',False,config_dir=config)
            self.assertFalse(discover_plugins(ws,config_dir=config).get('settings').enabled)
            self.assertEqual((config/'plugins.json').stat().st_mode & 0o777,0o600)
            self.assertFalse(json.loads((config/'plugins.json').read_text())['enabled']['settings'])
    def test_settings_provider_contract(self):
        provider=SettingsToolProvider(); self.assertIsInstance(provider,ToolProvider)
        self.assertIn('settings_list',{t.name for t in provider.tools()})
        self.assertTrue(provider.security_for('future_unknown',{}).mutating)

if __name__ == '__main__': unittest.main()
