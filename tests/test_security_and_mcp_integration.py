from __future__ import annotations

import contextlib
import io
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
from aicoder.swarm_runner import run_swarm_ask
from aicoder.session_state import migrate_enabled_tools
from aicoder.tool_policy import (
    CODING_MCP_TOOLS,
    INTERNAL_MCP_TOOLS,
    filter_tool_catalog,
    require_allowed_tool,
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

    def test_forbidden_scopes_and_aliases_are_denied(self):
        for name in (
            "admin_users", "vault_keys", "mail_send", "notify_send",
            "restart_backend", "service_control", "remote_task", "shell",
            "task_runner", "binary_exec", "local_exec", "devops", "memory_clear",
            "remote.search", "admin:health", "mcp/vault.keys",
        ):
            with self.subTest(name=name):
                allowed, reason = require_allowed_tool(name, None)
                self.assertFalse(allowed)
                self.assertTrue(reason)

    def test_catalog_filter_is_fail_closed_for_malformed_and_forbidden_tools(self):
        catalog = [
            {"name": "code_read", "inputSchema": {}},
            {"name": "vault_keys", "inputSchema": {}},
            {"description": "missing name"},
            "invalid",
        ]
        self.assertEqual(
            filter_tool_catalog(catalog, {"code_read", "vault_keys"}),
            [{"name": "code_read", "inputSchema": {}}],
        )

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

    def test_read_tool_no_longer_accepts_a_command_string(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(executor, "get_state", return_value={"workspace_root": temp}):
                result, error = executor.run_file_read({"command": "find . -delete"})
        self.assertTrue(error)
        self.assertIn("path", result)


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

    def test_frontend_mcp_contract_matches_backend_canonical_profile(self):
        expected = {
            "code_read", "code_search", "code_tree",
            "dev_analyze", "dev_debug", "dev_lint", "dev_links",
            "dev_refactor", "dev_summarize",
            "doc_read", "doc_search",
            "health", "search", "crawl",
            "memory_search", "memory_store",
            "models", "specialist", "prompts", "swarm_broadcast",
        }
        self.assertEqual(set(CODING_MCP_TOOLS) | set(INTERNAL_MCP_TOOLS), expected)

    def test_client_blocks_forbidden_tool_before_network(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with patch.object(client, "_request") as request:
            with self.assertRaises(ClientError):
                client.mcp_call("shell", {"command": "id"})
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


class FallbackAndSwarmTests(unittest.TestCase):
    def test_fallback_result_is_marked_locally(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with patch.object(
            client, "_request",
            side_effect=[ClientError("timeout"), {"response": "ok", "model": "fallback"}],
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                result = client.chat(message="x", model="primary", fallback_model="fallback")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["primary_model"], "primary")

    def test_auth_failure_does_not_try_fallback(self):
        client = TriForceClient("https://example.invalid", token="opaque")
        with patch.object(client, "_request", side_effect=TokenExpiredError("expired")) as request:
            with self.assertRaises(TokenExpiredError):
                client.chat(message="x", model="primary", fallback_model="fallback")
        self.assertEqual(request.call_count, 1)

    def test_swarm_without_fallback_does_not_duplicate_operator(self):
        client = MagicMock()
        client.chat.return_value = {"response": "operator", "model": "primary"}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_swarm_ask("x", "primary", None, None, "on", client), 0)
        self.assertEqual(client.chat.call_count, 1)

    def test_review_receives_operator_response(self):
        client = MagicMock()
        client.chat.side_effect = [
            {"response": "operator answer", "model": "primary"},
            {"response": "review", "model": "advisor"},
        ]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_swarm_ask("request", "primary", "advisor", None, "review", client), 0)
        self.assertIn("operator answer", client.chat.call_args_list[1].kwargs["message"])


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
