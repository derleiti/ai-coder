from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import NativeLightRuntime, _needs_completion_audit
from aicoder.executor import adaptive_request_timeout


class AdaptiveContinuationTimeoutTests(unittest.TestCase):
    def test_continuation_does_not_inherit_five_minute_task_timeout(self):
        prompt = "vollstaendiger integration test " * 200
        self.assertEqual(
            adaptive_request_timeout(
                300, prompt=prompt, iteration=20,
                model="openrouter/qwen/qwen3.8-27b", continuation=True,
            ),
            90,
        )

    def test_slow_reasoning_continuation_has_bounded_extra_room(self):
        self.assertEqual(
            adaptive_request_timeout(
                300, prompt="tool result", iteration=20,
                model="provider/reasoning-model", continuation=True,
            ),
            120,
        )

    def test_first_turn_keeps_configured_budget(self):
        self.assertEqual(
            adaptive_request_timeout(
                300, prompt="large repository build", iteration=0,
                model="openrouter/qwen/qwen3.8-27b", continuation=False,
            ),
            300,
        )


class CompletionAuditTests(unittest.TestCase):
    def test_structured_tasks_require_audit_but_simple_tasks_do_not(self):
        structured = "- create A\n- verify A\n- remove A\n"
        self.assertTrue(_needs_completion_audit(structured))
        self.assertFalse(_needs_completion_audit("fix one typo"))

    def test_runtime_audits_once_before_accepting_done(self):
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
            self.assertEqual(result.response, "DONE: audited")
            self.assertEqual(client.chat.call_count, 3)
            self.assertEqual(sum(kind == "completion_audit" for kind, _ in events), 1)
            starts = [payload for kind, payload in events if kind == "model_start"]
            self.assertEqual(starts[0]["phase"], "planning")
            self.assertEqual(starts[0]["timeout"], 300)
            self.assertEqual(starts[1]["phase"], "continuation")
            self.assertEqual(starts[1]["timeout"], 90)


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


if __name__ == "__main__":
    unittest.main()
