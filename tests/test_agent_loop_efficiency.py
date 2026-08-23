from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import NativeLightRuntime, _needs_completion_audit
from aicoder.executor import adaptive_request_timeout


class AdaptiveContinuationTimeoutTests(unittest.TestCase):
    def test_continuation_uses_configured_idle_timeout(self):
        prompt = "vollstaendiger integration test " * 200
        self.assertEqual(
            adaptive_request_timeout(
                150, prompt=prompt, iteration=20,
                model="openrouter/qwen/qwen3.8-27b", continuation=True,
            ),
            150,
        )

    def test_reasoning_model_uses_same_configured_idle_timeout(self):
        self.assertEqual(
            adaptive_request_timeout(
                150, prompt="tool result", iteration=20,
                model="provider/reasoning-model", continuation=True,
            ),
            150,
        )

    def test_named_reasoning_models_do_not_override_idle_timeout(self):
        for model in (
            "deepseek/deepseek-r1", "openai/o3-mini", "openai/o1",
            "anthropic/claude-3.7-sonnet", "qwen/qwq-32b",
        ):
            with self.subTest(model=model):
                self.assertEqual(
                    adaptive_request_timeout(
                        150, prompt="tool result", iteration=2,
                        model=model, continuation=True,
                    ),
                    150,
                )

    def test_first_turn_keeps_configured_budget(self):
        self.assertEqual(
            adaptive_request_timeout(
                150, prompt="large repository build", iteration=0,
                model="openrouter/qwen/qwen3.8-27b", continuation=False,
            ),
            150,
        )


class CompletionAuditTests(unittest.TestCase):
    def test_structured_tasks_require_audit_but_simple_tasks_do_not(self):
        structured = "- create A\n- verify A\n- remove A\n"
        self.assertTrue(_needs_completion_audit(structured))
        self.assertFalse(_needs_completion_audit("fix one typo"))

    def test_read_only_structured_task_does_not_add_completion_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "probe.txt").write_text("alpha", encoding="utf-8")
            client = MagicMock()
            client.timeout = 300
            client.chat.side_effect = [
                {
                    "response": 'TOOL_CALL file_read\n{"path":"probe.txt"}\nEND_TOOL_CALL',
                    "model": "openrouter/qwen/qwen3.8-27b",
                },
                {"response": "DONE: premature", "model": "openrouter/qwen/qwen3.8-27b"},
                {"response": "DONE: audited", "model": "openrouter/qwen/qwen3.8-27b"},
            ]
            events: list[tuple[str, dict]] = []
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="- inspect probe\n- verify result\n- report completion\n",
                model="openrouter/qwen/qwen3.8-27b",
                fallback_model=None,
                workspace_root=str(root),
                tools=[{
                    "name": "file_read",
                    "description": "read file",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }],
                load_tools_on_start=False,
                persistent_plan=False,
                base_timeout=300,
                event_fn=lambda kind, payload: events.append((kind, payload)),
            )
            with patch("aicoder.agent_runtime.run_tool", return_value=("alpha", False)):
                result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.response, "DONE: premature")
            self.assertEqual(client.chat.call_count, 2)
            self.assertEqual(sum(kind == "completion_audit" for kind, _ in events), 0)
            starts = [payload for kind, payload in events if kind == "model_start"]
            self.assertEqual(starts[0]["phase"], "planning")
            self.assertEqual(starts[0]["timeout"], 300)
            self.assertEqual(starts[1]["phase"], "continuation")
            self.assertEqual(starts[1]["timeout"], 300)


