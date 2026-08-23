from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import NativeLightRuntime
from aicoder.executor import (MAX_CONTEXT_MESSAGES, LOCAL_FILE_READ_SCHEMA, LOCAL_SUBAGENT_SCHEMA, run_tool, trim_messages)


class _NativeTransport:
    def __init__(self):
        self.timeout = 300
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if len(self.calls) == 1:
            return {
                "response": "",
                "model": "openrouter/test/tool-model",
                "tool_calls": [{
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "file_read", "arguments": '{"path":"x.txt"}'},
                }],
            }
        return {"response": "DONE: read complete", "model": "openrouter/test/tool-model"}


class NativeContextTrimTests(unittest.TestCase):
    def test_trim_keeps_assistant_parent_when_window_starts_inside_tool_batch(self):
        prefix = [
            {"role": "system", "content": "system"},
            *({"role": "user", "content": f"old-{i}"} for i in range(MAX_CONTEXT_MESSAGES - 2)),
        ]
        assistant = {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "call_b", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ],
        }
        messages = prefix + [
            assistant,
            {"role": "tool", "tool_call_id": "call_a", "content": "A"},
            {"role": "tool", "tool_call_id": "call_b", "content": "B"},
            {"role": "user", "content": "continue"},
        ]
        trimmed = trim_messages(messages)
        first_tool = next(i for i, msg in enumerate(trimmed) if msg.get("role") == "tool")
        self.assertGreater(first_tool, 1)
        self.assertEqual(trimmed[first_tool - 1].get("role"), "assistant")
        self.assertTrue(trimmed[first_tool - 1].get("tool_calls"))


class NativeOpenRouterMessageTests(unittest.TestCase):
    def test_native_tool_result_uses_assistant_tool_calls_and_role_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "x.txt").write_text("alpha", encoding="utf-8")
            execution_client = MagicMock()
            execution_client.timeout = 300
            transport = _NativeTransport()
            tools = [LOCAL_FILE_READ_SCHEMA]
            runtime = NativeLightRuntime(
                client=execution_client,
                model_client=transport,
                initial_prompt="Read x.txt and report the result.",
                model="openrouter/test/tool-model",
                fallback_model=None,
                workspace_root=str(root),
                tools=tools,
                load_tools_on_start=True,
                persistent_plan=False,
                native_openrouter_tool_calling=True,
                base_timeout=300,
            )
            with (
                patch.object(runtime, "_tools_for_request", return_value=tools),
                patch("aicoder.agent_runtime.run_tool", return_value=("alpha", False)),
            ):
                result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(len(transport.calls), 2)
            messages = transport.calls[1]["messages"]
            assistant = next(msg for msg in messages if msg.get("role") == "assistant" and msg.get("tool_calls"))
            tool_msg = next(msg for msg in messages if msg.get("role") == "tool")
            self.assertEqual(assistant["tool_calls"][0]["id"], "call_123")
            self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "file_read")
            self.assertEqual(tool_msg["tool_call_id"], "call_123")
            self.assertEqual(tool_msg["name"], "file_read")
            self.assertEqual(tool_msg["content"], "alpha")
            self.assertFalse(any(
                msg.get("role") == "user" and "Tool file_read result" in str(msg.get("content") or "")
                for msg in messages
            ))


class ExecutorSubagentContextTests(unittest.TestCase):
    def test_debug_subagent_receives_parent_workspace_tools_and_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = MagicMock()
            client.timeout = 30
            approval = MagicMock(return_value=True)
            with (
                patch("aicoder.executor.load_tools", return_value=[LOCAL_SUBAGENT_SCHEMA, LOCAL_FILE_READ_SCHEMA]),
                patch("aicoder.subagents.run_subagent", return_value=("child ok", False)) as child,
            ):
                result, is_error = run_tool(
                    client,
                    "subagent_run",
                    {"task": "inspect x.txt", "role": "debug"},
                    approval_fn=approval,
                    model="test/model",
                    allowed_tools={"subagent_run", "file_read"},
                    workspace_root=root,
                )
            self.assertFalse(is_error, result)
            kwargs = child.call_args.kwargs
            self.assertIs(kwargs["execution_client"], client)
            self.assertEqual(kwargs["workspace_root"], str(root.resolve()))
            self.assertIs(kwargs["approval_fn"], approval)
            self.assertEqual([tool["name"] for tool in kwargs["tools"]], ["file_read"])
            self.assertEqual(kwargs["enabled_tool_names"], ["file_read", "subagent_run"])


class _EvidenceStore:
    instances: list["_EvidenceStore"] = []

    def __init__(self, workspace):
        self.workspace = str(workspace)
        self.inspections: list[tuple] = []
        self.__class__.instances.append(self)

    def health(self):
        return {}

    def inspect_file(self, *key):
        self.inspections.append(tuple(key))
        return "alpha", True

    def remember_failure(self, *args):
        return None


class EvidencePathNormalizationTests(unittest.TestCase):
    def test_equivalent_relative_paths_reuse_same_read_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.txt").write_text("alpha", encoding="utf-8")
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = [
                {"response": 'TOOL_CALL file_read\n{"path":"a.txt"}\nEND_TOOL_CALL', "model": "test/model"},
                {"response": 'TOOL_CALL file_read\n{"path":"./a.txt"}\nEND_TOOL_CALL', "model": "test/model"},
                {"response": "DONE: inspected", "model": "test/model"},
            ]
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Inspect a.txt twice using equivalent paths and report.",
                model="test/model",
                fallback_model=None,
                workspace_root=str(root),
                tools=[LOCAL_FILE_READ_SCHEMA],
                load_tools_on_start=False,
                persistent_plan=False,
                base_timeout=30,
            )
            _EvidenceStore.instances.clear()
            with (
                patch("aicoder.agent_runtime.ProjectEvidenceStore", _EvidenceStore),
                patch("aicoder.agent_runtime.run_tool", return_value=("alpha", False)) as execute,
            ):
                result = runtime.run()
            self.assertEqual(result.status, "completed")
            self.assertEqual(execute.call_count, 1)
            store = _EvidenceStore.instances[-1]
            normalized = str((root / "a.txt").resolve())
            self.assertTrue(any(row[0] == normalized for row in store.inspections))


if __name__ == "__main__":
    unittest.main()
