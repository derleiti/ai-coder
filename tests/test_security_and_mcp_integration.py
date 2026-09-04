from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import aicoder.agent as cli_agent
import aicoder.executor as executor
from aicoder import audit
from aicoder import clipboard, web_search
from aicoder.client import CLIENT_PROFILE, ClientError, TokenExpiredError, TriForceClient
from aicoder.privileges import assess_execution
from aicoder.session_state import migrate_enabled_tools
from aicoder.tool_policy import (
    OPERATOR_MCP_TOOLS,
    INTERNAL_MCP_TOOLS,
    filter_tool_catalog,
    require_allowed_tool,
    triforce_host_forbidden_reason,
)


class ToolPolicyIntegrationTests(unittest.TestCase):
    def test_legacy_all_tools_snapshot_migrates_to_dynamic_all(self):
        legacy = [
            "agents", "clipboard_read", "clipboard_write", "code_grep", "code_read",
            "code_search", "code_tree", "dev_analyze", "dev_debug", "dev_links",
            "dev_lint", "dev_refactor", "dev_summarize", "devops", "doc_read",
            "doc_search", "file_edit", "file_read", "file_tree", "git", "health",
            "lint", "local_exec", "logs", "logs_errors", "logs_stats",
            "memory_search", "memory_store", "models", "ollama_list", "ollama_status",
            "remote_hosts", "remote_status", "search", "status", "test", "vault_keys",
            "vault_status", "web_fetch_local", "web_search_local",
        ]
        self.assertIsNone(migrate_enabled_tools(legacy))
        self.assertEqual(migrate_enabled_tools(["file_read", "test"]), ["file_read", "test"])

    def test_run_subset_policy_is_separate_from_mcp_transport_boundary(self):
        for name in (
            "vault_keys", "mail_send", "notify_send", "memory_clear",
            "search", "models", "specialist",
        ):
            with self.subTest(name=name):
                allowed, reason = require_allowed_tool(name, None)
                self.assertTrue(allowed, reason)

    def test_local_execution_names_are_allowed_for_runtime_but_not_mcp_catalog(self):
        for name in ("shell", "binary_exec", "task_runner"):
            allowed, reason = require_allowed_tool(name, {name})
            self.assertTrue(allowed, reason)
        catalog = [
            {"name": "shell", "inputSchema": {}},
            {"name": "binary_exec", "inputSchema": {}},
            {"name": "task_runner", "inputSchema": {}},
        ]
        self.assertEqual(filter_tool_catalog(catalog, OPERATOR_MCP_TOOLS), [])

    def test_catalog_filter_rejects_malformed_and_triforce_host_tools(self):
        catalog = [
            {"name": "code_read", "inputSchema": {}},
            {"name": "dev_refactor", "inputSchema": {}},
            {"name": "service_control", "inputSchema": {}},
            {"name": "remote_task", "inputSchema": {}},
            {"name": "vault_keys", "inputSchema": {}},
            {"name": "search", "inputSchema": {}},
            {"description": "missing name"},
            "invalid",
        ]
        self.assertEqual(
            filter_tool_catalog(catalog, None),
            [
                {"name": "vault_keys", "inputSchema": {}},
                {"name": "search", "inputSchema": {}},
            ],
        )
        for name in (
            "code_read", "dev_refactor", "service_control", "remote_task",
            "restart_backend", "admin_users", "docker_ps", "federation_status",
            "admin:health", "remote.search", "triforce.shell", "doc_read", "doc_search",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(triforce_host_forbidden_reason(name))
        for name in ("search", "models", "memory_search", "memory_store", "specialist"):
            with self.subTest(name=name):
                self.assertIsNone(triforce_host_forbidden_reason(name))

    def test_only_canonical_search_is_allowed(self):
        catalog = [
            {"name": "search", "description": "Search", "inputSchema": {}},
            {"name": "web_search", "description": "Legacy search", "inputSchema": {}},
        ]
        self.assertEqual(
            [tool["name"] for tool in filter_tool_catalog(catalog, OPERATOR_MCP_TOOLS)],
            ["search"],
        )

    def test_loaded_catalog_exposes_exactly_one_search_tool(self):
        client = MagicMock()
        client.base_url = "https://example.invalid"
        client.token = "opaque"
        client._request.return_value = {
            "result": {"tools": [{
                "name": "search",
                "description": "Unified search",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }]},
        }
        with (
            patch.object(executor, "_tool_cache", None),
            patch.object(executor, "_tool_cache_ts", 0),
            patch.object(executor, "_tool_cache_key", None),
            patch.object(executor, "_tool_security_hints", {}),
        ):
            names = [tool["name"] for tool in executor.load_tools(client)]
        self.assertEqual([name for name in names if name in {"search", "web_search", "web_search_local"}], ["search"])
        self.assertEqual(names.count("code_search"), 1)

    def test_cli_agent_cannot_execute_a_tool_when_tool_mode_is_off(self):
        client = MagicMock()
        client.chat.side_effect = [
            {
                "response": '<tool_call>{"name":"health","arguments":{}}</tool_call>',
                "model": "test",
            },
            {"response": "DONE: finished", "model": "test"},
        ]
        state = {
            "workspace_root": ".", "request_timeout": 30,
            "tool_mode": "off", "enabled_tools": [], "approval_mode": "ask",
        }
        with (
            patch.object(cli_agent, "load_session", return_value=SimpleNamespace(
                base_url="https://example.invalid", token="opaque",
            )),
            patch.object(cli_agent, "get_state", return_value=state),
            patch.object(cli_agent, "TriForceClient", return_value=client),
            patch.object(cli_agent, "run_tool") as execute,
            patch.object(cli_agent, "print_header"),
            patch.object(cli_agent, "print_task"),
            patch.object(cli_agent, "print_tool_call"),
            patch.object(cli_agent, "print_tool_result"),
            patch.object(cli_agent, "print_final"),
            patch.object(cli_agent, "history_record"),
        ):
            self.assertEqual(cli_agent.run_agent("hello", "test", None), 0)
        execute.assert_not_called()

    def test_explanatory_fenced_json_is_not_executable(self):
        text = (
            "This is only an example:\n"
            "```json\n"
            '{"name":"code_read","arguments":{"path":"README.md"}}\n'
            "```"
        )
        self.assertEqual(executor.parse_tool_calls(text), [])


class LocalCapabilityTests(unittest.TestCase):
    def setUp(self):
        self._saved_active_workspace = os.environ.pop("AICODER_ACTIVE_WORKSPACE", None)

    def tearDown(self):
        if self._saved_active_workspace is not None:
            os.environ["AICODER_ACTIVE_WORKSPACE"] = self._saved_active_workspace
        else:
            os.environ.pop("AICODER_ACTIVE_WORKSPACE", None)

    def test_known_mutation_bypasses_are_classified(self):
        for tool, command in (
            ("local_exec", "find . -type f -delete"),
            ("local_exec", "npm publish"),
            ("git", "git restore important.py"),
        ):
            with self.subTest(command=command):
                self.assertTrue(assess_execution(tool, {"command": command}).needs_approval)

    def test_typed_file_edit_is_confined_and_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "example.txt"
            target.write_text("before\n", encoding="utf-8")
            with patch.object(executor, "get_state", return_value={"workspace_root": str(root)}):
                result, error = executor.run_file_edit({
                    "path": "example.txt", "operation": "replace",
                    "old_text": "before", "new_text": "after",
                })
                self.assertFalse(error, result)
                self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
                _, outside_error = executor.run_file_read({"path": "../outside.txt"})
                self.assertTrue(outside_error)

    def test_directory_create_creates_nested_directory_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(executor, "get_state", return_value={"workspace_root": str(root)}):
                result, error = executor.run_directory_create({"path": "pac-man/assets"})
                self.assertFalse(error, result)
                self.assertTrue((root / "pac-man" / "assets").is_dir())
                result, error = executor.run_directory_create({"path": "pac-man/assets"})
                self.assertFalse(error, result)
                self.assertIn("already exists", result)

    def test_directory_create_rejects_existing_file_and_workspace_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pac-man").write_text("not a directory", encoding="utf-8")
            with patch.object(executor, "get_state", return_value={"workspace_root": str(root)}):
                result, error = executor.run_directory_create({"path": "pac-man"})
                self.assertTrue(error)
                self.assertIn("not a directory", result)
                _, outside_error = executor.run_directory_create({"path": "../outside"})
                self.assertTrue(outside_error)

    def test_directory_create_is_advertised_as_mutating_local_tool(self):
        names = {tool["name"] for tool in executor.LOCAL_TOOL_SCHEMAS}
        self.assertIn("directory_create", names)
        risk = assess_execution("directory_create", {"path": "pac-man"})
        self.assertTrue(risk.needs_approval)
        self.assertTrue(risk.mutation)

    def test_local_execution_tools_are_advertised(self):
        names = {tool["name"] for tool in executor.LOCAL_TOOL_SCHEMAS}
        self.assertTrue({"shell", "binary_exec", "task_runner"}.issubset(names))

    def test_file_read_rejects_binary_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "module.so").write_bytes(b"\x7fELF\x00binary")
            with patch.object(executor, "get_state", return_value={"workspace_root": str(root)}):
                result, error = executor.run_file_read({"path": "module.so"})
        self.assertTrue(error)
        self.assertIn("binary file", result)

    def test_read_tool_no_longer_accepts_a_command_string(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(executor, "get_state", return_value={"workspace_root": temp}):
                result, error = executor.run_file_read({"command": "find . -delete"})
        self.assertTrue(error)
        self.assertIn("path", result)


class TriForceHostBoundaryTests(unittest.TestCase):
    def test_direct_host_tool_mcp_call_is_blocked_before_network(self):
        client = TriForceClient("https://example.invalid")
        client.token = "opaque"
        client._request = MagicMock()
        with self.assertRaises(ClientError) as ctx:
            client.mcp_call("dev_refactor", {"path": "a.py"})
        self.assertIn("backend service", str(ctx.exception))
        client._request.assert_not_called()

    def test_backend_service_mcp_call_remains_available(self):
        client = TriForceClient("https://example.invalid")
        client.token = "opaque"
        client._request = MagicMock(return_value={
            "jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "ok"}]},
        })
        response = client.mcp_call("search", {"query": "hello"})
        self.assertIn("result", response)
        client._request.assert_called_once()

    def test_local_code_tool_rejects_legacy_remote_target_without_mcp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "x.py").write_text("print(1)\n", encoding="utf-8")
            client = MagicMock()
            result, is_error = executor.run_tool(
                client, "code_read", {"path": "x.py", "target": "remote"},
                workspace_root=root,
            )
            self.assertTrue(is_error)
            self.assertIn("remote code targets are disabled", result)
            client.mcp_call.assert_not_called()


