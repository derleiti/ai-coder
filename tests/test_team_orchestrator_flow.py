from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import AgentRunResult, auto_resumable_pause, auto_resume_limit
from aicoder.team_orchestrator import AgentStageResult, CandidateResult, _anonymized_brainstorm_round, _brainstorm_rounds, _build_brainstorm_prompt, _RESEARCH_TOOL_NAMES, _brainstorm_participants, _merge_completion_contradiction, _run_worker_with_auto_resume, _worker_event_forwarder, _call_advisor, run_team
from aicoder.team_runtime import config_from_state
from aicoder.workspace_backend import RamWorkspace


def _result(text: str, model: str = "test/model") -> AgentRunResult:
    return AgentRunResult(
        status="completed", response=text, model=model, messages=[], tools=[], system_prompt="",
    )


class FakeIntegrationRuntime:
    calls = 0

    def __init__(self, *, workspace_root: str, model: str, **kwargs):
        self.workspace_root = Path(workspace_root)
        self.model = model

    def run(self):
        FakeIntegrationRuntime.calls += 1
        marker = self.workspace_root / "integrated.txt"
        if FakeIntegrationRuntime.calls == 1:
            marker.write_text("merged\n", encoding="utf-8")
            return _result("DONE: merge", self.model)
        marker.write_text("final\n", encoding="utf-8")
        return _result("DONE: final", self.model)


class TeamResearchToolPolicyTests(unittest.TestCase):
    def test_research_workers_have_safe_diagnostic_tools(self):
        self.assertTrue({"binary_exec", "test", "lint"}.issubset(_RESEARCH_TOOL_NAMES))
        self.assertNotIn("shell", _RESEARCH_TOOL_NAMES)
        self.assertNotIn("config_set", _RESEARCH_TOOL_NAMES)
        self.assertNotIn("mail_send", _RESEARCH_TOOL_NAMES)



class TeamBrainstormRoundTests(unittest.TestCase):
    def test_round_count_normalizes_to_one_through_five(self):
        self.assertEqual(_brainstorm_rounds({}), 2)
        self.assertEqual(_brainstorm_rounds({"team_brainstorm_rounds": 1}), 1)
        self.assertEqual(_brainstorm_rounds({"team_brainstorm_rounds": 5}), 5)
        self.assertEqual(_brainstorm_rounds({"team_brainstorm_rounds": 99}), 5)
        self.assertEqual(_brainstorm_rounds({"team_brainstorm_rounds": "bad"}), 2)


class TeamBrainstormStateTests(unittest.TestCase):
    def test_operator_input_is_anonymized(self):
        rows = [
            AgentStageResult("brainstorm:r1:coder:architecture-first", "provider/a", "completed", "idea A", 1),
            AgentStageResult("brainstorm:r1:research:security", "provider/b", "completed", "idea B", 1),
        ]
        text = _anonymized_brainstorm_round(rows)
        self.assertIn("proposal-01", text)
        self.assertIn("proposal-02", text)
        self.assertNotIn("provider/a", text)
        self.assertNotIn("provider/b", text)
        self.assertNotIn("architecture-first", text)
        self.assertNotIn("research:security", text)

    def test_evolution_prompt_contains_research_and_shared_state(self):
        research = [AgentStageResult("research:primary_sources", "provider/a", "completed", "verified fact", 1)]
        prompt = _build_brainstorm_prompt(
            "Improve project", "workspace=/tmp/project", research, "security and reliability",
            round_index=2, brainstorm_state="[STRONG_IDEAS]\nidea A",
        )
        self.assertIn("[BRAINSTORM_ROUND]\n2", prompt)
        self.assertIn("verified fact", prompt)
        self.assertIn("[STRONG_IDEAS]\nidea A", prompt)
        self.assertIn("security and reliability", prompt)


class TeamBrainstormParticipantTests(unittest.TestCase):
    def test_distinct_models_are_deduplicated_and_coder_only_model_is_included(self):
        state = {
            "selected_model": "provider/a", "team_runtime_mode": "on",
            "team_research_model_1": "provider/a", "team_research_model_2": "provider/a",
            "team_planner_model": "provider/a",
            "team_coder_model_1": "provider/a", "team_coder_model_2": "provider/b",
        }
        config = config_from_state(state)
        participants = _brainstorm_participants(config)
        self.assertEqual([model for _, model, _ in participants], ["provider/a", "provider/b"])
        self.assertTrue(any(label.startswith("coder:") and model == "provider/b" for label, model, _ in participants))



