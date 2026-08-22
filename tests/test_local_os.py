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
