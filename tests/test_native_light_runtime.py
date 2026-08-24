from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from aicoder.agent_plan import PlanStore
from aicoder.agent_runtime import NativeLightRuntime
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
            )

            result = runtime.run()

            self.assertEqual(result.status, "completed")
            self.assertFalse(result.plan_id)
            self.assertIsNone(store.load_current(str(workspace)))

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
                "test/model", "", [LOCAL_TEST_SCHEMA], "simple",
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



class NativeLightOnDemandTests(unittest.TestCase):
    def test_prepare_tools_keeps_full_catalogue_host_side(self):
        client = MagicMock(); client.timeout = 30
        catalogue = [
            {"name":"shell","description":"shell"},
            {"name":"file_read","description":"read"},
            {"name":"file_edit","description":"edit"},
            {"name":"test","description":"test"},
            {"name":"search","description":"web search"},
            {"name":"memory_search","description":"memory"},
        ]
        runtime = NativeLightRuntime(
            client=client, initial_prompt="fix app.py and test it", model="test/model",
            fallback_model=None, workspace_root=".", tool_mode="on_demand",
        )
        with patch("aicoder.agent_runtime.load_tools", return_value=catalogue):
            active, full = runtime._prepare_tools()
        self.assertEqual(full, catalogue)
        self.assertIn("shell", {tool["name"] for tool in active})
        self.assertIn("toolbox_search", {tool["name"] for tool in active})
        self.assertNotIn("toolbox_search", {tool["name"] for tool in full})

    def test_on_demand_can_expand_and_use_new_tool_next_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            client = MagicMock(); client.timeout = 30
            client.chat.side_effect = [
                {"response": '<tool_call>{"name":"capability_request","arguments":{"capabilities":["memory"]}}</tool_call>', "model":"test/model"},
                {"response": '<tool_call>{"name":"memory_search","arguments":{"query":"parser"}}</tool_call>', "model":"test/model"},
                {"response": "DONE: memory inspected", "model":"test/model"},
            ]
            catalogue = [
                {"name":"shell","description":"shell"},
                {"name":"file_read","description":"read files"},
                {"name":"file_edit","description":"edit files"},
                {"name":"test","description":"tests"},
                {"name":"memory_search","description":"search project memory"},
            ]
            runtime = NativeLightRuntime(
                client=client, initial_prompt="fix app.py", model="test/model",
                fallback_model=None, workspace_root=temp, tool_mode="on_demand",
                persistent_plan=False, base_timeout=30,
            )
            events = []
            runtime.event_fn = lambda kind, payload: events.append((kind, payload))
            with patch("aicoder.agent_runtime.load_tools", return_value=catalogue), \
                 patch("aicoder.agent_runtime.supports_tools", return_value=True), \
                 patch("aicoder.agent_runtime.run_tool", return_value=("memory result", False)) as execute:
                result = runtime.run()
            self.assertEqual(result.status, "completed")
            self.assertEqual(execute.call_args.args[1], "memory_search")
            self.assertTrue(any(kind == "tools_expanded" and "memory_search" in payload["names"] for kind, payload in events))
            sent_tools = client.chat.call_args_list[1].kwargs["tools"]
            self.assertIn("memory_search", {tool["name"] for tool in sent_tools})

    def test_always_mode_exposes_full_catalogue_without_meta_tools(self):
        client = MagicMock(); client.timeout = 30
        catalogue = [{"name":"shell"}, {"name":"search"}]
        runtime = NativeLightRuntime(
            client=client, initial_prompt="anything", model="test/model",
            fallback_model=None, workspace_root=".", tool_mode="always",
        )
        with patch("aicoder.agent_runtime.load_tools", return_value=catalogue):
            active, full = runtime._prepare_tools()
        self.assertEqual(active, catalogue)
        self.assertEqual(full, catalogue)

if __name__ == "__main__":
    unittest.main()