class TeamWorkerEventForwarderTests(unittest.TestCase):
    def test_reserved_payload_kind_does_not_collide_with_team_event_kind(self):
        events = []
        forward = _worker_event_forwarder(
            lambda kind, payload: events.append((kind, payload)),
            "coder:1",
        )
        forward("performance_warning", {"kind": "filesystem_latency", "elapsed_ms": 123})
        self.assertEqual(len(events), 1)
        kind, payload = events[0]
        self.assertEqual(kind, "team_worker_event")
        self.assertEqual(payload.get("event"), "performance_warning")
        self.assertEqual(payload.get("role"), "coder:1")
        self.assertEqual(payload.get("elapsed_ms"), 123)
        self.assertEqual(payload.get("worker_payload"), {"kind": "filesystem_latency"})


class TeamMergeCompletionGuardTests(unittest.TestCase):
    def test_explicitly_incomplete_merge_result_is_not_success(self):
        text = "[MERGE_RESULT]\nMerge konnte nicht ausgeführt werden.\n- Verification: nicht möglich\nDONE: merge complete"
        self.assertTrue(_merge_completion_contradiction(text))

    def test_failure_phrase_before_merge_result_does_not_poison_valid_final(self):
        text = "Rejected candidate said merge could not be executed.\n[MERGE_RESULT]\nVerification: 504 tests passed\nDONE: merge complete"
        self.assertFalse(_merge_completion_contradiction(text))

    def test_merge_incomplete_pause_is_auto_resumable(self):
        self.assertTrue(auto_resumable_pause("Merge self-reported incomplete: continue existing merge"))


class TeamAdvisorRecoveryTests(unittest.TestCase):
    def test_incomplete_envelope_retries_advisor_after_long_cooldown(self):
        model_client = MagicMock()
        model_client.chat.side_effect = [
            RuntimeError("Transient incomplete chat response: no recognized assistant response envelope; keys=['_transport_telemetry']"),
            {"response": "PLAN OK", "model": "test/model"},
        ]
        with patch("aicoder.team_orchestrator.time.sleep") as sleep_mock:
            result = _call_advisor(
                model_client, model="test/model", system="system", prompt="task", max_tokens=64,
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.response, "PLAN OK")
        self.assertEqual(model_client.chat.call_count, 2)
        sleep_mock.assert_called_once_with(30.0)


