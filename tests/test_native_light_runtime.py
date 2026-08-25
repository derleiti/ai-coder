from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from aicoder.agent_plan import PlanStore
from aicoder.agent_runtime import NativeLightRuntime, _completion_audit_prompt, _is_behavior_verification_call, auto_resumable_pause, auto_resume_limit, auto_resume_prompt
from aicoder.executor import LOCAL_FILE_EDIT_SCHEMA, LOCAL_FILE_READ_SCHEMA, LOCAL_TEST_SCHEMA
from aicoder.gui.chat_widget import _AgentWorker


class NativeLightPlanTests(unittest.TestCase):
    def test_compatibility_runtime_does_not_create_persistent_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            client = MagicMock()
            client.timeout = 30
            client.chat.return_value = {"response": "DONE: classic", "model": "test/model"}
            events = []
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Explain current state",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                persistent_plan=False,
                base_timeout=30,
                event_fn=lambda kind, payload: events.append((kind, payload)),
            )

            result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertFalse(result.plan_id)
            self.assertIsNone(store.load_current(str(workspace)))
            runtime_events = [payload for kind, payload in events if kind == "runtime_status"]
            self.assertTrue(runtime_events)
            self.assertTrue(all(item.get("runtime_mode") == "classic" for item in runtime_events))
            self.assertEqual(runtime_events[0]["phase"], "bootstrap")
            self.assertEqual(runtime_events[-1]["status"], "completed")


    def test_native_light_runtime_emits_same_runtime_status_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            client = MagicMock()
            client.timeout = 30
            client.chat.return_value = {"response": "DONE: native", "model": "test/model"}
            events = []
            runtime = NativeLightRuntime(
                client=client, initial_prompt="Explain current state", model="test/model",
                fallback_model=None, workspace_root=str(workspace), tools=[],
                load_tools_on_start=False, plan_store=store, persistent_plan=True,
                base_timeout=30, event_fn=lambda kind, payload: events.append((kind, payload)),
            )

            result = runtime.run()

            self.assertEqual(result.status, "completed")
            runtime_events = [payload for kind, payload in events if kind == "runtime_status"]
            self.assertTrue(runtime_events)
            self.assertTrue(all(item.get("runtime_mode") == "native-light" for item in runtime_events))
            self.assertEqual(runtime_events[0]["phase"], "bootstrap")
            self.assertEqual(runtime_events[-1]["status"], "completed")

    def test_plan_store_persists_current_plan_per_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            plan = store.create("Fix the parser", str(workspace), "test/model")
            plan.iteration = 3
            plan.status = "paused"
            plan.pause_reason = "needs another pass"
            store.save(plan)

            loaded = store.load_current(str(workspace))
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.id, plan.id)
            self.assertEqual(loaded.iteration, 3)
            self.assertEqual(loaded.status, "paused")
            self.assertEqual(loaded.pause_reason, "needs another pass")
            self.assertEqual([step.id for step in loaded.steps], ["inspect", "implement", "verify"])

    def test_runtime_records_mutation_and_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = [
                {
                    "response": '<tool_call>{"name":"file_edit","arguments":{"path":"x.txt","operation":"write","content":"x"}}</tool_call>',
                    "model": "test/model",
                },
                {
                    "response": '<tool_call>{"name":"test","arguments":{"command":"python3 -m unittest"}}</tool_call>',
                    "model": "test/model",
                },
                {"response": "DONE: verified", "model": "test/model"},
            ]
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Fix and test x.txt",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[LOCAL_FILE_EDIT_SCHEMA, LOCAL_TEST_SCHEMA],
                load_tools_on_start=True,
                approval_fn=lambda _name, _args: True,
                plan_store=store,
                base_timeout=30,
            )
            with patch("aicoder.agent_runtime.run_tool", return_value=("ok", False)) as run_tool:
                result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.response, "DONE: verified")
            self.assertEqual(run_tool.call_count, 2)
            plan = store.load_current(str(workspace))
            self.assertEqual(plan.id, result.plan_id)
            self.assertEqual(plan.status, "completed")
            statuses = {step.id: step.status for step in plan.steps}
            self.assertEqual(statuses["inspect"], "completed")
            self.assertEqual(statuses["implement"], "completed")
            self.assertEqual(statuses["verify"], "completed")

    def test_file_read_after_mutation_does_not_count_as_behavior_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            plan = store.create("Change behavior", str(workspace), "test/model")
            runtime = NativeLightRuntime(
                client=MagicMock(), initial_prompt="x", model="test/model",
                fallback_model=None, workspace_root=str(workspace), plan_store=store,
            )
            mutated, verified = runtime._record_tool_progress(
                plan, "file_edit", {"path": "x.py", "operation": "write"},
                "updated x.py", False, False,
            )
            self.assertTrue(mutated)
            mutated, verified = runtime._record_tool_progress(
                plan, "file_read", {"path": "x.py"}, "print(1)", False, mutated,
            )
            self.assertTrue(mutated)
            self.assertFalse(verified)
            loaded = store.load(str(workspace), plan.id)
            self.assertEqual(
                next(step.status for step in loaded.steps if step.id == "verify"),
                "in_progress",
            )

    def test_user_rejected_tool_pauses_run_immediately(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            client = MagicMock()
            client.timeout = 30
            client.chat.return_value = {
                "response": '<tool_call>{"name":"file_edit","arguments":{"path":"x.txt","operation":"write","content":"x"}}</tool_call>',
                "model": "test/model",
            }
            runtime = NativeLightRuntime(
                client=client, initial_prompt="write x", model="test/model",
                fallback_model=None, workspace_root=str(workspace),
                tools=[LOCAL_FILE_EDIT_SCHEMA], load_tools_on_start=True,
                approval_fn=lambda _name, _args: False, persistent_plan=False,
                base_timeout=30,
            )

            result = runtime.run()

            self.assertEqual(result.status, "paused")
            self.assertIn("user rejected file_edit", result.response)
            self.assertEqual(client.chat.call_count, 1)

    def test_runtime_pauses_instead_of_claiming_done_without_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = [
                {
                    "response": '<tool_call>{"name":"file_edit","arguments":{"path":"x.txt","operation":"write","content":"x"}}</tool_call>',
                    "model": "test/model",
                },
                {"response": "DONE: changed", "model": "test/model"},
                {"response": "DONE: still changed", "model": "test/model"},
            ]
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="Change x.txt",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[LOCAL_FILE_EDIT_SCHEMA, LOCAL_TEST_SCHEMA],
                load_tools_on_start=True,
                approval_fn=lambda _name, _args: True,
                plan_store=store,
                base_timeout=30,
            )
            with patch("aicoder.agent_runtime.run_tool", return_value=("changed", False)):
                result = runtime.run()

            self.assertEqual(result.status, "paused")
            self.assertIn("verification", result.response.lower())
            self.assertEqual(client.chat.call_count, 3)
            self.assertTrue(any(
                "Verification required" in str(message.get("content", ""))
                for message in result.messages
            ))
            plan = store.load_current(str(workspace))
            self.assertEqual(plan.status, "paused")
            self.assertEqual(
                next(step.status for step in plan.steps if step.id == "verify"),
                "in_progress",
            )

    def test_complete_json_in_unclosed_tool_tag_is_not_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = [
                {"response": '<tool_call>{"name":"file_read","arguments":{"path":"README.md"}}', "model": "test/model"},
                {"response": 'TOOL_CALL file_read\n{"path":"README.md"}\nEND_TOOL_CALL', "model": "test/model"},
                {"response": "DONE: fresh tool call finished", "model": "test/model"},
            ]
            runtime = NativeLightRuntime(
                client=client, initial_prompt="Inspect README", model="test/model",
                fallback_model=None, workspace_root=str(workspace),
                tools=[LOCAL_FILE_READ_SCHEMA], load_tools_on_start=True,
                persistent_plan=False, base_timeout=30,
            )
            events = []
            runtime.event_fn = lambda name, payload: events.append((name, payload))
            with patch("aicoder.agent_runtime.run_tool", return_value=("README contents", False)) as execute:
                result = runtime.run()
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.response, "DONE: fresh tool call finished")
            self.assertEqual(execute.call_count, 1)
            self.assertFalse(any(name == "tool_call_recovered" for name, _ in events))

    def test_truncated_tool_call_gets_one_repair_turn_then_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = [
                {"response": '<tool_call>{"name":"file_read","arguments":{"path":"README.md"', "model": "test/model"},
                {"response": '<tool_call>{"name":"file_read","arguments":{"path":"README.md"}}</tool_call>', "model": "test/model"},
                {"response": "DONE: repaired tool call and finished", "model": "test/model"},
            ]
            runtime = NativeLightRuntime(
                client=client, initial_prompt="Inspect README", model="test/model",
                fallback_model=None, workspace_root=str(workspace),
                tools=[LOCAL_FILE_READ_SCHEMA], load_tools_on_start=True,
                persistent_plan=False, base_timeout=30,
            )
            events = []
            runtime.event_fn = lambda name, payload: events.append((name, payload))
            with patch("aicoder.agent_runtime.run_tool", return_value=("README contents", False)) as execute:
                result = runtime.run()
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.response, "DONE: repaired tool call and finished")
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(client.chat.call_count, 3)
            self.assertTrue(any(name == "final_response_repair" for name, _ in events))

    def test_orphan_closing_tool_tag_is_repaired_not_completed(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = [
                {"response": "}}\n</tool_call>", "model": "test/model"},
                {"response": "DONE: valid textual summary", "model": "test/model"},
            ]
            runtime = NativeLightRuntime(
                client=client, initial_prompt="Summarize the current state", model="test/model",
                fallback_model=None, workspace_root=str(workspace),
                tools=[], load_tools_on_start=False, persistent_plan=False, base_timeout=30,
            )
            events = []
            runtime.event_fn = lambda name, payload: events.append((name, payload))
            result = runtime.run()
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.response, "DONE: valid textual summary")
            self.assertEqual(client.chat.call_count, 2)
            self.assertTrue(any(name == "final_response_repair" for name, _ in events))

    def test_empty_response_after_tool_never_completes_and_pauses_after_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = [
                {"response": '<tool_call>{"name":"file_read","arguments":{"path":"README.md"}}</tool_call>', "model": "test/model"},
                {"response": "", "model": "test/model"},
                {"response": "", "model": "test/model"},
            ]
            runtime = NativeLightRuntime(
                client=client, initial_prompt="Inspect README and summarize", model="test/model",
                fallback_model=None, workspace_root=str(workspace),
                tools=[LOCAL_FILE_READ_SCHEMA], load_tools_on_start=True,
                plan_store=store, base_timeout=30,
            )
            with patch("aicoder.agent_runtime.run_tool", return_value=("README contents", False)) as execute:
                result = runtime.run()
            self.assertEqual(result.status, "paused")
            self.assertIn("no usable final response", result.response)
            self.assertEqual(execute.call_count, 1)
            plan = store.load_current(str(workspace))
            self.assertEqual(plan.status, "paused")
            self.assertEqual(plan.last_response, "")
            self.assertIn("no usable final response", plan.pause_reason)

    def test_duplicate_tool_call_is_blocked_before_second_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            client = MagicMock()
            client.timeout = 30
            repeated = {
                "response": '<tool_call>{"name":"file_read","arguments":{"path":"README.md"}}</tool_call>',
                "model": "test/model",
            }
            client.chat.side_effect = [
                repeated,
                repeated,
                {"response": "DONE: used the existing result", "model": "test/model"},
            ]
            runtime = NativeLightRuntime(
                client=client, initial_prompt="Inspect README", model="test/model",
                fallback_model=None, workspace_root=str(workspace),
                tools=[LOCAL_FILE_READ_SCHEMA], load_tools_on_start=True,
                persistent_plan=False, base_timeout=30,
            )
            events = []
            runtime.event_fn = lambda name, payload: events.append((name, payload))
            with patch("aicoder.agent_runtime.run_tool", return_value=("README contents", False)) as run_tool:
                result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.response, "DONE: used the existing result")
            self.assertEqual(run_tool.call_count, 1)
            self.assertEqual(client.chat.call_count, 3)
            self.assertTrue(any(name == "loop_prevented" for name, _ in events))


    def test_progress_guard_nudges_after_six_read_only_batches(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = [
                {"response": f'<tool_call>{{"name":"file_read","arguments":{{"path":"file{i}.txt"}}}}</tool_call>', "model": "test/model"}
                for i in range(6)
            ] + [{"response": "DONE: evidence consolidated", "model": "test/model"}]
            runtime = NativeLightRuntime(
                client=client, initial_prompt="Inspect the relevant files and summarize", model="test/model",
                fallback_model=None, workspace_root=str(workspace),
                tools=[LOCAL_FILE_READ_SCHEMA], load_tools_on_start=True,
                persistent_plan=False, base_timeout=30,
            )
            events = []
            runtime.event_fn = lambda name, payload: events.append((name, payload))
            with patch("aicoder.agent_runtime.run_tool", return_value=("contents", False)) as run_tool:
                result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(run_tool.call_count, 6)
            guards = [payload for name, payload in events if name == "runtime_status" and payload.get("phase") == "progress_guard"]
            self.assertEqual(len(guards), 1)
            self.assertIn("6 consecutive batches", guards[0].get("message", ""))
            self.assertTrue(any(
                "Progress guard:" in str(message.get("content", ""))
                for message in result.messages
            ))

    def test_repeated_blocked_duplicate_pauses_with_visible_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            client = MagicMock()
            client.timeout = 30
            repeated = {
                "response": '<tool_call>{"name":"file_read","arguments":{"path":"README.md"}}</tool_call>',
                "model": "test/model",
            }
            client.chat.side_effect = [repeated, repeated, repeated]
            runtime = NativeLightRuntime(
                client=client, initial_prompt="Inspect README", model="test/model",
                fallback_model=None, workspace_root=str(workspace),
                tools=[LOCAL_FILE_READ_SCHEMA], load_tools_on_start=True,
                persistent_plan=False, base_timeout=30,
            )
            with patch("aicoder.agent_runtime.run_tool", return_value=("README contents", False)) as run_tool:
                result = runtime.run()

            self.assertEqual(result.status, "paused")
            self.assertIn("same tool operation", result.response)
            self.assertEqual(run_tool.call_count, 1)
            self.assertEqual(client.chat.call_count, 3)

    def test_resume_reuses_paused_plan_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            original = store.create("Finish migration", str(workspace), "test/model")
            original.status = "paused"
            original.iteration = 5
            original.pause_reason = "stopped for review"
            store.save(original)

            client = MagicMock()
            client.timeout = 30
            client.chat.return_value = {"response": "DONE: resumed", "model": "test/model"}
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                resume=True,
                base_timeout=30,
            )
            result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.plan_id, original.id)
            resumed = store.load_current(str(workspace))
            self.assertEqual(resumed.id, original.id)
            self.assertEqual(resumed.status, "completed")
            self.assertTrue(any(event.get("kind") == "resume" for event in resumed.events))


    def test_explicit_resume_uses_requested_plan_and_cumulative_iteration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            target = store.create("Repair target parser", str(workspace), "test/model")
            target.status = "paused"
            target.iteration = 5
            target.pause_reason = "process stopped"
            store.save(target)
            other = store.create("Different current task", str(workspace), "test/model")
            other.status = "paused"
            store.save(other)

            client = MagicMock()
            client.timeout = 30
            client.chat.return_value = {"response": "DONE: resumed target", "model": "test/model"}
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                quick_chat=True,
                plan_store=store,
                resume=True,
                resume_plan_id=target.id,
                base_timeout=30,
            )
            result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.plan_id, target.id)
            resumed = store.load(str(workspace), target.id)
            self.assertEqual(resumed.iteration, 6)
            self.assertEqual(resumed.resume_count, 1)
            request_messages = client.chat.call_args.kwargs["messages"]
            resume_inputs = [
                str(message.get("content", "")) for message in request_messages
                if message.get("role") == "user"
                and "Resume persistent plan" in str(message.get("content", ""))
            ]
            self.assertEqual(len(resume_inputs), 1)
            self.assertIn("Original task: Repair target parser", resume_inputs[0])
            self.assertNotEqual(resume_inputs[0].strip().lower(), "continue")

    def test_explicit_current_resume_requires_existing_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            client = MagicMock()
            client.timeout = 30
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                quick_chat=True,
                plan_store=PlanStore(root / "plans"),
                resume=True,
                resume_plan_id="current",
                base_timeout=30,
            )
            result = runtime.run()

            self.assertEqual(result.status, "failed")
            self.assertIn("no current persistent plan", result.error)
            client.chat.assert_not_called()

    def test_explicit_resume_missing_plan_fails_without_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            client = MagicMock()
            client.timeout = 30
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                quick_chat=True,
                plan_store=PlanStore(root / "plans"),
                resume=True,
                resume_plan_id="missing-plan",
                base_timeout=30,
            )
            result = runtime.run()

            self.assertEqual(result.status, "failed")
            self.assertIn("not found", result.error)
            client.chat.assert_not_called()

    def test_resume_restores_mutation_progress_and_blocks_write_until_fresh_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            plan = store.create("Finish x.txt safely", str(workspace), "test/model")
            plan.status = "paused"
            plan.iteration = 4
            plan.pause_reason = "restart after write"
            plan.set_step("inspect", "completed", "inspected before prior write")
            plan.set_step("implement", "completed", "prior write succeeded")
            plan.set_step("verify", "in_progress", "verification pending")
            store.save(plan)

            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = [
                {
                    "response": '<tool_call>{"name":"file_edit","arguments":{"path":"x.txt","operation":"write","content":"again"}}</tool_call>',
                    "model": "test/model",
                },
                {
                    "response": '<tool_call>{"name":"file_read","arguments":{"path":"x.txt"}}</tool_call>',
                    "model": "test/model",
                },
                {
                    "response": '<tool_call>{"name":"test","arguments":{"command":"python3 -m unittest"}}</tool_call>',
                    "model": "test/model",
                },
                {"response": "DONE: existing mutation verified", "model": "test/model"},
            ]
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[LOCAL_FILE_EDIT_SCHEMA, LOCAL_FILE_READ_SCHEMA, LOCAL_TEST_SCHEMA],
                load_tools_on_start=True,
                approval_fn=lambda _name, _args: True,
                plan_store=store,
                resume=True,
                base_timeout=30,
            )
            with patch("aicoder.agent_runtime.run_tool", return_value=("current x.txt", False)) as run_tool:
                result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertEqual(run_tool.call_count, 2)
            self.assertEqual([call.args[1] for call in run_tool.call_args_list], ["file_read", "test"])
            self.assertTrue(any(
                "require a fresh successful read/check" in str(message.get("content", ""))
                for message in result.messages
            ))
            resumed = store.load_current(str(workspace))
            self.assertEqual(resumed.id, plan.id)
            self.assertEqual(
                next(step.status for step in resumed.steps if step.id == "verify"),
                "completed",
            )

    def test_plan_prompt_does_not_replay_raw_event_or_last_response_content(self):
        from aicoder.agent_plan import plan_prompt_context, resume_prompt_context

        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            store = PlanStore(Path(temp) / "plans")
            plan = store.create("Check safe state", str(workspace), "test/model")
            plan.status = "paused"
            plan.pause_reason = "normal pause"
            plan.last_response = "RAW_SECRET_LAST_RESPONSE"
            plan.events.append({
                "kind": "tool",
                "tool": "file_read",
                "message": "RAW_SECRET_TOOL_OUTPUT",
                "is_error": False,
            })

            system_context = plan_prompt_context(plan)
            resume_context = resume_prompt_context(plan, "continue")
            combined = system_context + resume_context
            self.assertNotIn("RAW_SECRET_TOOL_OUTPUT", combined)
            self.assertNotIn("RAW_SECRET_LAST_RESPONSE", combined)
            self.assertIn("tool:file_read (ok)", system_context)


    def test_action_with_empty_enabled_tools_fails_before_model_call(self):
        client = MagicMock()
        client.timeout = 30
        runtime = NativeLightRuntime(
            client=client,
            initial_prompt="Create a game in ~/games",
            model="test/model",
            fallback_model=None,
            workspace_root=".",
            tools=[],
            load_tools_on_start=False,
            tools_unavailable_reason="No tools are enabled. Complete tool onboarding in Settings.",
            persistent_plan=False,
        )
        result = runtime.run()
        self.assertEqual(result.status, "failed")
        self.assertIn("No tools are enabled", result.error)
        client.chat.assert_not_called()

    def test_stop_interrupts_wait_for_blocking_model_call(self):
        stop_event = threading.Event()
        client = MagicMock()
        client.timeout = 30
        def slow_chat(**_kwargs):
            time.sleep(2.0)
            return {"response": "too late", "model": "test/model"}
        client.chat.side_effect = slow_chat
        runtime = NativeLightRuntime(
            client=client, initial_prompt="Inspect the workspace", model="test/model",
            fallback_model=None, workspace_root=".", tools=[], load_tools_on_start=False,
            stop_requested=stop_event.is_set, persistent_plan=False, base_timeout=30,
        )
        timer = threading.Timer(0.15, stop_event.set)
        timer.start()
        started = time.monotonic()
        result = runtime.run()
        elapsed = time.monotonic() - started
        timer.join()
        self.assertEqual(result.status, "paused")
        self.assertIn("stopped by user", result.response.lower())
        self.assertLess(elapsed, 0.8)


