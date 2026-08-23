from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent import _headless_approval, _native_exit_code, _run_native_light_agent
from aicoder.agent_runtime import AgentRunResult


class HeadlessAgentTests(unittest.TestCase):
    def test_exit_codes_are_machine_distinct(self):
        self.assertEqual(_native_exit_code("completed"), 0)
        self.assertEqual(_native_exit_code("failed"), 1)
        self.assertEqual(_native_exit_code("paused"), 3)

    def test_headless_approval_never_prompts_in_ask_mode(self):
        with patch("aicoder.agent.get_state", return_value={"approval_mode": "ask"}):
            self.assertFalse(_headless_approval("file_edit", {"path": "x", "operation": "create", "content": "x"}))
        with patch("aicoder.agent.get_state", return_value={"approval_mode": "autopilot"}):
            self.assertTrue(_headless_approval("file_edit", {"path": "x", "operation": "create", "content": "x"}))

    def test_json_mode_prints_single_result_object(self):
        with tempfile.TemporaryDirectory() as temp:
            result = AgentRunResult(
                status="completed",
                response="DONE: ok",
                model="test/model",
                messages=[],
                tools=[],
                system_prompt="system",
                iterations=2,
                latency_ms=12,
                plan_id="plan-1",
            )
            runtime = MagicMock()
            runtime.run.return_value = result
            state = {
                "workspace_root": temp,
                "request_timeout": 30,
                "tool_mode": "off",
                "enabled_tools": None,
                "swarm_mode": "off",
                "approval_mode": "ask",
            }
            out = io.StringIO()
            with (
                patch("aicoder.agent.load_session", return_value=MagicMock(base_url="https://example.test", token="x")),
                patch("aicoder.agent.TriForceClient"),
                patch("aicoder.agent_runtime.NativeLightRuntime", return_value=runtime),
                patch("aicoder.agent.history_record"),
                redirect_stdout(out),
            ):
                rc = _run_native_light_agent(
                    "test task", "test/model", None,
                    conversation=None,
                    state=state,
                    json_output=True,
                )
            self.assertEqual(rc, 0)
            lines = [line for line in out.getvalue().splitlines() if line.strip()]
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["type"], "result")
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["plan_id"], "plan-1")

    def test_json_events_emit_ndjson_and_final_result(self):
        with tempfile.TemporaryDirectory() as temp:
            holder = {}

            class FakeRuntime:
                def __init__(self, **kwargs):
                    holder.update(kwargs)
                def run(self):
                    holder["event_fn"]("run_start", {"plan_id": "p1", "tools": 2})
                    holder["event_fn"]("thought", {"text": "checking"})
                    return AgentRunResult(
                        status="paused", response="needs approval", model="test/model",
                        messages=[], tools=[], system_prompt="system", iterations=1,
                        latency_ms=4, plan_id="p1",
                    )

            state = {
                "workspace_root": temp,
                "request_timeout": 30,
                "tool_mode": "off",
                "enabled_tools": None,
                "swarm_mode": "off",
                "approval_mode": "ask",
            }
            out = io.StringIO()
            with (
                patch("aicoder.agent.load_session", return_value=MagicMock(base_url="https://example.test", token="x")),
                patch("aicoder.agent.TriForceClient"),
                patch("aicoder.agent_runtime.NativeLightRuntime", FakeRuntime),
                patch("aicoder.agent.history_record"),
                redirect_stdout(out),
            ):
                rc = _run_native_light_agent(
                    "test task", "test/model", None,
                    conversation=None,
                    state=state,
                    json_events=True,
                )
            self.assertEqual(rc, 3)
            payloads = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
            self.assertEqual([item["type"] for item in payloads], ["run_start", "thought", "result"])
            self.assertEqual(payloads[-1]["status"], "paused")


if __name__ == "__main__":
    unittest.main()