class FrontierAuditRuntimeTests(unittest.TestCase):
    def test_successful_provider_fallback_is_promoted_for_followup_turn(self):
        client = MagicMock()
        client.timeout = 300
        client.chat.side_effect = [
            {
                "response": 'TOOL_CALL file_read\n{"path":"probe.txt"}\nEND_TOOL_CALL',
                "model": "provider/fallback",
                "fallback_used": True,
            },
            {"response": "DONE: fallback continued", "model": "provider/fallback"},
        ]
        runtime = NativeLightRuntime(
            client=client, initial_prompt="inspect probe.txt", model="provider/primary",
            fallback_model="provider/fallback", workspace_root="/tmp",
            tools=[{
                "name": "file_read", "description": "read file",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            }], load_tools_on_start=False, persistent_plan=False, base_timeout=300,
        )
        with patch("aicoder.agent_runtime.run_tool", return_value=("alpha", False)):
            result = runtime.run()
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.fallback_used)
        self.assertEqual(client.chat.call_count, 2)
        second = client.chat.call_args_list[1].kwargs
        self.assertEqual(second["model"], "provider/fallback")
        self.assertIsNone(second["fallback_model"])

    def test_done_with_final_tool_call_still_runs_completion_audit(self):
        client = MagicMock()
        client.timeout = 300
        client.chat.side_effect = [
            {
                "response": 'DONE: wrote file\nTOOL_CALL file_edit\n{"path":"x.txt","operation":"write","content":"ok"}\nEND_TOOL_CALL',
                "model": "test/model",
            },
            {"response": "DONE: audited", "model": "test/model"},
        ]
        events = []
        runtime = NativeLightRuntime(
            client=client,
            initial_prompt="- write x.txt\n- verify x.txt\n- report completion\n",
            model="test/model", fallback_model=None, workspace_root="/tmp",
            tools=[{
                "name": "file_edit", "description": "edit file",
                "inputSchema": {"type": "object", "properties": {
                    "path": {"type": "string"}, "operation": {"type": "string"}, "content": {"type": "string"}
                }, "required": ["path", "operation"]},
            }], load_tools_on_start=False, persistent_plan=False, base_timeout=300,
            event_fn=lambda kind, payload: events.append((kind, payload)),
        )
        with patch(
            "aicoder.agent_runtime.run_tool",
            return_value=("updated x.txt; verified exact content (2 chars)", False),
        ):
            result = runtime.run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.response, "DONE: audited")
        self.assertEqual(client.chat.call_count, 2)
        self.assertEqual(sum(kind == "completion_audit" for kind, _ in events), 1)


class PlanVerificationSemanticsTests(unittest.TestCase):
    def test_deterministic_write_result_completes_verification(self):
        from aicoder.agent_plan import PlanStore
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = PlanStore(root / "plans")
            plan = store.create("change file", str(root), "test/model")
            runtime = NativeLightRuntime(
                client=MagicMock(), initial_prompt="change file", model="test/model",
                fallback_model=None, workspace_root=str(root), plan_store=store,
            )
            mutation, verified = runtime._record_tool_progress(
                plan, "file_edit",
                {"path": "x.txt", "operation": "create", "content": "alpha"},
                "updated x.txt; verified exact content (5 chars)", False, False,
            )
            self.assertTrue(mutation)
            self.assertTrue(verified)
            self.assertEqual(next(x.status for x in plan.steps if x.id == "verify"), "completed")

    def test_config_write_readback_counts_as_artifact_verification(self):
        from aicoder.agent_plan import PlanStore
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = PlanStore(root / "plans")
            plan = store.create("change config", str(root), "test/model")
            runtime = NativeLightRuntime(
                client=MagicMock(), initial_prompt="change config", model="test/model",
                fallback_model=None, workspace_root=str(root), plan_store=store,
            )
            mutation, verified = runtime._record_tool_progress(
                plan, "file_edit",
                {"path": "settings.json", "operation": "write", "content": '{"port":8080}'},
                "updated settings.json; verified exact content (13 chars)", False, False,
            )
            self.assertTrue(mutation)
            self.assertTrue(verified)
            self.assertEqual(next(x.status for x in plan.steps if x.id == "verify"), "completed")

    def test_code_write_readback_does_not_replace_behavior_verification(self):
        from aicoder.agent_plan import PlanStore
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = PlanStore(root / "plans")
            plan = store.create("change code behavior", str(root), "test/model")
            runtime = NativeLightRuntime(
                client=MagicMock(), initial_prompt="change code behavior", model="test/model",
                fallback_model=None, workspace_root=str(root), plan_store=store,
            )
            mutation, verified = runtime._record_tool_progress(
                plan, "file_edit",
                {"path": "x.py", "operation": "write", "content": "print(1)"},
                "updated x.py; verified exact content (8 chars)", False, False,
            )
            self.assertTrue(mutation)
            self.assertFalse(verified)
            mutation, verified = runtime._record_tool_progress(
                plan, "file_read", {"path": "x.py"}, "print(1)", False, mutation,
            )
            self.assertFalse(verified)
            self.assertEqual(next(x.status for x in plan.steps if x.id == "verify"), "in_progress")