class TeamWorkerAutoResumeTests(unittest.TestCase):
    def test_paused_worker_auto_resumes_with_preserved_context(self):
        calls = []
        events = []
        paused = AgentRunResult(
            status="paused", response="Transient model/backend failure after request retries were exhausted: timeout",
            model="test/model", messages=[{"role": "system", "content": "sys"}, {"role": "assistant", "content": "partial"}],
            tools=[], system_prompt="sys",
        )
        completed = AgentRunResult(
            status="completed", response="DONE: complete", model="test/model",
            messages=[{"role": "system", "content": "sys"}, {"role": "assistant", "content": "DONE: complete"}],
            tools=[], system_prompt="sys",
        )

        def run_once(prompt, conversation):
            calls.append((prompt, conversation))
            return paused if len(calls) == 1 else completed

        with patch("aicoder.team_orchestrator.time.sleep") as sleep_mock:
            result = _run_worker_with_auto_resume(
                run_once, role="coder:1", event_fn=lambda kind, payload: events.append((kind, payload)),
                stop_requested=lambda: False,
            )
        sleep_mock.assert_called_once()
        self.assertGreaterEqual(float(sleep_mock.call_args.args[0]), 1.0)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0][0])
        self.assertIn("Automatic runtime resume 1/3", calls[1][0])
        self.assertEqual(calls[1][1], [{"role": "assistant", "content": "partial"}])
        self.assertTrue(any(kind == "team_worker_event" and payload.get("phase") == "auto_resume" for kind, payload in events))

    def test_incomplete_envelope_gets_extended_recovery_budget_and_cooldown(self):
        reason = "Transient model/backend failure after request retries were exhausted: Transient incomplete chat response: no recognized assistant response envelope; keys=['_transport_telemetry']"
        self.assertEqual(auto_resume_limit(reason), 5)
        calls = []
        paused = AgentRunResult(
            status="paused", response=reason, model="test/model",
            messages=[{"role": "system", "content": "sys"}, {"role": "assistant", "content": "partial"}],
            tools=[], system_prompt="sys",
        )
        completed = AgentRunResult(
            status="completed", response="DONE: recovered", model="test/model",
            messages=[], tools=[], system_prompt="sys",
        )
        def run_once(prompt, conversation):
            calls.append((prompt, conversation))
            return paused if len(calls) == 1 else completed
        with patch("aicoder.team_orchestrator.time.sleep") as sleep_mock:
            result = _run_worker_with_auto_resume(
                run_once, role="coder:1", event_fn=None, stop_requested=lambda: False,
            )
        self.assertEqual(result.status, "completed")
        sleep_mock.assert_called_once_with(30.0)
        self.assertIn("FRESH RECOVERY CHAT 1/5", calls[1][0])
        self.assertIn("partial", calls[1][0])
        self.assertIsNone(calls[1][1], "incomplete-envelope recovery must start a fresh provider chat")

    def test_safety_pause_gets_continuation_budget_beyond_three_slices(self):
        calls = []
        safety = AgentRunResult(
            status="paused", response="Agent safety pause after an unusually long run. The persistent plan is preserved; resume it with continue.",
            model="test/model", messages=[{"role": "system", "content": "sys"}, {"role": "assistant", "content": "evidence"}],
            tools=[], system_prompt="sys",
        )
        completed = AgentRunResult(
            status="completed", response="DONE: implemented", model="test/model",
            messages=[{"role": "system", "content": "sys"}], tools=[], system_prompt="sys",
        )
        def run_once(prompt, conversation):
            calls.append((prompt, conversation))
            return completed if len(calls) == 5 else safety
        result = _run_worker_with_auto_resume(
            run_once, role="coder:2", event_fn=None, stop_requested=lambda: False,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 5)
        self.assertIn("Automatic continuation slice 4/6", calls[4][0])
        self.assertIn("NOT a fresh analysis pass", calls[1][0])

    def test_continuation_and_recovery_budgets_are_independent(self):
        calls = []
        safety = AgentRunResult(
            status="paused", response="Agent safety pause after an unusually long run. The persistent plan is preserved; resume it with continue.",
            model="test/model", messages=[{"role": "system", "content": "sys"}], tools=[], system_prompt="sys",
        )
        repair = AgentRunResult(
            status="paused", response="Agent paused because the model returned no usable final response after a final-response repair request.",
            model="test/model", messages=[{"role": "system", "content": "sys"}], tools=[], system_prompt="sys",
        )
        completed = AgentRunResult(
            status="completed", response="DONE: merge complete", model="test/model",
            messages=[{"role": "system", "content": "sys"}], tools=[], system_prompt="sys",
        )
        sequence = [safety, safety, repair, repair, completed]
        def run_once(prompt, conversation):
            calls.append(prompt)
            return sequence[len(calls) - 1]
        result = _run_worker_with_auto_resume(
            run_once, role="merge", event_fn=None, stop_requested=lambda: False,
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 5)
        self.assertIn("Automatic continuation slice 1/6", calls[1])
        self.assertIn("Automatic continuation slice 2/6", calls[2])
        self.assertIn("Automatic runtime resume 1/3", calls[3])
        self.assertIn("Automatic runtime resume 2/3", calls[4])

    def test_user_stop_never_auto_resumes(self):
        calls = []
        paused = AgentRunResult(
            status="paused", response="Agent stopped by user", model="test/model",
            messages=[], tools=[], system_prompt="sys",
        )
        def run_once(prompt, conversation):
            calls.append((prompt, conversation))
            return paused
        result = _run_worker_with_auto_resume(
            run_once, role="research:test", event_fn=None, stop_requested=lambda: False,
        )
        self.assertEqual(result.status, "paused")
        self.assertEqual(len(calls), 1)