class NativeLightGuiTests(unittest.TestCase):
    def test_gui_worker_dispatches_to_native_light_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = [
                {
                    "response": '<tool_call>{"name":"test","arguments":{"command":"python3 -m unittest"}}</tool_call>',
                    "model": "test/model",
                },
                {"response": "DONE: gui verified", "model": "test/model"},
            ]
            worker = _AgentWorker(
                client,
                [
                    {"role": "system", "content": "simple"},
                    {"role": "user", "content": "Run the tests"},
                ],
                "test/model", [LOCAL_TEST_SCHEMA], "simple",
                load_tools_on_start=True,
            )
            finished = []
            worker.finished.connect(lambda text, model: finished.append((text, model)))
            state = {
                "runtime_mode": "native-light",
                "workspace_root": str(workspace),
                "request_timeout": 30,
                "swarm_mode": "off",
            }
            with (
                patch("aicoder.gui.chat_widget.get_state", return_value=state),
                patch("aicoder.agent_plan.CONFIG_DIR", Path(temp) / "config"),
                patch("aicoder.agent_runtime.run_tool", return_value=("tests ok", False)) as run_tool,
            ):
                worker.run()

            self.assertEqual(run_tool.call_count, 1)
            self.assertEqual(finished, [("DONE: gui verified", "test/model")])
            current_plans = list((Path(temp) / "config" / "plans").rglob("current.json"))
            self.assertEqual(len(current_plans), 1)

    def test_review_text_mentioning_tool_call_is_a_normal_final_response(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            client = MagicMock()
            client.timeout = 30
            review = (
                "P0: Tool-call normalization should keep parse_tool_calls() strict.\n"
                "P1: END_TOOL_CALL is only a protocol marker when emitted as a tool call.\n"
                "Fazit: no code changes are required for this review."
            )
            client.chat.return_value = {"response": review, "model": "test/model"}
            runtime = NativeLightRuntime(
                client=client, initial_prompt="Review the code only", model="test/model",
                fallback_model=None, workspace_root=str(workspace),
                tools=[], load_tools_on_start=False, persistent_plan=False, base_timeout=30,
            )
            result = runtime.run()
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.response, review)
            self.assertEqual(client.chat.call_count, 1)





