from __future__ import annotations
import io, json, unittest
from aicoder.local_os import LocalOSToolProvider
from aicoder.mcp_server import MODERN_PROTOCOL, handle_request, serve_stdio

class McpServerTests(unittest.TestCase):
    def setUp(self): self.provider=LocalOSToolProvider()
    def test_modern_discover(self):
        r=handle_request({'jsonrpc':'2.0','id':1,'method':'server/discover','params':{},'_meta':{}},self.provider)
        self.assertIn(MODERN_PROTOCOL,r['result']['supportedVersions'])
    def test_legacy_initialize(self):
        r=handle_request({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},self.provider)
        self.assertEqual(r['result']['protocolVersion'],'2025-11-25')
    def test_list_is_read_only(self):
        r=handle_request({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},self.provider)
        names={tool['name'] for tool in r['result']['tools']}
        self.assertIn('os_system_overview',names)
        self.assertTrue(all(tool['annotations']['readOnlyHint'] for tool in r['result']['tools']))
        self.assertEqual(r['result']['ttlMs'],300000)
    def test_stdio_roundtrip(self):
        raw=json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})+'\n'
        out=io.StringIO(); rc=serve_stdio(self.provider,stdin=io.StringIO(raw),stdout=out)
        self.assertEqual(rc,0)
        self.assertEqual(json.loads(out.getvalue())['id'],2)

if __name__=='__main__': unittest.main()
