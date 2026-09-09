from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen
from unittest.mock import MagicMock, patch

from aicoder.cli import build_parser, cmd_mcp
from aicoder.mcp_credentials import MCPAuthError, auth_headers
from aicoder.mcp_registry import (
    MCPRegistry, MCPRegistryError, MCPServerConfig, external_tool_schemas,
    list_server_tools, namespaced_tool_name,
)
from aicoder import mcp_service
from aicoder.setup import _repl_mcp_command


STDIO_SERVER = r'''import json, sys
for line in sys.stdin:
    msg=json.loads(line)
    if msg.get("id") is None: continue
    method=msg.get("method")
    if method=="initialize": result={"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"fake","version":"1"}}
    elif method=="tools/list": result={"tools":[{"name":"read","description":"read","inputSchema":{"type":"object"},"annotations":{"readOnlyHint":True}},{"name":"write","description":"write","inputSchema":{"type":"object"}}]}
    elif method=="tools/call": result={"content":[{"type":"text","text":"called:"+(msg.get("params") or {}).get("name","")}],"isError":False}
    else: result={}
    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":result}),flush=True)
'''


class RegistryAndPolicyTests(unittest.TestCase):
    def test_registry_roundtrip_permissions_and_secret_absence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mcp_servers.json"
            registry = MCPRegistry(path)
            config = MCPServerConfig(
                name="docs", transport="streamable-http", url="https://example.invalid/mcp",
                trust="untrusted", auth_type="api-key", auth_header="X-API-Key",
                allow_tools=["read"], deny_tools=["write"], capability_tags=["docs"],
            )
            registry.put(config)
            loaded = registry.get("docs")
            self.assertEqual(loaded, config)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("secret-value", raw)
            self.assertNotIn("access_token", raw)
            self.assertNotIn("refresh_token", raw)

    def test_credentials_in_url_and_secret_query_are_rejected(self):
        for url in (
            "https://user:pass@example.invalid/mcp",
            "https://example.invalid/mcp?api_key=secret",
            "https://example.invalid/mcp?authorization=secret",
        ):
            with self.subTest(url=url), self.assertRaises(MCPRegistryError):
                mcp_service.test_config(MCPServerConfig(name="bad", transport="streamable-http", url=url))

    def test_http_environment_header_injection_is_rejected(self):
        with self.assertRaises(MCPRegistryError):
            mcp_service.test_config(MCPServerConfig(
                name="bad", transport="streamable-http", url="https://example.invalid/mcp",
                header_env={"X-Token": "MCP_SECRET"},
            ))

    def test_reserved_custom_headers_are_rejected(self):
        for header in ("Authorization", "Host", "Mcp-Session-Id", "Cookie", "Content-Length"):
            with self.subTest(header=header), self.assertRaises(MCPRegistryError):
                mcp_service.test_config(MCPServerConfig(
                    name="bad", transport="streamable-http", url="https://example.invalid/mcp",
                    auth_type="custom-header", auth_header=header,
                ))

    def test_namespace_is_stable_and_collision_resistant(self):
        self.assertEqual(namespaced_tool_name("github", "search_code"), "mcp.github.search_code")
        self.assertNotEqual(namespaced_tool_name("github", "search"), namespaced_tool_name("docs", "search"))

    def test_allow_deny_and_untrusted_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "server.py"
            script.write_text(STDIO_SERVER, encoding="utf-8")
            config = MCPServerConfig(
                name="demo", command=sys.executable, args=[str(script)],
                allow_tools=["read", "write"], deny_tools=["write"], trust="untrusted", timeout=5,
            )
            registry = MagicMock()
            registry.list.return_value = [{**config.__dict__, "builtin": False}]
            schemas = external_tool_schemas(registry)
        self.assertEqual([row["name"] for row in schemas], ["mcp.demo.read"])
        self.assertFalse(schemas[0]["annotations"]["readOnlyHint"])

    def test_disabled_server_exposes_no_runtime_tools(self):
        config = MCPServerConfig(name="demo", enabled=False, command=sys.executable)
        registry = MagicMock()
        registry.list.return_value = [{**config.__dict__, "builtin": False}]
        with patch("aicoder.mcp_registry.list_server_tools") as discover:
            self.assertEqual(external_tool_schemas(registry), [])
        discover.assert_not_called()


