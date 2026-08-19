from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_plan import PlanStore
from aicoder.agent_runtime import NativeLightRuntime
from aicoder.executor import (
    LOCAL_FILE_EDIT_SCHEMA,
    LOCAL_FILE_READ_SCHEMA,
    LOCAL_SUBAGENT_SCHEMA,
    run_tool,
)
from aicoder.subagents import MAX_SUBAGENT_CONTEXT, MAX_SUBAGENT_TASK, run_subagent


class _RecordingTransport:
    def __init__(self):
        self.timeout = 30
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "response": '<tool_call>{"name":"file_edit","arguments":{"path":"owned.txt","operation":"write","content":"bad"}}</tool_call>\nAdvisory only.',
            "model": kwargs.get("model") or "test/model",
        }


class _RuntimeTransport:
    def __init__(self):
        self.timeout = 30
        self.calls: list[dict] = []
        self.main_calls = 0

    def chat(self, **kwargs):
        self.calls.append(dict(kwargs))
        if kwargs.get("tools") is None:
            return {
                "response": '<tool_call>{"name":"file_edit","arguments":{"path":"owned.txt","operation":"write","content":"bad"}}</tool_call>\nPotential issue found.',
                "model": "test/model",
            }
        self.main_calls += 1
        if self.main_calls == 1:
            return {
                "response": '<tool_call>{"name":"subagent_run","arguments":{"role":"review","task":"Review this approach","context":"candidate patch"}}</tool_call>',
                "model": "test/model",
            }
        return {"response": "DONE: parent kept execution ownership", "model": "test/model"}


class _ResumeTransport:
    def __init__(self):
        self.timeout = 30
        self.main_calls = 0

    def chat(self, **kwargs):
        if kwargs.get("tools") is None:
            return {"response": "Advisory: consider another write.", "model": "test/model"}
        self.main_calls += 1
        if self.main_calls == 1:
            return {
                "response": '<tool_call>{"name":"subagent_run","arguments":{"task":"Think about next edit","role":"analyze"}}</tool_call>',
                "model": "test/model",
            }
        if self.main_calls == 2:
            return {
                "response": '<tool_call>{"name":"file_edit","arguments":{"path":"x.txt","operation":"write","content":"again"}}</tool_call>',
                "model": "test/model",
            }
        if self.main_calls == 3:
            return {
                "response": '<tool_call>{"name":"file_read","arguments":{"path":"x.txt"}}</tool_call>',
                "model": "test/model",
            }
        return {"response": "DONE: verified", "model": "test/model"}


class SubagentUnitTests(unittest.TestCase):
    def test_subagent_receives_no_tools_and_tool_markup_remains_text(self):
        transport = _RecordingTransport()
        result, is_error = run_subagent(
            transport,
            task="Review a parser change",
            role="review",
            context="diff text",
            model="test/model",
        )
        self.assertFalse(is_error)
        self.assertIn("<tool_call>", result)
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertIsNone(call["tools"])
        self.assertEqual(call["tool_choice"], "none")
        self.assertIn("cannot execute tools", call["system_prompt"])
        self.assertIn("Review a parser change", call["message"])

    def test_subagent_rejects_bad_role_and_empty_task_without_model_call(self):
        transport = MagicMock()
        result, is_error = run_subagent(transport, task="x", role="executor")
        self.assertTrue(is_error)
        self.assertIn("unsupported role", result)
        result, is_error = run_subagent(transport, task="", role="review")
        self.assertTrue(is_error)
        self.assertIn("task is required", result)
        transport.chat.assert_not_called()

    def test_subagent_bounds_task_and_context(self):
        transport = _RecordingTransport()
        run_subagent(
            transport,
            task="T" * (MAX_SUBAGENT_TASK + 500),
            context="C" * (MAX_SUBAGENT_CONTEXT + 500),
            model="test/model",
        )
        message = transport.calls[0]["message"]
        self.assertNotIn("T" * (MAX_SUBAGENT_TASK + 1), message)
        self.assertNotIn("C" * (MAX_SUBAGENT_CONTEXT + 1), message)

    def test_classic_executor_subagent_is_advisory_only(self):
        client = MagicMock()
        client.chat.return_value = {
            "response": '<tool_call>{"name":"file_edit","arguments":{"path":"x","operation":"write"}}</tool_call>',
            "model": "test/model",
        }
        result, is_error = run_tool(
            client,
            "subagent_run",
            {"task": "review", "role": "review"},
            model="test/model",
            allowed_tools={"subagent_run"},
        )
        self.assertFalse(is_error)
        self.assertIn("<tool_call>", result)
        kwargs = client.chat.call_args.kwargs
        self.assertIsNone(kwargs["tools"])
        self.assertEqual(kwargs["tool_choice"], "none")


class SubagentRuntimeTests(unittest.TestCase):
    def test_native_runtime_uses_model_transport_and_does_not_execute_subagent_tool_markup(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            client = MagicMock()
            client.timeout = 30
            transport = _RuntimeTransport()
            runtime = NativeLightRuntime(
                client=client,
                model_client=transport,
                initial_prompt="Review then finish",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[LOCAL_SUBAGENT_SCHEMA],
                load_tools_on_start=True,
                plan_store=PlanStore(Path(temp) / "plans"),
                base_timeout=30,
            )
            with patch("aicoder.agent_runtime.run_tool") as local_run:
                result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.response, "DONE: parent kept execution ownership")
            local_run.assert_not_called()
            self.assertEqual(len(transport.calls), 3)
            advisory = transport.calls[1]
            self.assertIsNone(advisory["tools"])
            self.assertEqual(advisory["tool_choice"], "none")
            self.assertTrue(any(
                "Potential issue found" in str(message.get("content", ""))
                for message in result.messages
            ))

    def test_subagent_does_not_count_as_fresh_workspace_inspection_after_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            plan = store.create("Finish prior edit", str(workspace), "test/model")
            plan.status = "paused"
            plan.set_step("inspect", "completed", "prior inspection")
            plan.set_step("implement", "completed", "prior write")
            plan.set_step("verify", "in_progress", "pending")
            store.save(plan)

            client = MagicMock()
            client.timeout = 30
            transport = _ResumeTransport()
            runtime = NativeLightRuntime(
                client=client,
                model_client=transport,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[LOCAL_SUBAGENT_SCHEMA, LOCAL_FILE_EDIT_SCHEMA, LOCAL_FILE_READ_SCHEMA],
                load_tools_on_start=True,
                approval_fn=lambda _name, _args: True,
                plan_store=store,
                resume=True,
                base_timeout=30,
            )
            with patch("aicoder.agent_runtime.run_tool", return_value=("current state", False)) as local_run:
                result = runtime.run()

            self.assertEqual(result.status, "completed")
            executed = [call.args[1] for call in local_run.call_args_list]
            self.assertEqual(executed, ["file_read"])
            self.assertTrue(any(
                "require a fresh successful read/check" in str(message.get("content", ""))
                for message in result.messages
            ))


if __name__ == "__main__":
    unittest.main()