class McpProtocolTests(unittest.TestCase):
    def test_login_normalizes_the_backend_account_role_contract(self):
        client = TriForceClient("https://example.invalid")
        with patch.object(
            client, "_request",
            return_value={"token": "opaque", "tier": "pro", "client_id": "client-1"},
        ):
            result = client.login("user@example.invalid", "secret")
        self.assertEqual(result["account_role"], "pro")

    def test_client_announces_the_restrictive_backend_profile(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with patch.object(client, "_do_request", return_value={}) as request:
            client._request("GET", "/v1/auth/verify", require_auth=True)
        headers = request.call_args.args[2]
        self.assertEqual(headers["X-Client-Profile"], CLIENT_PROFILE)

    def test_operator_mcp_scope_uses_authenticated_backend_catalog(self):
        self.assertIsNone(OPERATOR_MCP_TOOLS)
        self.assertEqual(INTERNAL_MCP_TOOLS, {"swarm_broadcast"})

    def test_client_blocks_local_only_tool_before_network(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with patch.object(client, "_request") as request:
            with self.assertRaises(ClientError):
                client.mcp_call("shell", {"command": "id"})
        request.assert_not_called()

    def test_client_blocks_backend_host_operator_tool_name(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with patch.object(client, "_request", return_value={"result": {}}) as request:
            with self.assertRaises(ClientError):
                client.mcp_call("service_control", {"action": "status"})
        request.assert_not_called()

    def test_mcp_call_has_no_automatic_http_retry(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with patch.object(client, "_request", return_value={"result": {}}) as request:
            client.mcp_call("health", {})
        self.assertEqual(request.call_args.kwargs["_retries"], 0)

    def test_json_rpc_error_is_never_reported_as_success(self):
        client = MagicMock()
        client.mcp_call.side_effect = ClientError('MCP failed: {"code": -32601}')
        result, error = executor.run_mcp_tool(client, "health", {})
        self.assertTrue(error)
        self.assertIn("TOOL FAILED", result)

    def test_mutating_mcp_timeout_is_not_retried(self):
        client = MagicMock()
        client.mcp_call.side_effect = ClientError("timeout after commit")
        executor.run_mcp_tool(client, "memory_store", {"key": "x"})
        self.assertEqual(client.mcp_call.call_count, 1)

    def test_all_text_blocks_and_structured_content_are_supported(self):
        client = MagicMock()
        client.mcp_call.return_value = {
            "result": {"content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}
        }
        self.assertEqual(executor.run_mcp_tool(client, "health", {}), ("one\ntwo", False))
        client.mcp_call.return_value = {"result": {"structuredContent": {"ok": True}}}
        text, error = executor.run_mcp_tool(client, "health", {})
        self.assertFalse(error)
        self.assertIn('"ok": true', text)

    def test_backend_annotations_drive_transport_independent_approval(self):
        read_only = executor._tool_security_metadata({
            "name": "future_read", "annotations": {"readOnlyHint": True},
        })
        mutating = executor._tool_security_metadata({
            "name": "future_write", "annotations": {"readOnlyHint": False},
        })
        self.assertEqual(read_only, (False, None))
        self.assertEqual(mutating, (True, None))


class ModelCatalogAuthFallbackTests(unittest.TestCase):
    def test_expired_local_token_uses_public_catalog_without_sending_auth(self):
        client = TriForceClient("https://example.invalid", token="expired.jwt.value")
        with (
            patch.object(client, "is_token_expired", return_value=True),
            patch.object(client, "_request", return_value={"tier": "guest", "models": []}) as request,
        ):
            result = client.model_catalog()
        self.assertEqual(result["tier"], "guest")
        request.assert_called_once_with(
            "GET", "/v1/client/models", require_auth=False, _label="models-public"
        )

    def test_server_rejected_session_retries_only_model_catalog_publicly(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with (
            patch.object(client, "is_token_expired", return_value=False),
            patch.object(
                client, "_request",
                side_effect=[TokenExpiredError("expired"), {"tier": "guest", "models": ["ollama/gemma4:cloud"]}],
            ) as request,
        ):
            result = client.model_catalog()
        self.assertEqual(result["models"], ["ollama/gemma4:cloud"] )
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["require_auth"], True)
        self.assertEqual(request.call_args_list[1].kwargs["require_auth"], False)



class FallbackAndSwarmTests(unittest.TestCase):
    def test_legacy_fallback_argument_does_not_hide_primary_failure(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with patch.object(client, "_request", side_effect=ClientError("timeout")) as request:
            with self.assertRaises(ClientError):
                client.chat(message="x", model="primary", fallback_model="fallback")
        self.assertEqual(request.call_count, 1)

    def test_auth_failure_does_not_try_fallback(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with patch.object(client, "_request", side_effect=TokenExpiredError("expired")) as request:
            with self.assertRaises(TokenExpiredError):
                client.chat(message="x", model="primary", fallback_model="fallback")
        self.assertEqual(request.call_count, 1)

    def test_expired_auth_retries_no_tools_ollama_chat_as_public_guest(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        success = {"response": "OK", "model": "ollama/gemma4:cloud", "backend": "ollama"}
        with patch.object(
            client, "_request", side_effect=[TokenExpiredError("expired"), success]
        ) as request:
            result = client.chat(message="x", model="ollama/gemma4:cloud")
        self.assertEqual(result["response"], "OK")
        self.assertEqual(request.call_count, 2)
        self.assertTrue(request.call_args_list[0].kwargs["require_auth"])
        self.assertFalse(request.call_args_list[1].kwargs["require_auth"])

    def test_expired_auth_does_not_downgrade_ollama_tool_chat_to_guest(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with patch.object(client, "_request", side_effect=TokenExpiredError("expired")) as request:
            with self.assertRaises(TokenExpiredError):
                client.chat(
                    message="x", model="ollama/gemma4:cloud",
                    tools=[{"name": "health", "inputSchema": {"type": "object"}}],
                )
        self.assertEqual(request.call_count, 1)



class AuditRedactionTests(unittest.TestCase):
    def test_nested_arguments_and_inline_secrets_are_redacted(self):
        sanitized = audit._sanitize_args("code_read", {
            "token": "abc",
            "nested": {"api_key": "def"},
            "command": "curl -H 'Authorization: Bearer-secret' example.invalid",
        })
        rendered = repr(sanitized)
        self.assertNotIn("abc", rendered)
        self.assertNotIn("def", rendered)
        self.assertNotIn("Bearer-secret", rendered)
        self.assertIn("REDACTED", rendered)


class LocalDataBoundaryTests(unittest.TestCase):
    def test_windows_clipboard_text_is_passed_via_stdin(self):
        completed = MagicMock(returncode=0, stdout="", stderr="")
        payload = "'; Remove-Item -Recurse C:\\\\important; '"
        with (
            patch.object(clipboard, "IS_WINDOWS", True),
            patch.object(clipboard.subprocess, "run", return_value=completed) as run,
        ):
            result, error = clipboard.clipboard_write(payload)
        self.assertFalse(error, result)
        self.assertEqual(run.call_args.kwargs["input"], payload)
        self.assertNotIn(payload, " ".join(run.call_args.args[0]))

    def test_web_fetch_rejects_localhost_before_opening(self):
        with patch.object(web_search, "build_opener") as opener:
            result, error = web_search.web_fetch("http://localhost/internal")
        self.assertTrue(error)
        self.assertIn("local/private", result)
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