class AuthenticationTests(unittest.TestCase):
    def test_api_key_bearer_basic_and_custom_header(self):
        values = {
            "api_key": "api-secret",
            "bearer_token": "bearer-secret",
            "basic_password": "basic-secret",
            "custom_header": "custom-secret",
        }
        with patch("aicoder.mcp_credentials.get_mcp_secret", side_effect=lambda _server, field: values.get(field, "")):
            api = auth_headers(MCPServerConfig(name="x", transport="streamable-http", url="https://x.invalid", auth_type="api-key", auth_header="X-API-Key"))
            bearer = auth_headers(MCPServerConfig(name="x", transport="streamable-http", url="https://x.invalid", auth_type="bearer"))
            basic = auth_headers(MCPServerConfig(name="x", transport="streamable-http", url="https://x.invalid", auth_type="basic", auth_username="markus"))
            custom = auth_headers(MCPServerConfig(name="x", transport="streamable-http", url="https://x.invalid", auth_type="custom-header", auth_header="X-MCP-Token"))
        self.assertEqual(api, {"X-API-Key": "api-secret"})
        self.assertEqual(bearer, {"Authorization": "Bearer bearer-secret"})
        self.assertEqual(base64.b64decode(basic["Authorization"].split()[1]).decode(), "markus:basic-secret")
        self.assertEqual(custom, {"X-MCP-Token": "custom-secret"})

    def test_missing_required_credential_fails_closed(self):
        with patch("aicoder.mcp_credentials.get_mcp_secret", return_value=""):
            with self.assertRaises(MCPAuthError):
                auth_headers(MCPServerConfig(name="x", transport="streamable-http", url="https://x.invalid", auth_type="bearer"))

    def test_oauth_is_not_static_bearer_alias(self):
        config = MCPServerConfig(name="oauth", transport="streamable-http", url="https://x.invalid/mcp", auth_type="oauth2", oauth_client_id="client")
        with patch("aicoder.mcp_oauth.oauth_access_token", return_value="fresh-token") as token:
            self.assertEqual(auth_headers(config), {"Authorization": "Bearer fresh-token"})
        token.assert_called_once_with(config)


class _AuthHTTPHandler(BaseHTTPRequestHandler):
    last_headers: dict[str, str] = {}
    saw_session = False
    use_sse = False
    sse_notification_first = False
    protocol_versions: list[str] = []

    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802
        type(self).last_headers = {key: value for key, value in self.headers.items()}
        type(self).protocol_versions.append(str(self.headers.get("MCP-Protocol-Version") or ""))
        if self.headers.get("Mcp-Session-Id") == "session-42":
            type(self).saw_session = True
        size = int(self.headers.get("Content-Length", "0"))
        msg = json.loads(self.rfile.read(size) or b"{}")
        if msg.get("id") is None:
            self.send_response(202)
            self.end_headers()
            return
        method = msg.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "http-fake", "version": "1"}}
        else:
            result = {"tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}]}
        payload = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        if type(self).use_sse:
            prefix = ""
            if type(self).sse_notification_first and method == "tools/list":
                notification = {"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info"}}
                prefix = "event: message\n" + "data: " + json.dumps(notification) + "\n\n"
            body = (prefix + "event: message\n" + "data: " + json.dumps(payload) + "\n\n").encode()
            content_type = "text/event-stream"
        else:
            body = json.dumps(payload).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Mcp-Session-Id", "session-42")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HTTPTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _AuthHTTPHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        _AuthHTTPHandler.last_headers = {}
        _AuthHTTPHandler.saw_session = False
        _AuthHTTPHandler.use_sse = False
        _AuthHTTPHandler.sse_notification_first = False
        _AuthHTTPHandler.protocol_versions = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_authenticated_connection_modes_use_real_http_path(self):
        cases = [
            ("api-key", "api_key", "X-API-Key", "secret", None),
            ("bearer", "bearer_token", "Authorization", "Bearer secret", None),
            ("basic", "basic_password", "Authorization", None, "user"),
            ("custom-header", "custom_header", "X-Custom-Token", "secret", None),
        ]
        for mode, field, header, expected, username in cases:
            with self.subTest(mode=mode):
                _AuthHTTPHandler.last_headers = {}
                config = MCPServerConfig(
                    name="web", transport="streamable-http", url=self.url, auth_type=mode,
                    auth_header=header if mode in {"api-key", "custom-header"} else "X-API-Key",
                    auth_username=username or "", timeout=5,
                )
                with patch("aicoder.mcp_credentials.get_mcp_secret", side_effect=lambda _server, requested: "secret" if requested == field else ""):
                    tools = list_server_tools(config)
                self.assertEqual([tool["name"] for tool in tools], ["echo"])
                normalized_headers = {key.lower(): value for key, value in _AuthHTTPHandler.last_headers.items()}
                actual = normalized_headers.get(header.lower())
                if mode == "basic":
                    self.assertTrue(actual.startswith("Basic "))
                    self.assertEqual(base64.b64decode(actual.split()[1]).decode(), "user:secret")
                else:
                    self.assertEqual(actual, expected)
                self.assertTrue(_AuthHTTPHandler.saw_session)

    def test_sse_response_path_and_protocol_version_header(self):
        _AuthHTTPHandler.use_sse = True
        _AuthHTTPHandler.sse_notification_first = True
        tools = list_server_tools(MCPServerConfig(name="sse", transport="streamable-http", url=self.url, timeout=5))
        self.assertEqual([tool["name"] for tool in tools], ["echo"])
        self.assertTrue(_AuthHTTPHandler.saw_session)
        self.assertEqual(_AuthHTTPHandler.protocol_versions[0], "")
        self.assertTrue(all(value == "2025-06-18" for value in _AuthHTTPHandler.protocol_versions[1:]))


