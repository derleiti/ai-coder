from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import aicoder.executor as executor
from aicoder.cli import build_parser, cmd_mcp
from aicoder.mcp_registry import (
    MCPRegistry, MCPRegistryError, MCPServerConfig, call_external_tool,
    doctor_server, external_tool_schemas, list_server_tools, parse_header_env,
)


STDIO_SERVER = r'''import json, sys
for line in sys.stdin:
    try: msg=json.loads(line)
    except Exception: continue
    ident=msg.get("id")
    method=msg.get("method")
    if ident is None:
        continue
    if method=="initialize":
        result={"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"dummy","version":"1"}}
    elif method=="tools/list":
        result={"tools":[
          {"name":"echo","description":"Echo input","inputSchema":{"type":"object","properties":{"text":{"type":"string"}}},"annotations":{"readOnlyHint":True}},
          {"name":"other","description":"Other","inputSchema":{"type":"object","properties":{}}}
        ]}
    elif method=="tools/call":
        params=msg.get("params") or {}; name=params.get("name"); args=params.get("arguments") or {}
        result={"content":[{"type":"text","text":f"{name}:{args.get('text','')}"}],"isError":False}
    else:
        result={}
    print(json.dumps({"jsonrpc":"2.0","id":ident,"result":result}), flush=True)
'''


class MCPRegistryTests(unittest.TestCase):
    def test_registry_is_private_and_stores_env_names_not_values(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"EXAMPLE_API_KEY":"super-secret-value"}, clear=False):
            path=Path(temp)/"mcp.json"; registry=MCPRegistry(path)
            registry.put(MCPServerConfig(name="demo",command=sys.executable,args=["server.py"],env_names=["EXAMPLE_API_KEY"]))
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            raw=path.read_text()
            self.assertIn("EXAMPLE_API_KEY",raw)
            self.assertNotIn("super-secret-value",raw)
            self.assertEqual(registry.get("demo").env_names,["EXAMPLE_API_KEY"])

    def test_url_credentials_and_secret_query_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            registry=MCPRegistry(Path(temp)/"mcp.json")
            with self.assertRaises(MCPRegistryError):
                registry.put(MCPServerConfig(name="bad",transport="streamable-http",url="https://user:pw@example.test/mcp"))
            with self.assertRaises(MCPRegistryError):
                registry.put(MCPServerConfig(name="bad2",transport="streamable-http",url="https://example.test/mcp?token=value"))

    def test_stdio_initialize_tools_filter_and_call(self):
        with tempfile.TemporaryDirectory() as temp:
            script=Path(temp)/"server.py"; script.write_text(STDIO_SERVER)
            config=MCPServerConfig(name="demo",command=sys.executable,args=[str(script)],allow_tools=["echo"],timeout=5,trust="trusted")
            tools=list_server_tools(config)
            self.assertEqual([t["name"] for t in tools],["echo"])
            with patch("aicoder.mcp_registry.MCPRegistry.get", return_value=config):
                result,is_error=call_external_tool("mcp.demo.echo",{"text":"hello"})
            self.assertFalse(is_error)
            self.assertEqual(result,"echo:hello")
            self.assertTrue(doctor_server(config)["ok"])

    def test_untrusted_server_cannot_self_declare_read_only(self):
        config=MCPServerConfig(name="demo",command=sys.executable,trust="untrusted")
        registry=MagicMock()
        registry.list.return_value=[{**config.__dict__,"builtin":False}]
        remote={"name":"echo","description":"x","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":True}}
        with patch("aicoder.mcp_registry.list_server_tools", return_value=[remote]):
            schema=external_tool_schemas(registry)[0]
        self.assertEqual(schema["name"],"mcp.demo.echo")
        self.assertFalse(schema["annotations"]["readOnlyHint"])

    def test_trusted_server_may_keep_read_only_hint(self):
        config=MCPServerConfig(name="demo",command=sys.executable,trust="trusted")
        registry=MagicMock(); registry.list.return_value=[{**config.__dict__,"builtin":False}]
        remote={"name":"echo","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":True}}
        with patch("aicoder.mcp_registry.list_server_tools", return_value=[remote]):
            schema=external_tool_schemas(registry)[0]
        self.assertTrue(schema["annotations"]["readOnlyHint"])

    def test_parser_exposes_registry_actions_and_transport_options(self):
        args=build_parser().parse_args(["mcp","add","demo","--transport","stdio","--command",sys.executable,"--env","EXAMPLE_API_KEY","--allow-tool","echo"])
        self.assertEqual(args.tool,"add")
        self.assertEqual(args.arg,["demo"])
        self.assertEqual(args.command,sys.executable)
        self.assertEqual(args.env_name,["EXAMPLE_API_KEY"])
        self.assertEqual(args.allow_tool,["echo"])

    def test_header_env_parser_is_strict(self):
        self.assertEqual(parse_header_env(["Authorization=MCP_AUTH"]), {"Authorization":"MCP_AUTH"})
        with self.assertRaises(MCPRegistryError):
            parse_header_env(["Authorization"])

    def test_cli_add_validates_then_persists_private_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            script=Path(temp)/"server.py"; script.write_text(STDIO_SERVER)
            args=build_parser().parse_args([
                "mcp","add","demo","--transport","stdio","--command",sys.executable,
                "--server-arg",str(script),"--allow-tool","echo","--env","MCP_TEST_KEY"
            ])
            with patch("aicoder.mcp_registry.CONFIG_DIR", Path(temp)):
                rc=cmd_mcp(args)
                registry=MCPRegistry(Path(temp)/"mcp_servers.json")
                saved=registry.get("demo")
            self.assertEqual(rc,0)
            self.assertIsNotNone(saved)
            self.assertEqual(saved.allow_tools,["echo"])
            self.assertEqual(saved.env_names,["MCP_TEST_KEY"])
            self.assertEqual((Path(temp)/"mcp_servers.json").stat().st_mode & 0o777,0o600)

    def test_executor_catalog_and_routing_include_namespaced_external_tool(self):
        client=MagicMock(); client.base_url="https://example.test"; client.token="x"
        client._request.return_value={"result":{"tools":[{"name":"health","description":"h","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":True}}]}}
        external={"name":"mcp.demo.echo","description":"external","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":True}}
        with patch("aicoder.mcp_registry.external_tool_schemas", return_value=[external]):
            tools=executor.load_tools(client,force_refresh=True)
        self.assertIn("mcp.demo.echo",{t["name"] for t in tools})
        with patch("aicoder.mcp_registry.call_external_tool", return_value=("ok",False)) as call, patch.object(executor.audit,"log_tool"):
            result,is_error=executor.run_tool(client,"mcp.demo.echo",{},allowed_tools={"mcp.demo.echo"})
        self.assertFalse(is_error); self.assertEqual(result,"ok"); call.assert_called_once()