class PlanIndependentProgressTests(unittest.TestCase):
    def test_classic_runtime_tracks_mutation_without_persistent_plan(self):
        runtime = NativeLightRuntime(
            client=MagicMock(), initial_prompt="change code", model="test/model",
            fallback_model=None, workspace_root="/tmp", persistent_plan=False,
        )
        mutation, verified = runtime._record_tool_progress(
            None, "file_edit",
            {"path": "x.py", "operation": "write", "content": "print(1)"},
            "updated x.py; verified exact content (8 chars)", False, False,
        )
        self.assertTrue(mutation)
        self.assertFalse(verified)
        mutation, verified = runtime._record_tool_progress(
            None, "test", {"command": "python3 -m unittest"}, "OK", False, mutation,
        )
        self.assertTrue(mutation)
        self.assertTrue(verified)

    def test_verification_command_is_not_itself_an_implementation_mutation(self):
        runtime = NativeLightRuntime(
            client=MagicMock(), initial_prompt="run tests", model="test/model",
            fallback_model=None, workspace_root="/tmp", persistent_plan=False,
        )
        mutation, verified = runtime._record_tool_progress(
            None, "test", {"command": "python3 -m unittest"}, "OK", False, False,
        )
        self.assertFalse(mutation)
        self.assertFalse(verified)

    def test_mutating_shell_does_not_self_verify(self):
        runtime = NativeLightRuntime(
            client=MagicMock(), initial_prompt="move file", model="test/model",
            fallback_model=None, workspace_root="/tmp", persistent_plan=False,
        )
        mutation, verified = runtime._record_tool_progress(
            None, "shell", {"command": "mv a.txt b.txt"}, "", False, False,
        )
        self.assertTrue(mutation)
        self.assertFalse(verified)

    def test_read_only_shell_does_not_create_mutation_progress(self):
        runtime = NativeLightRuntime(
            client=MagicMock(), initial_prompt="inspect git", model="test/model",
            fallback_model=None, workspace_root="/tmp", persistent_plan=False,
        )
        mutation, verified = runtime._record_tool_progress(
            None, "shell", {"command": "git status --short"}, "", False, False,
        )
        self.assertFalse(mutation)
        self.assertFalse(verified)

    def test_read_only_binary_exec_does_not_create_mutation_progress(self):
        runtime = NativeLightRuntime(
            client=MagicMock(), initial_prompt="inspect python", model="test/model",
            fallback_model=None, workspace_root="/tmp", persistent_plan=False,
        )
        mutation, verified = runtime._record_tool_progress(
            None, "binary_exec",
            {"program": "python", "arguments": ["--version"]},
            "Python 3", False, False,
        )
        self.assertFalse(mutation)
        self.assertFalse(verified)

    def test_mutating_binary_exec_creates_mutation_progress(self):
        runtime = NativeLightRuntime(
            client=MagicMock(), initial_prompt="move file", model="test/model",
            fallback_model=None, workspace_root="/tmp", persistent_plan=False,
        )
        mutation, verified = runtime._record_tool_progress(
            None, "binary_exec",
            {"program": "mv", "arguments": ["a.txt", "b.txt"]},
            "", False, False,
        )
        self.assertTrue(mutation)
        self.assertFalse(verified)


class ToolResultHandoffTests(unittest.TestCase):
    def test_empty_file_tree_result_is_present_in_next_model_request(self):
        client = MagicMock()
        client.timeout = 150
        client.chat.side_effect = [
            {"response": 'TOOL_CALL file_tree\n{"path":"."}\nEND_TOOL_CALL', "model": "openrouter/qwen/qwen3.8-27b"},
            {"response": "DONE: saw empty workspace", "model": "openrouter/qwen/qwen3.8-27b"},
        ]
        runtime = NativeLightRuntime(
            client=client, initial_prompt="inspect and report",
            model="openrouter/qwen/qwen3.8-27b", fallback_model=None,
            workspace_root="/tmp",
            tools=[{"name":"file_tree","inputSchema":{"type":"object","properties":{"path":{"type":"string"}}}}],
            load_tools_on_start=False, persistent_plan=False, base_timeout=150,
        )
        with patch("aicoder.agent_runtime.run_tool", return_value=("(empty directory)", False)):
            result = runtime.run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(client.chat.call_count, 2)
        second_messages = client.chat.call_args_list[1].kwargs["messages"]
        joined = "\n".join(str(m.get("content", "")) for m in second_messages)
        self.assertIn("Tool file_tree result:\n(empty directory)", joined)