class _TokenHandler(BaseHTTPRequestHandler):
    forms: list[dict[str, list[str]]] = []
    mcp_authorization = ""

    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(size)
        if self.path.startswith("/mcp"):
            type(self).mcp_authorization = str(self.headers.get("Authorization") or "")
            if type(self).mcp_authorization != "Bearer new-access":
                self.send_response(401)
                self.end_headers()
                return
            msg = json.loads(raw or b"{}")
            if msg.get("id") is None:
                self.send_response(202)
                self.end_headers()
                return
            if msg.get("method") == "initialize":
                result = {"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"oauth-mcp","version":"1"}}
            elif msg.get("method") == "tools/list":
                result = {"tools":[{"name":"secure_echo","description":"OAuth tool","inputSchema":{"type":"object"}}]}
            else:
                result = {}
            body = json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "oauth-session")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        form = parse_qs(raw.decode(), keep_blank_values=True)
        type(self).forms.append(form)
        body = json.dumps({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600, "token_type": "Bearer"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _TokenHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        _TokenHandler.forms = []
        _TokenHandler.mcp_authorization = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def config(self):
        return MCPServerConfig(
            name="oauth", transport="streamable-http", url=f"{self.origin}/mcp",
            auth_type="oauth2", oauth_client_id="client-1",
            oauth_authorization_url=f"{self.origin}/authorize",
            oauth_token_url=f"{self.origin}/token", oauth_scopes=["tools.read"], timeout=5,
        )

    def test_refresh_token_flow_updates_keyring_only(self):
        from aicoder.mcp_oauth import refresh_oauth_token
        stored: dict[str, str] = {}
        current = {"oauth_refresh_token": "old-refresh"}
        with (
            patch("aicoder.mcp_oauth.get_mcp_secret", side_effect=lambda _server, field: current.get(field, "")),
            patch("aicoder.mcp_oauth.set_mcp_secret", side_effect=lambda _server, field, value: stored.__setitem__(field, value)),
        ):
            token = refresh_oauth_token(self.config())
        self.assertEqual(token, "new-access")
        self.assertEqual(stored["oauth_access_token"], "new-access")
        self.assertEqual(stored["oauth_refresh_token"], "new-refresh")
        self.assertIn("oauth_expires_at", stored)
        self.assertEqual(_TokenHandler.forms[0]["grant_type"], ["refresh_token"])
        self.assertEqual(_TokenHandler.forms[0]["refresh_token"], ["old-refresh"])

    def test_oauth_authorization_then_real_mcp_connection_e2e(self):
        from aicoder.mcp_oauth import authorize_oauth
        stored: dict[str, str] = {}

        def fake_browser_open(auth_url, *_args, **_kwargs):
            query = parse_qs(urlsplit(auth_url).query)
            redirect = query["redirect_uri"][0]
            state = query["state"][0]
            with urlopen(redirect + "?" + urlencode({"code":"e2e-code","state":state}), timeout=5) as response:
                response.read()
            return True

        config = self.config()
        with (
            patch("aicoder.mcp_oauth.webbrowser.open", side_effect=fake_browser_open),
            patch("aicoder.mcp_oauth.get_mcp_secret", side_effect=lambda _server, field: stored.get(field, "")),
            patch("aicoder.mcp_oauth.set_mcp_secret", side_effect=lambda _server, field, value: stored.__setitem__(field, value)),
        ):
            result = authorize_oauth(config, timeout=10, open_browser=True)
        self.assertTrue(result["authorized"])
        with (
            patch("aicoder.mcp_credentials.get_mcp_secret", side_effect=lambda _server, field: stored.get(field, "")),
            patch("aicoder.mcp_oauth.get_mcp_secret", side_effect=lambda _server, field: stored.get(field, "")),
        ):
            tools = list_server_tools(config)
        self.assertEqual([tool["name"] for tool in tools], ["secure_echo"])
        self.assertEqual(_TokenHandler.mcp_authorization, "Bearer new-access")

    def test_authorization_code_pkce_loopback_flow(self):
        from aicoder.mcp_oauth import authorize_oauth
        stored: dict[str, str] = {}

        def fake_browser_open(auth_url, *_args, **_kwargs):
            query = parse_qs(urlsplit(auth_url).query)
            redirect = query["redirect_uri"][0]
            state = query["state"][0]
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertTrue(query["code_challenge"][0])
            with urlopen(redirect + "?" + urlencode({"code": "auth-code", "state": state}), timeout=5) as response:
                response.read()
            return True

        with (
            patch("aicoder.mcp_oauth.webbrowser.open", side_effect=fake_browser_open),
            patch("aicoder.mcp_oauth.get_mcp_secret", return_value=""),
            patch("aicoder.mcp_oauth.set_mcp_secret", side_effect=lambda _server, field, value: stored.__setitem__(field, value)),
        ):
            result = authorize_oauth(self.config(), timeout=10, open_browser=True)
        self.assertTrue(result["authorized"])
        self.assertEqual(stored["oauth_access_token"], "new-access")
        form = _TokenHandler.forms[0]
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["code"], ["auth-code"])
        self.assertTrue(form["code_verifier"][0])


class SharedServiceTransactionTests(unittest.TestCase):
    def test_failed_save_restores_credentials_and_does_not_persist_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = MCPRegistry(Path(temp) / "mcp.json")
            config = MCPServerConfig(name="web", transport="streamable-http", url="https://example.invalid/mcp", auth_type="bearer")
            with (
                patch("aicoder.mcp_service.snapshot_mcp_secrets", return_value={"bearer_token": "old"}),
                patch("aicoder.mcp_service.set_mcp_secret") as store,
                patch("aicoder.mcp_service.credential_status", return_value={"bearer_token": True}),
                patch("aicoder.mcp_service.doctor_server", return_value={"name":"web", "ok":False, "error":"connection failed"}),
                patch("aicoder.mcp_service.restore_mcp_secrets") as restore,
            ):
                with self.assertRaises(mcp_service.MCPServiceError):
                    mcp_service.save_server(config, secrets={"bearer_token": "new"}, registry=registry, test=True)
            store.assert_called_once()
            restore.assert_called_once_with("web", {"bearer_token": "old"})
            self.assertIsNone(registry.get("web"))

    def test_candidate_connection_test_never_persists_and_restores_keyring(self):
        config = MCPServerConfig(name="web", transport="streamable-http", url="https://example.invalid/mcp", auth_type="bearer")
        with (
            patch("aicoder.mcp_service.snapshot_mcp_secrets", return_value={"bearer_token":"old"}),
            patch("aicoder.mcp_service.set_mcp_secret") as store,
            patch("aicoder.mcp_service.doctor_server", return_value={"name":"web","ok":True,"tool_count":1,"error":""}),
            patch("aicoder.mcp_service.restore_mcp_secrets") as restore,
        ):
            result = mcp_service.test_candidate(config, secrets={"bearer_token":"candidate"})
        self.assertTrue(result["ok"])
        store.assert_called_once_with("web", "bearer_token", "candidate")
        restore.assert_called_once_with("web", {"bearer_token":"old"})

    def test_successful_save_invalidates_tool_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = MCPRegistry(Path(temp) / "mcp.json")
            config = MCPServerConfig(name="stdio", command=sys.executable, args=["fake.py"])
            with (
                patch("aicoder.mcp_service.snapshot_mcp_secrets", return_value={}),
                patch("aicoder.mcp_service.credential_status", return_value={}),
                patch("aicoder.mcp_service.doctor_server", return_value={"name":"stdio", "ok":True, "tool_count":2, "error":""}),
                patch("aicoder.executor.invalidate_tool_cache") as invalidate,
            ):
                mcp_service.save_server(config, registry=registry, test=True)
            invalidate.assert_called_once()
            self.assertIsNotNone(registry.get("stdio"))

    def test_remove_rolls_back_registry_when_keyring_delete_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = MCPRegistry(Path(temp) / "mcp.json")
            config = MCPServerConfig(name="web", transport="streamable-http", url="https://example.invalid/mcp")
            registry.put(config)
            with (
                patch("aicoder.mcp_service.snapshot_mcp_secrets", return_value={"bearer_token": "secret"}),
                patch("aicoder.mcp_service.credential_status", return_value={"bearer_token": True}),
                patch("aicoder.mcp_service.delete_mcp_secret", side_effect=RuntimeError("keyring failure")),
                patch("aicoder.mcp_service.restore_mcp_secrets") as restore,
            ):
                with self.assertRaises(RuntimeError):
                    mcp_service.remove_server("web", registry)
            self.assertEqual(registry.get("web"), config)
            restore.assert_called_once()

    def test_enable_disable_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = MCPRegistry(Path(temp) / "mcp.json")
            registry.put(MCPServerConfig(name="demo", command=sys.executable))
            with patch("aicoder.executor.invalidate_tool_cache") as invalidate:
                mcp_service.disable_server("demo", registry)
                self.assertFalse(registry.get("demo").enabled)
                mcp_service.enable_server("demo", registry)
                self.assertTrue(registry.get("demo").enabled)
            self.assertEqual(invalidate.call_count, 2)


class SurfaceIntegrationTests(unittest.TestCase):
    def test_cli_management_uses_shared_service(self):
        args = build_parser().parse_args(["mcp", "list"])
        rows = [{"name":"triforce","builtin":True,"transport":"builtin","enabled":True,"trust":"builtin"}]
        with patch("aicoder.mcp_service.list_servers", return_value=rows) as shared, redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_mcp(args), 0)
        shared.assert_called_once()

    def test_cli_doctor_without_name_uses_shared_service(self):
        args = build_parser().parse_args(["mcp", "doctor"])
        with patch("aicoder.mcp_service.doctor", return_value=[]) as shared, redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_mcp(args), 0)
        shared.assert_called_once()

    def test_cli_has_no_secret_argument(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["mcp", "add", "demo", "--secret", "visible-secret"])

    def test_repl_list_uses_shared_service(self):
        rows = [{"name":"triforce","builtin":True,"transport":"builtin","enabled":True,"trust":"builtin"}]
        with patch("aicoder.mcp_service.list_servers", return_value=rows) as shared, redirect_stdout(io.StringIO()):
            self.assertEqual(_repl_mcp_command("list"), 0)
        shared.assert_called_once()

    def test_repl_rejects_secret_argument_before_service_call(self):
        output = io.StringIO()
        with patch("aicoder.mcp_service.save_server") as save, redirect_stdout(output):
            rc = _repl_mcp_command("add demo --url https://example.invalid/mcp --secret visible")
        self.assertEqual(rc, 2)
        self.assertIn("--secret is forbidden", output.getvalue())
        save.assert_not_called()

    @unittest.skipUnless(os.environ.get("QT_QPA_PLATFORM") == "offscreen", "GUI test is run in the offscreen verification pass")
    def test_gui_uses_shared_service_and_new_after_triforce_is_editable(self):
        from PyQt6.QtWidgets import QApplication
        import aicoder.gui.mcp_widget as widget_module
        app = QApplication.instance() or QApplication([])
        rows = [
            {"name":"triforce","builtin":True,"transport":"builtin","enabled":True,"trust":"builtin"},
            {"name":"demo","builtin":False,"transport":"stdio","enabled":True,"trust":"untrusted"},
        ]
        demo = MCPServerConfig(name="demo", command=sys.executable)
        with (
            patch.object(widget_module, "list_servers", return_value=rows) as shared,
            patch.object(widget_module, "get_server", return_value=demo),
            patch.object(widget_module, "authentication_status", return_value={"configured":True,"credential_status":{}}),
        ):
            widget = widget_module.MCPServersWidget()
            widget._load("triforce")
            self.assertFalse(widget.name.isEnabled())
            widget.clear()
            self.assertTrue(widget.name.isEnabled())
            self.assertEqual(widget.secret.text(), "")
            self.assertEqual(widget.oauth_client_secret.text(), "")
            shared.assert_called()
            widget.deleteLater()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