class TeamOrchestratorFlowTests(unittest.TestCase):
    def test_pipeline_selects_candidate_merges_finalizes_and_persists(self):
        FakeIntegrationRuntime.calls = 0
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "app.py").write_text("value = 0\n", encoding="utf-8")
            (source / "pyproject.toml").write_text("[project]\nname=\"demo\"\nversion=\"0.1.0\"\n", encoding="utf-8")
            (source / "tests").mkdir()
            (source / "tests" / "test_app.py").write_text("import unittest\nimport app\nclass T(unittest.TestCase):\n    def test_value(self): self.assertGreaterEqual(app.value, 0)\n", encoding="utf-8")
            state = {
                "selected_model": "test/model",
                "team_runtime_mode": "on",
                "workspace_mode": "ram",
                "team_research_model_1": "test/model",
                "team_research_model_2": "test/model",
                "team_research_model_3": "",
                "team_research_model_4": "",
                "team_planner_model": "test/model",
                "team_coordinator_model": "",
                "team_coder_model_1": "test/model",
                "team_coder_model_2": "test/model",
                "team_coder_model_3": "",
                "team_coder_model_4": "",
                "team_merge_model": "test/model",
                "team_test_planner_model": "test/model",
            }
            config = config_from_state(state)

            def researcher(**kwargs):
                return AgentStageResult(
                    role=f"research:{kwargs['role']}", model=kwargs["model"], status="completed",
                    response=f"evidence {kwargs['role']}", elapsed_ms=1,
                )

            def advisor(_model_client, *, model, system, prompt, max_tokens=0, **_kwargs):
                return AgentStageResult("advisor", model, "completed", "shared plan", 1)

            candidates = []
            def candidate(**kwargs):
                backend = RamWorkspace(source, ram_root=ram_dir)
                backend.prepare()
                slot = kwargs["slot"]
                (backend.info.execution_root / "app.py").write_text(f"value = {slot}\n", encoding="utf-8")
                (backend.info.execution_root / "tests" / "test_app.py").write_text(
                    "import unittest\nimport app\nclass T(unittest.TestCase):\n"
                    f"    def test_value(self): self.assertEqual(app.value, {slot})\n",
                    encoding="utf-8",
                )
                item = CandidateResult(slot, kwargs["model"], kwargs["strategy"], backend, _result(f"DONE: candidate {slot}"))
                candidates.append(item)
                return item

            def evaluate(item):
                return {
                    "score": 90 if item.slot == 2 else 70,
                    "delta": item.workspace.delta_summary(),
                    "checks": {"compile": {"ok": True}, "tests": {"ok": True}},
                    "diff": f"candidate {item.slot}",
                    "candidate_id": f"cand-{item.slot}",
                    "verification_passed": True,
                }

            def create_backend(root, mode, **kwargs):
                return RamWorkspace(root, ram_root=ram_dir)

            with (
                patch("aicoder.team_orchestrator.load_tools", return_value=[]),
                patch("aicoder.team_orchestrator._run_researcher", side_effect=researcher),
                patch("aicoder.team_orchestrator._call_advisor", side_effect=advisor),
                patch("aicoder.team_orchestrator._run_candidate", side_effect=candidate),
                patch("aicoder.team_orchestrator.evaluate_candidate", side_effect=evaluate),
                patch("aicoder.team_orchestrator.create_isolated_team_workspace", side_effect=create_backend),
                patch("aicoder.team_orchestrator.NativeLightRuntime", FakeIntegrationRuntime),
            ):
                result = run_team(
                    task="Implement feature", state=state, config=config, client=MagicMock(),
                    model_client=MagicMock(), source_workspace=str(source),
                )

            self.assertEqual(result.status, "completed", result.error)
            self.assertEqual(result.performance["winner_candidate_id"], "cand-2")
            self.assertEqual(result.performance["ledger"]["completed"], [
                "plan_research", "research", "brainstorm", "plan_code", "code", "merge_plan", "merge",
                "plan_tests", "tests_function_ok", "atomic_disk_write",
            ])
            self.assertEqual((source / "app.py").read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual((source / "integrated.txt").read_text(encoding="utf-8"), "merged\n")
            self.assertFalse((source / ".aicoder-team").exists())


if __name__ == "__main__":
    unittest.main()

class MergeRecoveryRequiredGuardTests(unittest.TestCase):
    def test_recovery_required_final_merge_is_contradictory_success(self):
        text = "[MERGE_RESULT]\nstatus: recovery_required\nverification: incomplete\nDONE: merge complete"
        self.assertTrue(_merge_completion_contradiction(text))
