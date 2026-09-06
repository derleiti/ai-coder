from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import AgentRunResult
from aicoder.team_orchestrator import _run_candidate
from aicoder.workspace_backend import RamWorkspace


class _NoopClient:
    pass


def _result(status: str, response: str, messages=None) -> AgentRunResult:
    return AgentRunResult(status, response, "test/model", list(messages or []), [], "system")


class TeamCandidateAutoResumeTests(unittest.TestCase):
    def _backend(self):
        backend = MagicMock(spec=RamWorkspace)
        backend.info.execution_root = Path("/tmp/fake-team-candidate")
        return backend

    def test_paused_candidate_is_reprompted_in_same_workspace_with_history(self):
        backend = self._backend()
        backend.delta_summary.side_effect = [
            {"changed_count": 0, "deleted_count": 0},
            {"changed_count": 1, "deleted_count": 0},
            {"changed_count": 1, "deleted_count": 0},
        ]
        first = _result(
            "paused",
            "Agent paused: state changed successfully, but verification is incomplete.",
            [
                {"role": "system", "content": "old system"},
                {"role": "assistant", "content": "I changed app.py"},
                {"role": "user", "content": "Tool result: edit ok"},
            ],
        )
        second = _result("completed", "DONE: verified candidate")
        calls = []
        results = iter([first, second])

        def runtime_factory(**kwargs):
            calls.append(kwargs)
            runtime = MagicMock()
            runtime.run.return_value = next(results)
            return runtime

        with (
            patch("aicoder.team_orchestrator.create_isolated_team_workspace", return_value=backend),
            patch("aicoder.team_orchestrator.configured_project_python", return_value=None),
            patch("aicoder.team_orchestrator.NativeLightRuntime", side_effect=runtime_factory),
            patch("aicoder.team_orchestrator.evaluate_candidate", return_value={"verification_passed": True}),
        ):
            result = _run_candidate(
                client=_NoopClient(), model_client=_NoopClient(), source_workspace="/tmp/source",
                backend_mode="ram", slot=1, model="test/model", strategy="conservative",
                task="fix bug", plan="shared plan", coordinator="", tools=[], stop_requested=None,
            )

        self.assertEqual(result.run.status, "completed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["workspace_root"], calls[1]["workspace_root"])
        self.assertTrue(calls[0]["require_mutation_or_explicit_no_change"])
        self.assertFalse(calls[1]["require_mutation_or_explicit_no_change"])
        self.assertIn("AUTONOMOUS TEAM RESUME 1/4", calls[1]["initial_prompt"])
        self.assertIn("verification", calls[1]["initial_prompt"].lower())
        self.assertEqual(
            calls[1]["conversation"],
            [
                {"role": "assistant", "content": "I changed app.py"},
                {"role": "user", "content": "Tool result: edit ok"},
            ],
        )

    def test_liveness_timeout_tracks_inactivity_not_total_wall_time(self):
        backend = self._backend()
        backend.delta_summary.return_value = {"changed_count": 1, "deleted_count": 0}
        clock = [0.0]
        calls = []

        def fake_monotonic():
            return clock[0]

        def runtime_factory(**kwargs):
            calls.append(kwargs)
            runtime = MagicMock()
            if len(calls) == 1:
                def first_run():
                    clock[0] = 700.0
                    kwargs["event_fn"]("model_response", {"response": "still working"})
                    clock[0] = 1400.0
                    return _result("paused", "needs continuation")
                runtime.run.side_effect = first_run
            else:
                def second_run():
                    clock[0] = 1450.0
                    kwargs["event_fn"]("model_response", {"response": "done"})
                    return _result("completed", "DONE: candidate")
                runtime.run.side_effect = second_run
            return runtime

        with (
            patch("aicoder.team_orchestrator.create_isolated_team_workspace", return_value=backend),
            patch("aicoder.team_orchestrator.configured_project_python", return_value=None),
            patch("aicoder.team_orchestrator.NativeLightRuntime", side_effect=runtime_factory),
            patch("aicoder.team_orchestrator.evaluate_candidate", return_value={"verification_passed": True}),
            patch("aicoder.team_orchestrator.time.monotonic", side_effect=fake_monotonic),
        ):
            result = _run_candidate(
                client=_NoopClient(), model_client=_NoopClient(), source_workspace="/tmp/source",
                backend_mode="ram", slot=1, model="test/model", strategy="conservative",
                task="fix bug", plan="shared plan", coordinator="", tools=[], stop_requested=None,
                liveness_timeout_s=1200,
            )

        self.assertEqual(result.run.status, "completed")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("liveness timeout", result.run.error.lower())

    def test_explicit_user_stop_is_not_auto_resumed(self):
        backend = self._backend()
        backend.delta_summary.return_value = {"changed_count": 0, "deleted_count": 0}
        paused = _result("paused", "Agent stopped by user")
        calls = []

        def runtime_factory(**kwargs):
            calls.append(kwargs)
            runtime = MagicMock()
            runtime.run.return_value = paused
            return runtime

        with (
            patch("aicoder.team_orchestrator.create_isolated_team_workspace", return_value=backend),
            patch("aicoder.team_orchestrator.configured_project_python", return_value=None),
            patch("aicoder.team_orchestrator.NativeLightRuntime", side_effect=runtime_factory),
        ):
            result = _run_candidate(
                client=_NoopClient(), model_client=_NoopClient(), source_workspace="/tmp/source",
                backend_mode="ram", slot=1, model="test/model", strategy="conservative",
                task="fix bug", plan="shared plan", coordinator="", tools=[], stop_requested=None,
            )

        self.assertEqual(result.run.status, "paused")
        self.assertEqual(len(calls), 1)

    def test_auto_resume_has_hard_limit(self):
        backend = self._backend()
        backend.delta_summary.return_value = {"changed_count": 0, "deleted_count": 0}
        calls = []

        def runtime_factory(**kwargs):
            calls.append(kwargs)
            runtime = MagicMock()
            runtime.run.return_value = _result("paused", "needs continuation")
            return runtime

        with (
            patch("aicoder.team_orchestrator.create_isolated_team_workspace", return_value=backend),
            patch("aicoder.team_orchestrator.configured_project_python", return_value=None),
            patch("aicoder.team_orchestrator.NativeLightRuntime", side_effect=runtime_factory),
        ):
            result = _run_candidate(
                client=_NoopClient(), model_client=_NoopClient(), source_workspace="/tmp/source",
                backend_mode="ram", slot=1, model="test/model", strategy="conservative",
                task="fix bug", plan="shared plan", coordinator="", tools=[], stop_requested=None,
            )

        self.assertEqual(result.run.status, "paused")
        self.assertEqual(len(calls), 5)  # initial run + four autonomous resumes

    def test_candidate_exception_releases_private_workspace(self):
        from aicoder.team_orchestrator import _run_candidate
        from aicoder.workspace_backend import RamWorkspace

        class RaisingRuntime:
            def __init__(self, **kwargs):
                pass

            def run(self):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "app.py").write_text("x = 1\n", encoding="utf-8")
            backend = RamWorkspace(source, ram_root=ram_dir)
            execution = backend.info.execution_root
            with (
                patch("aicoder.team_orchestrator.create_isolated_team_workspace", return_value=backend),
                patch("aicoder.team_orchestrator.NativeLightRuntime", RaisingRuntime),
                patch("aicoder.team_orchestrator.configured_project_python", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    _run_candidate(
                        client=MagicMock(), model_client=MagicMock(), source_workspace=str(source),
                        backend_mode="ram", slot=1, model="test/model", strategy="test",
                        task="task", plan="plan", coordinator="", tools=[], stop_requested=None,
                    )
            self.assertFalse(execution.exists())


if __name__ == "__main__":
    unittest.main()

class RuntimeCompletionSignalTests(unittest.TestCase):
    def test_runtime_complete_is_accepted_after_mutation_and_verification(self):
        client = MagicMock(); client.timeout = 30
        client.chat.side_effect = [
            {"response": 'TOOL_CALL file_edit\n{"path":"app.py","operation":"write","content":"x=1\\n"}\nEND_TOOL_CALL', "model": "test/model"},
            {"response": 'TOOL_CALL test\n{"command":["python","-m","pytest","-q"]}\nEND_TOOL_CALL', "model": "test/model"},
            {"response": 'TOOL_CALL runtime_complete\n{"summary":"implementation and tests complete","evidence":["pytest passed"]}\nEND_TOOL_CALL', "model": "test/model"},
        ]
        events = []
        tools = [
            {"name":"file_edit","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"operation":{"type":"string"},"content":{"type":"string"}}}},
            {"name":"test","inputSchema":{"type":"object","properties":{"command":{"type":"array","items":{"type":"string"}}}}},
        ]
        runtime = __import__('aicoder.agent_runtime', fromlist=['NativeLightRuntime']).NativeLightRuntime(
            client=client, initial_prompt="Implement and verify the repository change", model="test/model",
            fallback_model=None, workspace_root="/tmp", tools=tools, load_tools_on_start=False,
            persistent_plan=False, base_timeout=30, require_mutation_or_explicit_no_change=True,
            allow_completion_signal=True, event_fn=lambda kind, payload: events.append((kind, payload)),
        )
        with patch("aicoder.agent_runtime.run_tool", side_effect=[("updated app.py", False), ("1 passed", False)]):
            result = runtime.run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.response, "DONE: implementation and tests complete")
        signals = [payload for kind, payload in events if kind == "completion_signal"]
        self.assertEqual(len(signals), 1)
        self.assertTrue(signals[0]["accepted"])
        self.assertTrue(signals[0]["runtime_verified"])
        self.assertTrue(signals[0]["mutation_seen"])
        self.assertTrue(signals[0]["verification_seen"])

    def test_runtime_complete_is_rejected_until_verification_exists(self):
        client = MagicMock(); client.timeout = 30
        client.chat.side_effect = [
            {"response": 'TOOL_CALL file_edit\n{"path":"app.py","operation":"write","content":"x=1\\n"}\nEND_TOOL_CALL', "model": "test/model"},
            {"response": 'TOOL_CALL runtime_complete\n{"summary":"done too early"}\nEND_TOOL_CALL', "model": "test/model"},
            {"response": 'TOOL_CALL test\n{"command":["python","-m","pytest","-q"]}\nEND_TOOL_CALL', "model": "test/model"},
            {"response": 'TOOL_CALL runtime_complete\n{"summary":"verified now"}\nEND_TOOL_CALL', "model": "test/model"},
        ]
        events = []
        tools = [
            {"name":"file_edit","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"operation":{"type":"string"},"content":{"type":"string"}}}},
            {"name":"test","inputSchema":{"type":"object","properties":{"command":{"type":"array","items":{"type":"string"}}}}},
        ]
        runtime = __import__('aicoder.agent_runtime', fromlist=['NativeLightRuntime']).NativeLightRuntime(
            client=client, initial_prompt="Implement and verify the repository change", model="test/model",
            fallback_model=None, workspace_root="/tmp", tools=tools, load_tools_on_start=False,
            persistent_plan=False, base_timeout=30, require_mutation_or_explicit_no_change=True,
            allow_completion_signal=True, event_fn=lambda kind, payload: events.append((kind, payload)),
        )
        with patch("aicoder.agent_runtime.run_tool", side_effect=[("updated app.py", False), ("1 passed", False)]):
            result = runtime.run()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.response, "DONE: verified now")
        signals = [payload for kind, payload in events if kind == "completion_signal"]
        self.assertEqual([row["accepted"] for row in signals], [False, True])
        self.assertIn("verification", signals[0]["reason"].lower())