class DuplicateRecoveryRegressionTests(unittest.TestCase):
    def test_escaped_markdown_requirements_are_structured(self):
        escaped = "1\\. inspect\n2\\. mutate\n3\\. verify\n\\* cleanup\n"
        self.assertTrue(_needs_completion_audit(escaped))

    def test_duplicate_then_mixed_tool_protocol_repairs_and_continues(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = MagicMock()
            client.timeout = 300
            first = 'TOOL_CALL file_tree\n{"path":"."}\nEND_TOOL_CALL'
            mixed = (
                "The directory is empty; continuing now.\n\n"
                'TOOL_CALL directory_create\n{"path":"sub"}\nEND_TOOL_CALL\n'
                'TOOL_CALL file_edit\n{"path":"alpha.txt","operation":"create","content":"alpha"}\nEND_TOOL_CALL'
            )
            client.chat.side_effect = [
                {"response": first, "model": "openrouter/qwen/qwen3.8-27b"},
                {"response": first, "model": "openrouter/qwen/qwen3.8-27b"},
                {"response": mixed, "model": "openrouter/qwen/qwen3.8-27b"},
                {"response": "DONE: work complete", "model": "openrouter/qwen/qwen3.8-27b"},
                {"response": "DONE: audited", "model": "openrouter/qwen/qwen3.8-27b"},
            ]
            events: list[tuple[str, dict]] = []
            tools = [
                {"name": "file_tree", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
                {"name": "directory_create", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
                {"name": "file_edit", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "operation": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "operation"]}},
            ]
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="1\\. inspect\n2\\. create directory\n3\\. create file\n\\* verify\n",
                model="openrouter/qwen/qwen3.8-27b",
                fallback_model=None,
                workspace_root=str(root),
                tools=tools,
                load_tools_on_start=False,
                persistent_plan=False,
                base_timeout=300,
                event_fn=lambda kind, payload: events.append((kind, payload)),
            )
            executed: list[str] = []
            def fake_run(_client, name, args, **kwargs):
                executed.append(name)
                if name == "file_tree":
                    return "(empty directory)", False
                if name == "directory_create":
                    return "created directory sub; verified directory exists", False
                if name == "file_edit":
                    return "updated alpha.txt; verified exact content (5 chars)", False
                return "ok", False

            with patch("aicoder.agent_runtime.run_tool", side_effect=fake_run):
                result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.response, "DONE: audited")
            self.assertEqual(executed.count("file_tree"), 1, "duplicate read must be blocked before execution")
            self.assertEqual(executed.count("directory_create"), 1)
            self.assertEqual(executed.count("file_edit"), 1)
            self.assertEqual(sum(kind == "loop_prevented" for kind, _ in events), 1)
            repairs = [payload for kind, payload in events if kind == "final_response_repair"]
            self.assertEqual(repairs, [])
            self.assertTrue(any(kind == "thought" and "directory is empty" in str(payload.get("text", "")).lower()
                                for kind, payload in events))
            self.assertEqual(sum(kind == "completion_audit" for kind, _ in events), 1)


if __name__ == "__main__":
    unittest.main()


class PollingDuplicateGuardTests(unittest.TestCase):
    def test_explicit_polling_allows_bounded_repeated_read_only_calls(self):
        client = MagicMock(); client.timeout = 30
        call = 'TOOL_CALL file_read\n{"path":"status.txt"}\nEND_TOOL_CALL'
        client.chat.side_effect = [
            {"response": call, "model": "test/model"},
            {"response": call, "model": "test/model"},
            {"response": "DONE: changed", "model": "test/model"},
        ]
        runtime = NativeLightRuntime(
            client=client, initial_prompt="Monitor status.txt and check again for a change",
            model="test/model", fallback_model=None, workspace_root="/tmp",
            tools=[{"name":"file_read","description":"read","inputSchema":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}],
            load_tools_on_start=False, persistent_plan=False, base_timeout=30,
        )
        with patch("aicoder.agent_runtime.run_tool", side_effect=[("old", False), ("new", False)]) as run:
            result = runtime.run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(run.call_count, 2)