class _HTTPHandler(BaseHTTPRequestHandler):
    seen_session=False
    seen_auth=""
    def log_message(self,*_): pass
    def do_POST(self):
        length=int(self.headers.get("Content-Length","0")); msg=json.loads(self.rfile.read(length) or b"{}")
        method=msg.get("method"); ident=msg.get("id")
        if self.headers.get("Mcp-Session-Id")=="session-1": type(self).seen_session=True
        if self.headers.get("Authorization"): type(self).seen_auth=self.headers.get("Authorization")
        if ident is None:
            self.send_response(202); self.end_headers(); return
        if method=="initialize":
            result={"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"http-dummy","version":"1"}}
        elif method=="tools/list":
            result={"tools":[{"name":"echo","description":"HTTP echo","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":True}}]}
        elif method=="tools/call":
            args=(msg.get("params") or {}).get("arguments") or {}
            result={"content":[{"type":"text","text":"http:"+str(args.get("text") or "")}],"isError":False}
        else: result={}
        body=json.dumps({"jsonrpc":"2.0","id":ident,"result":result}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Mcp-Session-Id","session-1"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)


class MCPStreamableHTTPTests(unittest.TestCase):
    def test_http_initialize_session_tools_auth_env_and_call(self):
        _HTTPHandler.seen_session=False; _HTTPHandler.seen_auth=""
        server=ThreadingHTTPServer(("127.0.0.1",0),_HTTPHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            config=MCPServerConfig(
                name="web",transport="streamable-http",url=f"http://127.0.0.1:{server.server_port}/mcp",
                timeout=5,trust="trusted",header_env={"Authorization":"MCP_HTTP_AUTH"}
            )
            with patch.dict(os.environ,{"MCP_HTTP_AUTH":"Bearer test-secret"},clear=False):
                tools=list_server_tools(config)
                self.assertEqual([t["name"] for t in tools],["echo"])
                with patch("aicoder.mcp_registry.MCPRegistry.get", return_value=config):
                    result,is_error=call_external_tool("mcp.web.echo",{"text":"hello"})
            self.assertFalse(is_error); self.assertEqual(result,"http:hello")
            self.assertTrue(_HTTPHandler.seen_session)
            self.assertEqual(_HTTPHandler.seen_auth,"Bearer test-secret")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__": unittest.main()
