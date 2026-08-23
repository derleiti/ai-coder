from __future__ import annotations
import json, unittest
from aicoder.local_os import LocalOSToolProvider
from aicoder.plugins import discover_plugins

class LocalOSProviderTests(unittest.TestCase):
    def setUp(self): self.provider=LocalOSToolProvider()
    def test_read_only_tools_and_metadata(self):
        tools={tool.name:tool for tool in self.provider.tools()}
        self.assertTrue(tools['os_system_overview'].security.read_only)
        self.assertFalse(tools['os_system_overview'].security.mutating)
        registry=discover_plugins('.')
        schemas={schema['name']:schema for schema in registry.tool_schemas()}
        self.assertIn('system_diagnostics',schemas['os_system_overview']['capabilities'])
        self.assertTrue(schemas['os_system_overview']['annotations']['readOnlyHint'])
    def test_overview_runs(self):
        text,err=self.provider.execute('os_system_overview',{})
        self.assertFalse(err)
        data=json.loads(text)
        self.assertIn('hostname',data)
        self.assertIn('release',data)
    def test_local_os_is_builtin_provider(self):
        record=discover_plugins('.').get('local-os')
        self.assertIsNotNone(record)
        self.assertTrue(record.enabled)
        self.assertTrue(record.executable)

if __name__=='__main__': unittest.main()


class TermuxCompatibilityTests(unittest.TestCase):
    def test_termux_storage_avoids_gnu_df_x_and_tolerates_missing_lsblk(self):
        provider = LocalOSToolProvider()
        calls = []
        def fake_run(argv, **kwargs):
            calls.append(argv)
            return json.dumps({"argv": argv, "exit_code": 0, "stdout": "ok", "stderr": ""}), False
        with patch.dict(os.environ, {"TERMUX_VERSION": "0.119"}), patch("aicoder.local_os._run", side_effect=fake_run), patch("aicoder.local_os.shutil.which", return_value=None):
            text, err = provider.execute("os_storage_overview", {})
        self.assertFalse(err)
        self.assertEqual(calls[0], ["df", "-h"])
        payload = json.loads(text)
        self.assertEqual(payload["environment"], "termux")
        self.assertFalse(payload["block_devices"]["available"])
