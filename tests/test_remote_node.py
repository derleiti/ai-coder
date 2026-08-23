from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aicoder.config import Session
from aicoder.remote_node import (
    REMOTE_CONTROL_TOOLS,
    REMOTE_MODEL_TOOLS,
    REMOTE_READ_TOOLS,
    REMOTE_TOOLS,
    REMOTE_WRITE_TOOLS,
    RemoteNode,
    execute_remote_read_tool,
    execute_remote_tool,
    websocket_node_url,
)


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, value: str):
        self.sent.append(json.loads(value))


class RemoteNodeTests(unittest.TestCase):
    def setUp(self):
        self._saved_active_workspace = os.environ.pop("AICODER_ACTIVE_WORKSPACE", None)

    def tearDown(self):
        if self._saved_active_workspace is not None:
            os.environ["AICODER_ACTIVE_WORKSPACE"] = self._saved_active_workspace
        else:
            os.environ.pop("AICODER_ACTIVE_WORKSPACE", None)

    def _session(self) -> Session:
        return Session(
            base_url="https://api.ailinux.me",
            token="token value",
            client_id="client-1",
            user_id="user@example.test",
            tier="pro",
            account_role="user",
        )

    def test_websocket_url_uses_existing_node_endpoint(self):
        session = self._session()
        url = websocket_node_url(session.base_url, session)
        self.assertTrue(url.startswith("wss://api.ailinux.me/v1/mcp/node/connect?"))
        self.assertIn("session_id=client-1", url)
        self.assertIn("token=token+value", url)

    def test_remote_profile_is_read_only_by_default(self):
        self.assertEqual(
            REMOTE_READ_TOOLS,
            {"client_file_read", "client_file_list", "client_codebase_search", "client_git_status"},
        )
        self.assertEqual(REMOTE_WRITE_TOOLS, {"client_file_edit"})
        blocked = execute_remote_read_tool(
            "client_file_edit",
            {"path": "x", "operation": "create", "content": "x"},
        )
        self.assertTrue(blocked["isError"])
        self.assertIn("read-only", blocked["content"][0]["text"])

    def test_file_read_is_confined_to_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text("hello remote", encoding="utf-8")
            with patch("aicoder.executor.get_state", return_value={"workspace_root": temp}):
                result = execute_remote_tool("client_file_read", {"path": "README.md"})
                escaped = execute_remote_tool("client_file_read", {"path": "../outside"})
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["text"], "hello remote")
        self.assertTrue(escaped["isError"])
        self.assertIn("outside the active workspace", escaped["content"][0]["text"])

    def test_write_preview_creates_new_file_but_refuses_blind_write(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as config:
            root = Path(temp)
            with (
                patch("aicoder.executor.get_state", return_value={"workspace_root": temp}),
                patch("aicoder.remote_node.CONFIG_DIR", Path(config)),
            ):
                created = execute_remote_tool(
                    "client_file_edit",
                    {"path": "new.txt", "operation": "create", "content": "created\n"},
                    allow_writes=True,
                )
                blind = execute_remote_tool(
                    "client_file_edit",
                    {"path": "new.txt", "operation": "write", "content": "overwrite\n"},
                    allow_writes=True,
                )
            self.assertFalse(created["isError"])
            self.assertEqual((root / "new.txt").read_text(), "created\n")
            self.assertIn("backup=none", created["content"][0]["text"])
            self.assertTrue(blind["isError"])
            self.assertIn("create or operation=replace", blind["content"][0]["text"])
            self.assertEqual((root / "new.txt").read_text(), "created\n")

    def test_exact_replace_backs_up_original_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as config:
            root = Path(temp)
            target = root / "src" / "app.py"
            target.parent.mkdir()
            target.write_text("before\nneedle\nafter\n", encoding="utf-8")
            with (
                patch("aicoder.executor.get_state", return_value={"workspace_root": temp}),
                patch("aicoder.remote_node.CONFIG_DIR", Path(config)),
            ):
                result = execute_remote_tool(
                    "client_file_edit",
                    {
                        "path": "src/app.py",
                        "operation": "replace",
                        "old_text": "needle",
                        "new_text": "replacement",
                    },
                    allow_writes=True,
                )
            self.assertFalse(result["isError"])
            self.assertEqual(target.read_text(), "before\nreplacement\nafter\n")
            backups = list((Path(config) / "backups" / "remote").rglob("app.py"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), "before\nneedle\nafter\n")
            self.assertFalse(str(backups[0]).startswith(str(root)))
            self.assertIn("verification_required=true", result["content"][0]["text"])

    def test_exact_replace_rejects_ambiguous_match_before_backup(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as config:
            root = Path(temp)
            target = root / "x.txt"
            target.write_text("same same", encoding="utf-8")
            with (
                patch("aicoder.executor.get_state", return_value={"workspace_root": temp}),
                patch("aicoder.remote_node.CONFIG_DIR", Path(config)),
            ):
                result = execute_remote_tool(
                    "client_file_edit",
                    {"path": "x.txt", "operation": "replace", "old_text": "same", "new_text": "new"},
                    allow_writes=True,
                )
            self.assertTrue(result["isError"])
            self.assertIn("exactly once", result["content"][0]["text"])
            self.assertEqual(target.read_text(), "same same")
            self.assertFalse((Path(config) / "backups").exists())

    def test_write_preview_identity_advertises_edit_only_when_opted_in(self):
        import asyncio

        session = self._session()
        read_node = RemoteNode(session, session.base_url, allow_writes=False)
        write_node = RemoteNode(session, session.base_url, allow_writes=True)
        read_ws = _FakeWebSocket()
        write_ws = _FakeWebSocket()
        state = {"workspace_root": "/tmp/example"}
        with patch("aicoder.remote_node.get_state", return_value=state):
            asyncio.run(read_node._send_identity(read_ws))
            asyncio.run(write_node._send_identity(write_ws))
        self.assertEqual(set(read_ws.sent[1]["params"]["tools"]), REMOTE_READ_TOOLS | REMOTE_CONTROL_TOOLS)
        self.assertEqual(set(write_ws.sent[1]["params"]["tools"]), REMOTE_MODEL_TOOLS | REMOTE_CONTROL_TOOLS)
        self.assertEqual(read_ws.sent[0]["params"]["remote_profile"], "read-only-light")
        self.assertEqual(write_ws.sent[0]["params"]["remote_profile"], "write-preview")

    def test_remote_plan_tracks_write_verify_and_completion(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as config:
            root = Path(temp)
            target = root / "demo.txt"
            target.write_text("before", encoding="utf-8")
            metadata = {"_run_id": "remote-test-1", "_task": "change demo", "_model": "test/model"}
            with (
                patch("aicoder.executor.get_state", return_value={"workspace_root": temp}),
                patch("aicoder.remote_node.CONFIG_DIR", Path(config)),
            ):
                edited = execute_remote_tool(
                    "client_file_edit",
                    {**metadata, "path": "demo.txt", "operation": "replace", "old_text": "before", "new_text": "after"},
                    allow_writes=True,
                )
                verified = execute_remote_tool("client_file_read", {**metadata, "path": "demo.txt"})
                completed = execute_remote_tool(
                    "client_run_state",
                    {**metadata, "status": "completed", "response": "DONE: verified"},
                    allow_writes=True,
                )
                from aicoder.agent_plan import PlanStore
                plan = PlanStore(Path(config) / "remote-plans").load(str(root), "remote-test-1")
            self.assertFalse(edited["isError"])
            self.assertFalse(verified["isError"])
            self.assertFalse(completed["isError"])
            self.assertIsNotNone(plan)
            self.assertEqual(plan.runtime, "remote-antigravity-light")
            self.assertEqual(plan.status, "completed")
            statuses = {step.id: step.status for step in plan.steps}
            self.assertEqual(statuses["implement"], "completed")
            self.assertEqual(statuses["verify"], "completed")
            self.assertEqual(plan.last_response, "DONE: verified")


if __name__ == "__main__":
    unittest.main()
