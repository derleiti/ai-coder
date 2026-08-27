from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import AgentRunResult, auto_resumable_pause, auto_resume_limit
from aicoder.team_orchestrator import _coder_pool_size, CODER_MAX_ITERATIONS, AgentStageResult, CandidateResult, _TeamDebugLog, reset_team_debug_log, _test_evidence_result, evaluate_candidate, _anonymized_brainstorm_round, _brainstorm_rounds, _build_brainstorm_prompt, _RESEARCH_TOOL_NAMES, _brainstorm_participants, _merge_completion_contradiction, _plan_grounding_issues, _research_approval, _run_worker_with_auto_resume, _run_candidate, _candidate_is_mergeable, _verification_root_for_delta, _working_project_root, _worker_event_forwarder, _call_advisor, _save_stage_handoff, _load_stage_handoff, _render_stage_handoff, _team_handoff_dir, _create_run_backup, _preserve_failed_workspace, clear_team_checkpoint, run_team
from aicoder.team_runtime import BRAINSTORM_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT, CODER_SYSTEM_TEMPLATE, config_from_state
from aicoder.team_pipeline import TeamStage, VerificationResult
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




class TeamStageHandoffTests(unittest.TestCase):
    def test_stage_handoff_round_trip_is_bounded_and_workspace_scoped(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as checkpoint_dir:
            source = Path(source_dir)
            payload = {"task": "demo", "reports": [{"response": "x" * 20000}]}
            with patch("aicoder.team_orchestrator._TEAM_CHECKPOINT_DIR", Path(checkpoint_dir)):
                path = _save_stage_handoff(str(source), TeamStage.RESEARCH, payload)
                self.assertTrue(path.is_file())
                loaded = _load_stage_handoff(str(source), TeamStage.RESEARCH)
                self.assertEqual(loaded["stage"], "research")
                self.assertEqual(loaded["task"], "demo")
                self.assertIn("bounded stage handoff", loaded["reports"][0]["response"])
                rendered = _render_stage_handoff(str(source), TeamStage.RESEARCH)
                self.assertIn('"stage": "research"', rendered)

    def test_stage_prompts_require_independent_session_and_explicit_handoff(self):
        for prompt in (BRAINSTORM_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT, CODER_SYSTEM_TEMPLATE):
            self.assertIn("STAGE CONTEXT ISOLATION", prompt)
            self.assertIn("independent model session", prompt)
            self.assertIn("[STAGE_HANDOFF]", prompt)
        text = _build_brainstorm_prompt(
            "Improve", "workspace=/tmp/repo", [], "reliability",
            research_handoff='{"stage":"research","reports":["fact"]}',
        )
        self.assertIn("[STAGE_HANDOFF:RESEARCH]", text)
        self.assertIn('"fact"', text)

    def test_clear_checkpoint_removes_transient_stage_handoffs(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as checkpoint_dir:
            source = Path(source_dir)
            with patch("aicoder.team_orchestrator._TEAM_CHECKPOINT_DIR", Path(checkpoint_dir)):
                _save_stage_handoff(str(source), TeamStage.RESEARCH, {"task": "demo"})
                handoff_dir = _team_handoff_dir(str(source))
                self.assertTrue(handoff_dir.is_dir())
                clear_team_checkpoint(str(source))
                self.assertFalse(handoff_dir.exists())



class TeamCandidateGateTests(unittest.TestCase):
    def test_mergeable_requires_completed_and_verified(self):
        completed = _result("DONE")
        candidate = CandidateResult(1, "test/model", "minimal", MagicMock(), completed)
        candidate.evaluation = {"verification_passed": False}
        self.assertFalse(_candidate_is_mergeable(candidate))
        candidate.evaluation["verification_passed"] = True
        self.assertTrue(_candidate_is_mergeable(candidate))
        candidate.run.status = "paused"
        self.assertFalse(_candidate_is_mergeable(candidate))

    def test_verification_root_selects_changed_nested_project(self):
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            project = root / "aicoder-experimental"
            project.mkdir()
            (project / "pyproject.toml").write_text('[project]\nname="demo"\nversion="0.1"\n', encoding="utf-8")
            (root / ".benchmarks").mkdir()
            delta = {"changed": [".benchmarks", "aicoder-experimental/aicoder/app.py"], "deleted": []}
            self.assertEqual(_verification_root_for_delta(root, delta), project)

class TeamResearchToolPolicyTests(unittest.TestCase):
    def test_research_workers_only_receive_read_only_evidence_tools(self):
        self.assertTrue({"git", "code_read", "code_search", "search"}.issubset(_RESEARCH_TOOL_NAMES))
        self.assertTrue({"binary_exec", "test", "lint", "shell", "config_set", "mail_send"}.isdisjoint(_RESEARCH_TOOL_NAMES))



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
    def test_incomplete_envelope_retries_advisor_with_short_unlimited_recovery(self):
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
        self.assertEqual(sum(call.args[0] for call in sleep_mock.call_args_list), 2.0)

    def test_empty_advisor_response_retries_until_valid_response(self):
        model_client = MagicMock()
        model_client.chat.side_effect = [
            {"response": "", "model": "test/model"},
            {"response": "PLAN OK", "model": "test/model"},
        ]
        with patch("aicoder.team_orchestrator.time.sleep") as sleep_mock:
            result = _call_advisor(
                model_client, model="test/model", system="system", prompt="task", max_tokens=64,
                stop_requested=lambda: False,
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.response, "PLAN OK")
        self.assertEqual(model_client.chat.call_count, 2)
        self.assertEqual(sum(call.args[0] for call in sleep_mock.call_args_list), 1.0)

    def test_transient_advisor_connection_failure_retries_beyond_old_budget(self):
        model_client = MagicMock()
        model_client.chat.side_effect = (
            [RuntimeError("HTTP 503 Service Unavailable")] * 6
            + [{"response": "RECOVERED", "model": "test/model"}]
        )
        with patch("aicoder.team_orchestrator.time.sleep"):
            result = _call_advisor(
                model_client, model="test/model", system="system", prompt="task", max_tokens=64,
                stop_requested=lambda: False,
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.response, "RECOVERED")
        self.assertEqual(model_client.chat.call_count, 7)

    def test_nontransient_advisor_error_still_fails_without_retry(self):
        model_client = MagicMock()
        model_client.chat.side_effect = RuntimeError("invalid API key")
        with patch("aicoder.team_orchestrator.time.sleep") as sleep_mock:
            result = _call_advisor(
                model_client, model="test/model", system="system", prompt="task", max_tokens=64,
                stop_requested=lambda: False,
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(model_client.chat.call_count, 1)
        sleep_mock.assert_not_called()

    def test_advisor_transient_recovery_honors_user_stop(self):
        model_client = MagicMock()
        model_client.chat.side_effect = RuntimeError("connection refused")
        stop_checks = {"count": 0}
        def stopped():
            stop_checks["count"] += 1
            return stop_checks["count"] >= 3
        with patch("aicoder.team_orchestrator.time.sleep"):
            result = _call_advisor(
                model_client, model="test/model", system="system", prompt="task", max_tokens=64,
                stop_requested=stopped,
            )
        self.assertEqual(result.status, "failed")
        self.assertIn("stopped by user", result.error)


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
        self.assertGreaterEqual(sum(call.args[0] for call in sleep_mock.call_args_list), 1.0)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0][0])
        self.assertIn("Automatic runtime resume 1/unlimited", calls[1][0])
        self.assertEqual(calls[1][1], [{"role": "assistant", "content": "partial"}])
        self.assertTrue(any(kind == "team_worker_event" and payload.get("phase") == "auto_resume" for kind, payload in events))

    def test_generic_transient_worker_recovery_is_unlimited_and_preserves_context(self):
        reason = "Transient model/backend failure after request retries were exhausted: connection reset"
        paused = AgentRunResult(
            status="paused", response=reason, model="test/model",
            messages=[{"role": "system", "content": "sys"}, {"role": "assistant", "content": "work evidence"}],
            tools=[], system_prompt="sys",
        )
        completed = AgentRunResult(
            status="completed", response="DONE: recovered", model="test/model",
            messages=[], tools=[], system_prompt="sys",
        )
        calls = []
        def run_once(prompt, conversation):
            calls.append((prompt, conversation))
            return completed if len(calls) == 7 else paused
        with patch("aicoder.team_orchestrator.time.sleep"):
            result = _run_worker_with_auto_resume(
                run_once, role="coder:2", event_fn=None, stop_requested=lambda: False,
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 7)
        self.assertIn("Automatic runtime resume 6/unlimited", calls[-1][0])
        self.assertEqual(calls[-1][1], [{"role": "assistant", "content": "work evidence"}])

    def test_incomplete_envelope_uses_fresh_chat_without_recovery_budget(self):
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
        self.assertEqual(sum(call.args[0] for call in sleep_mock.call_args_list), 2.0)
        self.assertIn("FRESH RECOVERY CHAT 1/unlimited", calls[1][0])
        self.assertIn("partial", calls[1][0])
        self.assertIsNone(calls[1][1], "incomplete-envelope recovery must start a fresh provider chat")

    def test_incomplete_envelope_recovery_continues_beyond_five_fresh_sessions(self):
        reason = "Transient model/backend failure after request retries were exhausted: Transient incomplete chat response: no recognized assistant response envelope; keys=[_transport_telemetry]"
        paused = AgentRunResult(
            status="paused", response=reason, model="test/model",
            messages=[{"role": "assistant", "content": "preserved evidence"}], tools=[], system_prompt="sys",
        )
        completed = AgentRunResult(
            status="completed", response="DONE: recovered after provider congestion", model="test/model",
            messages=[], tools=[], system_prompt="sys",
        )
        calls = []
        def run_once(prompt, conversation):
            calls.append((prompt, conversation))
            return completed if len(calls) == 8 else paused
        with patch("aicoder.team_orchestrator.time.sleep"):
            result = _run_worker_with_auto_resume(
                run_once, role="coder:3", event_fn=None, stop_requested=lambda: False,
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 8)
        self.assertIn("FRESH RECOVERY CHAT 7/unlimited", calls[-1][0])
        self.assertIsNone(calls[-1][1])

    def test_incomplete_envelope_recovery_uses_progressive_backoff(self):
        reason = "Transient model/backend failure after request retries were exhausted: Transient incomplete chat response: no recognized assistant response envelope; keys=[_transport_telemetry]"
        paused = AgentRunResult(
            status="paused", response=reason, model="test/model", messages=[], tools=[], system_prompt="sys",
        )
        completed = AgentRunResult(
            status="completed", response="DONE: recovered", model="test/model", messages=[], tools=[], system_prompt="sys",
        )
        calls = []
        def run_once(prompt, conversation):
            calls.append((prompt, conversation))
            return completed if len(calls) == 7 else paused
        delays = []
        with patch("aicoder.team_orchestrator._interruptible_recovery_sleep", side_effect=lambda delay, _stop: delays.append(delay) or True):
            result = _run_worker_with_auto_resume(
                run_once, role="coder:2", event_fn=None, stop_requested=lambda: False,
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(delays, [2.0, 4.0, 8.0, 15.0, 30.0, 30.0])

    def test_recovery_warns_every_ten_attempts_without_stopping(self):
        reason = "Agent paused because the model returned no usable final response after a final-response repair request."
        paused = AgentRunResult(
            status="paused", response=reason, model="test/model", messages=[], tools=[], system_prompt="sys",
        )
        completed = AgentRunResult(
            status="completed", response="DONE: recovered", model="test/model", messages=[], tools=[], system_prompt="sys",
        )
        calls = []
        events = []
        def run_once(prompt, conversation):
            calls.append((prompt, conversation))
            return completed if len(calls) == 12 else paused
        result = _run_worker_with_auto_resume(
            run_once, role="coder:4",
            event_fn=lambda kind, payload: events.append((kind, payload)),
            stop_requested=lambda: False,
        )
        self.assertEqual(result.status, "completed")
        self.assertIn("Automatic runtime resume 10/unlimited", calls[10][0])
        warnings = [payload for kind, payload in events if kind == "team_worker_event" and payload.get("status") == "warning"]
        self.assertTrue(any("after 10 attempts" in payload.get("message", "") for payload in warnings))

    def test_fresh_recovery_handoff_preserves_text_and_tool_calls(self):
        reason = "Transient model/backend failure after request retries were exhausted: Transient incomplete chat response: no recognized assistant response envelope; keys=[_transport_telemetry]"
        paused = AgentRunResult(
            status="paused", response=reason, model="test/model",
            messages=[
                {"role": "system", "content": "sys"},
                {
                    "role": "assistant",
                    "content": "I inspected the target before editing.",
                    "tool_calls": [{"id": "call-1", "name": "file_read", "arguments": {"path": "a.py"}}],
                },
            ],
            tools=[], system_prompt="sys",
        )
        completed = AgentRunResult(
            status="completed", response="DONE: recovered", model="test/model",
            messages=[], tools=[], system_prompt="sys",
        )
        calls = []
        def run_once(prompt, conversation):
            calls.append((prompt, conversation))
            return paused if len(calls) == 1 else completed
        with patch("aicoder.team_orchestrator.time.sleep"):
            result = _run_worker_with_auto_resume(
                run_once, role="coder:1", event_fn=None, stop_requested=lambda: False,
            )
        self.assertEqual(result.status, "completed")
        handoff = calls[1][0]
        self.assertIn("I inspected the target before editing.", handoff)
        self.assertIn("call-1", handoff)
        self.assertIn("file_read", handoff)

    def test_worker_recovery_cooldown_honors_stop_request(self):
        reason = "Transient model/backend failure after request retries were exhausted: Transient incomplete chat response: no recognized assistant response envelope; keys=[_transport_telemetry]"
        paused = AgentRunResult(
            status="paused", response=reason, model="test/model",
            messages=[{"role": "system", "content": "sys"}], tools=[], system_prompt="sys",
        )
        calls = []
        stop_checks = {"count": 0}
        def run_once(prompt, conversation):
            calls.append((prompt, conversation))
            return paused
        def stop_requested():
            stop_checks["count"] += 1
            return stop_checks["count"] >= 3
        with patch("aicoder.team_orchestrator.time.monotonic", side_effect=[0.0, 0.0, 0.5]), \
             patch("aicoder.team_orchestrator.time.sleep") as sleep_mock:
            result = _run_worker_with_auto_resume(
                run_once, role="coder:1", event_fn=None, stop_requested=stop_requested,
            )
        self.assertEqual(result.status, "paused")
        self.assertEqual(len(calls), 1, "stop during cooldown must prevent a fresh model call")
        sleep_mock.assert_called_once()

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
        self.assertIn("Automatic runtime resume 1/unlimited", calls[3])
        self.assertIn("Automatic runtime resume 2/unlimited", calls[4])

    def test_advisor_incomplete_envelope_uses_progressive_backoff_and_warns(self):
        from aicoder.team_orchestrator import _call_advisor
        from aicoder.client import ClientError
        model_client = MagicMock()
        model_client.chat.side_effect = [
            ClientError("Transient incomplete chat response: no recognized assistant response envelope; keys=['_transport_telemetry']")
            for _ in range(10)
        ] + [{"response": "ok", "model": "test/model"}]
        events = []
        with patch("aicoder.team_orchestrator._interruptible_recovery_sleep", return_value=True) as sleeper:
            result = _call_advisor(
                model_client, model="test/model", system="sys", prompt="task",
                event_fn=lambda kind, payload: events.append((kind, payload)),
                role="plan_research", stop_requested=lambda: False,
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual([call.args[0] for call in sleeper.call_args_list[:5]], [2.0, 4.0, 8.0, 15.0, 30.0])
        self.assertEqual(sleeper.call_args_list[9].args[0], 30.0)
        self.assertTrue(any(payload.get("status") == "warning" and "10 attempts" in payload.get("message", "") for kind, payload in events if kind == "team_worker_event"))

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
                    f"    def test_value(self): self.assertEqual(app.value, {slot})\n", encoding="utf-8",
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


class TeamVerificationRecoveryTests(unittest.TestCase):
    @staticmethod
    def _verification(ok: bool, output: str) -> list[VerificationResult]:
        return [VerificationResult(
            name="python-tests", argv=["python3", "-m", "pytest", "-q"], ok=ok,
            exit_code=0 if ok else 1, elapsed_ms=3, output=output, required=True,
        )]

    def _run(self, workspace: Path, ram_dir: str, verification_side_effect):
        source = workspace / "project"
        source.mkdir()
        (source / "app.py").write_text("value = 0\n", encoding="utf-8")
        (source / "pyproject.toml").write_text(
            "[project]\nname=\"demo\"\nversion=\"0.1.0\"\n", encoding="utf-8",
        )
        (source / "tests").mkdir()
        (source / "tests" / "test_app.py").write_text(
            "def test_value():\n    assert True\n", encoding="utf-8",
        )
        state = {
            "selected_model": "test/model", "team_runtime_mode": "on",
            "runtime_mode": "native-light", "workspace_mode": "ram",
            "workspace_root": str(workspace),
            "team_research_model_1": "test/model", "team_research_model_2": "",
            "team_research_model_3": "", "team_research_model_4": "",
            "team_planner_model": "test/model", "team_coordinator_model": "",
            "team_coder_model_1": "test/model", "team_coder_model_2": "",
            "team_coder_model_3": "", "team_coder_model_4": "",
            "team_merge_model": "test/model", "team_test_planner_model": "test/model",
            "team_brainstorm_rounds": 1,
        }
        config = config_from_state(state)

        def researcher(**kwargs):
            return AgentStageResult(
                role=f"research:{kwargs['role']}", model=kwargs["model"], status="completed",
                response="evidence", elapsed_ms=1,
            )

        def advisor(_model_client, *, model, system, prompt, max_tokens=0, **_kwargs):
            return AgentStageResult("advisor", model, "completed", "shared plan", 1)

        def candidate(**kwargs):
            backend = RamWorkspace(source, ram_root=ram_dir)
            backend.prepare()
            (backend.info.execution_root / "app.py").write_text("value = 1\n", encoding="utf-8")
            (backend.info.execution_root / "tests" / "test_app.py").write_text(
                "def test_value():\n    assert 1 == 1\n", encoding="utf-8",
            )
            return CandidateResult(
                kwargs["slot"], kwargs["model"], kwargs["strategy"], backend,
                _result("DONE: candidate"),
            )

        def evaluate(item):
            return {
                "score": 90, "delta": item.workspace.delta_summary(),
                "checks": {"python-tests": {"ok": True}}, "diff": "candidate",
                "candidate_id": "cand-one", "content_fingerprint": "fp-one",
                "verification_passed": True,
            }

        def create_backend(root, mode, **kwargs):
            return RamWorkspace(root, ram_root=ram_dir)

        FakeIntegrationRuntime.calls = 0
        with (
            patch("aicoder.team_orchestrator.load_tools", return_value=[]),
            patch("aicoder.team_orchestrator._run_researcher", side_effect=researcher),
            patch("aicoder.team_orchestrator._call_advisor", side_effect=advisor),
            patch("aicoder.team_orchestrator._run_candidate", side_effect=candidate),
            patch("aicoder.team_orchestrator.evaluate_candidate", side_effect=evaluate),
            patch("aicoder.team_orchestrator.create_isolated_team_workspace", side_effect=create_backend),
            patch("aicoder.team_orchestrator.NativeLightRuntime", FakeIntegrationRuntime),
            patch("aicoder.team_orchestrator.execute_verification_plan", side_effect=verification_side_effect) as verify,
        ):
            result = run_team(
                task="Implement feature", state=state, config=config, client=MagicMock(),
                model_client=MagicMock(), source_workspace=str(source),
            )
        return source, result, verify

    def test_failed_gate_gets_one_debug_pass_then_full_reverification_and_write(self):
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as ram_dir:
            workspace = Path(workspace_dir)
            source, result, verify = self._run(
                workspace, ram_dir,
                [self._verification(False, "assertion failed"), self._verification(True, "518 passed")],
            )
            self.assertEqual(result.status, "completed", result.error)
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(FakeIntegrationRuntime.calls, 2, "merge plus exactly one debug runtime")
            self.assertTrue(any(stage.role == "debug_tests" for stage in result.stages))
            self.assertEqual((source / "integrated.txt").read_text(encoding="utf-8"), "final\n")
            backup = Path(result.performance["backup_path"])
            self.assertTrue((backup / "manifest.json").is_file())
            self.assertEqual((backup / "before" / "app.py").read_text(encoding="utf-8"), "value = 0\n")
            self.assertIn("assertion failed", (backup / "initial-verification.json").read_text(encoding="utf-8"))
            self.assertIn("518 passed", (backup / "verification.json").read_text(encoding="utf-8"))

    def test_failed_gate_after_debug_preserves_repo_and_never_writes_source(self):
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as ram_dir:
            workspace = Path(workspace_dir)
            source, result, verify = self._run(
                workspace, ram_dir,
                [self._verification(False, "first failure"), self._verification(False, "still failing")],
            )
            self.assertEqual(result.status, "failed")
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(FakeIntegrationRuntime.calls, 2, "merge plus one and only one debug runtime")
            failed = Path(result.performance["failed_workspace"])
            self.assertEqual(failed.parent, workspace)
            self.assertTrue(failed.name.startswith("test_fail_"))
            self.assertEqual((failed / "integrated.txt").read_text(encoding="utf-8"), "final\n")
            self.assertTrue((failed / ".aicoder-failure" / "run.json").is_file())
            self.assertIn("still failing", (failed / ".aicoder-failure" / "final-verification.json").read_text(encoding="utf-8"))
            self.assertEqual((source / "app.py").read_text(encoding="utf-8"), "value = 0\n")
            self.assertFalse((source / "integrated.txt").exists())
            self.assertFalse((source / ".backup").exists(), "failed runs must not create a success backup")

    def test_backup_directory_is_excluded_from_future_ram_candidates(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "app.py").write_text("before\n", encoding="utf-8")
            backend = RamWorkspace(source, ram_root=ram_dir)
            backend.prepare()
            (backend.info.execution_root / "app.py").write_text("after\n", encoding="utf-8")
            backup = _create_run_backup(
                backend, run_id="run-test", verification=[{"name": "tests", "ok": True}], task="demo",
            )
            self.assertTrue(backup.is_dir())
            second = RamWorkspace(source, ram_root=ram_dir)
            second.prepare()
            try:
                self.assertFalse((second.info.execution_root / ".backup").exists())
            finally:
                second.abort(); backend.abort()


if __name__ == "__main__":
    unittest.main()

class MergeRecoveryRequiredGuardTests(unittest.TestCase):
    def test_recovery_required_final_merge_is_contradictory_success(self):
        text = "[MERGE_RESULT]\nstatus: recovery_required\nverification: incomplete\nDONE: merge complete"
        self.assertTrue(_merge_completion_contradiction(text))


class CoderSchedulingPolicyTests(unittest.TestCase):
    def test_all_configured_coders_receive_worker_capacity(self):
        self.assertEqual(_coder_pool_size(4), 4)
        self.assertIsNone(CODER_MAX_ITERATIONS)


class WorkingProjectRootTests(unittest.TestCase):
    def test_named_nested_project_is_used_for_team_working_set(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            project = workspace / "aicoder-experimental"
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]\nname='aicoder'\n", encoding="utf-8")
            unrelated = workspace / "triforce"
            unrelated.mkdir()
            (unrelated / "pyproject.toml").write_text("[project]\nname='triforce'\n", encoding="utf-8")
            resolved = _working_project_root(workspace, "work on aicoder-experimental", "")
            self.assertEqual(resolved, project.resolve())

    def test_invalid_planner_root_cannot_escape_or_override_named_project(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            project = workspace / "aicoder-experimental"
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]\nname='aicoder'\n", encoding="utf-8")
            plan = "[AUTHORITATIVE_PROJECT_ROOT]\n/tmp/does-not-exist\n"
            resolved = _working_project_root(workspace, "work on aicoder-experimental", plan)
            self.assertEqual(resolved, project.resolve())

    def test_ambiguous_multi_project_task_falls_back_to_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            for name in ("alpha", "beta"):
                project = workspace / name
                project.mkdir()
                (project / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            resolved = _working_project_root(workspace, "update all projects", "")
            self.assertEqual(resolved, workspace.resolve())

class TeamPlanGroundingTests(unittest.TestCase):
    def test_rejects_invented_top_level_architecture(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "aicoder").mkdir()
            (root / "tests").mkdir()
            plan = """
[AFFECTED_AREAS]
- /src/security/secrets/
- `src/core/sanitization/adaptive_schema_validator.py`
- tests/security/test_secrets_scrubber.py
"""
            issues = _plan_grounding_issues(plan, str(root))
            self.assertTrue(any("src/" in issue for issue in issues))
            self.assertFalse(any("tests/" in issue for issue in issues))

    def test_allows_new_files_inside_existing_project_areas(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "aicoder").mkdir()
            (root / "tests").mkdir()
            plan = """
[AFFECTED_AREAS]
- `aicoder/plan_grounding.py` (new)
- `tests/test_plan_grounding.py` (new)
"""
            self.assertEqual(_plan_grounding_issues(plan, str(root)), [])

    def test_authoritative_absolute_root_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "aicoder").mkdir()
            plan = f"[AUTHORITATIVE_PROJECT_ROOT]\n`{root}`\n[AFFECTED_AREAS]\n- aicoder/runtime.py"
            self.assertEqual(_plan_grounding_issues(plan, str(root)), [])

    def test_all_configured_coders_receive_worker_capacity(self):
        self.assertEqual(_coder_pool_size(1), 1)
        self.assertEqual(_coder_pool_size(4), 4)


class CandidateTestEvidenceGateTests(unittest.TestCase):
    def _candidate(self, source: Path, ram_dir: str, *, change_test: bool) -> CandidateResult:
        backend = RamWorkspace(source, ram_root=ram_dir)
        backend.prepare()
        (backend.info.execution_root / "app.py").write_text("value = 2\n", encoding="utf-8")
        if change_test:
            (backend.info.execution_root / "tests" / "test_app.py").write_text(
                "def test_value():\n    assert 2 == 2\n", encoding="utf-8",
            )
        return CandidateResult(1, "test/model", "robustness/security", backend, _result("DONE: candidate"))

    def test_behavior_change_without_test_change_is_not_verified(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "tests").mkdir()
            (source / "app.py").write_text("value = 1\n", encoding="utf-8")
            (source / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
            candidate = self._candidate(source, ram_dir, change_test=False)
            try:
                passed = [VerificationResult("python-tests", ["python3", "-m", "pytest"], True, 0, 1, "1 passed", True)]
                with patch("aicoder.team_orchestrator.project_verification_plan", return_value=[]), patch(
                    "aicoder.team_orchestrator.execute_verification_plan", return_value=passed
                ):
                    evaluation = evaluate_candidate(candidate)
                self.assertFalse(evaluation["verification_passed"])
                self.assertFalse(evaluation["test_evidence"]["coverage_evidence_ok"])
                self.assertFalse(evaluation["checks"]["test-change-evidence"]["ok"])
            finally:
                candidate.workspace.abort()

    def test_behavior_change_with_test_change_can_be_verified(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "tests").mkdir()
            (source / "app.py").write_text("value = 1\n", encoding="utf-8")
            (source / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
            candidate = self._candidate(source, ram_dir, change_test=True)
            try:
                passed = [VerificationResult("python-tests", ["python3", "-m", "pytest"], True, 0, 1, "1 passed", True)]
                with patch("aicoder.team_orchestrator.project_verification_plan", return_value=[]), patch(
                    "aicoder.team_orchestrator.execute_verification_plan", return_value=passed
                ):
                    evaluation = evaluate_candidate(candidate)
                self.assertTrue(evaluation["verification_passed"])
                self.assertTrue(evaluation["test_evidence"]["coverage_evidence_ok"])
            finally:
                candidate.workspace.abort()

    def test_final_integration_test_evidence_matches_candidate_rule(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "tests").mkdir()
            (source / "app.py").write_text("before\n", encoding="utf-8")
            (source / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
            backend = RamWorkspace(source, ram_root=ram_dir)
            backend.prepare()
            try:
                (backend.info.execution_root / "app.py").write_text("after\n", encoding="utf-8")
                self.assertFalse(_test_evidence_result(backend).ok)
                (backend.info.execution_root / "tests" / "test_app.py").write_text("def test_x():\n    assert 1 == 1\n", encoding="utf-8")
                self.assertTrue(_test_evidence_result(backend).ok)
            finally:
                backend.abort()


class TeamDebugLogTests(unittest.TestCase):
    def test_debug_log_keeps_full_payload_and_redacts_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            with patch("aicoder.team_orchestrator._TEAM_DEBUG_LOG_PATH", path):
                reset_team_debug_log()
                long_text = "X" * 20000
                first = _TeamDebugLog("run-one")
                first.write("team_worker_event", {"response": long_text, "token": "super-secret-value"})
                second = _TeamDebugLog("run-two")
                second.write("team_worker_event", {"response": "second"})
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                self.assertEqual(rows[0]["run_id"], "run-one")
                self.assertEqual(rows[0]["payload"]["response"], long_text)
                self.assertEqual(rows[0]["payload"]["token"], "[REDACTED]")
                self.assertEqual(rows[1]["run_id"], "run-two")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_process_reset_overwrites_previous_session_log(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.jsonl"
            path.write_text("old session\n", encoding="utf-8")
            with patch("aicoder.team_orchestrator._TEAM_DEBUG_LOG_PATH", path):
                reset_team_debug_log()
                self.assertEqual(path.read_text(encoding="utf-8"), "")
                _TeamDebugLog("fresh-run").write("team_start", {"value": 1})
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("old session", text)
                self.assertIn('"run_id": "fresh-run"', text)


class ResearchAutonomousPolicyRegressionTests(unittest.TestCase):
    def test_research_denial_is_not_user_rejection(self):
        self.assertTrue(getattr(_research_approval, "_aicoder_autonomous_policy", False))
        self.assertFalse(_research_approval(
            "binary_exec", {"program": "python3", "arguments": ["-c", "print(1)"]},
        ))