class CompletionAuditPromptTests(unittest.TestCase):
    def test_completion_audit_preserves_long_requirement_prompt(self):
        prompt = "BEGIN\n" + ("requirement-line\n" * 700) + "TAIL-REQUIREMENT-MUST-SURVIVE"
        self.assertGreater(len(prompt), 5000)
        audit = _completion_audit_prompt(prompt)
        self.assertIn("TAIL-REQUIREMENT-MUST-SURVIVE", audit)


class AutoResumePolicyTests(unittest.TestCase):
    def test_only_runtime_recoverable_pauses_auto_resume(self):
        self.assertTrue(auto_resumable_pause(
            "Transient model/backend failure after request retries were exhausted: timeout"
        ))
        self.assertTrue(auto_resumable_pause(
            "Agent paused: state changed successfully, but the model did not perform a successful post-change verification"
        ))
        self.assertFalse(auto_resumable_pause("Agent stopped by user"))
        self.assertFalse(auto_resumable_pause("Agent paused because the user rejected file_edit."))
        prompt = auto_resume_prompt("same tool operation kept repeating without progress", 1)
        self.assertIn("Automatic runtime resume 1/3", prompt)
        self.assertIn("Do not repeat the same failed action unchanged", prompt)
        safety = "Agent safety pause after an unusually long run. The persistent plan is preserved; resume it with continue."
        self.assertEqual(auto_resume_limit(safety), 6)
        safety_prompt = auto_resume_prompt(safety, 1)
        self.assertIn("Automatic continuation slice 1/6", safety_prompt)
        self.assertIn("NOT a fresh analysis pass", safety_prompt)
        self.assertIn("Do not restart architecture discovery", safety_prompt)
        self.assertIn("Move the assigned task toward a terminal state now", safety_prompt)


class RuntimeEventPayloadRegressionTests(unittest.TestCase):
    def test_emit_allows_payload_field_named_kind(self):
        events = []
        runtime = NativeLightRuntime(
            client=MagicMock(), initial_prompt="test", model="test/model", fallback_model=None,
            workspace_root=".", event_fn=lambda event_kind, payload: events.append((event_kind, payload)),
        )
        runtime._emit("performance_warning", kind="model_latency", elapsed_ms=12000)
        self.assertEqual(events[0][0], "performance_warning")
        self.assertEqual(events[0][1]["kind"], "model_latency")


if __name__ == "__main__":
    unittest.main()


class BinaryExecVerificationClassificationTests(unittest.TestCase):
    def test_assertion_readback_python_is_verification(self):
        self.assertTrue(_is_behavior_verification_call("binary_exec", {
            "program": "python3",
            "arguments": ["-B", "-c", "from pathlib import Path; p=Path('x'); assert p.is_file(); assert p.read_bytes()==b'x'"],
        }))

    def test_arbitrary_python_is_not_verification(self):
        self.assertFalse(_is_behavior_verification_call("binary_exec", {
            "program": "python3",
            "arguments": ["-c", "print('hello')"],
        }))
