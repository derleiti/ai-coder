from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import AgentRunResult
from aicoder.team_orchestrator import (
    AgentStageResult, CandidateResult, _acceptance_artifact_paths, _acceptance_artifact_snapshots, _candidate_execution_handoff, _candidate_has_production_delta, _extract_adaptive_work_units, _work_unit_implementer_budget, CodingWorkUnit, _candidate_test_mutation, _command_matches_acceptance, _external_failed_acceptance_commands, _failed_task_acceptance_commands, _is_incomplete_envelope_reason, _redact_debug_value,
    _call_stage_agent_core, _run_candidate, _run_researcher, evaluate_candidate, run_team,
)
from aicoder.team_runtime import config_from_state
from aicoder.task_contract import compile_task_contract
from aicoder.team_handoff import make_handoff
from aicoder.workspace_backend import RamWorkspace


def _result(text: str, model: str = "test/model") -> AgentRunResult:
    return AgentRunResult(
        status="completed", response=text, model=model, messages=[], tools=[], system_prompt="",
    )






class AdaptiveCodingPlanTests(unittest.TestCase):
    def _plan(self, units):
        return "OBJECTIVE:\nDemo\n\nADAPTIVE WORK GRAPH\n```json\n" + json.dumps({"work_units": units}) + "\n```"

    def test_independent_units_remain_parallel_lanes(self):
        units = _extract_adaptive_work_units(self._plan([
            {"id":"parser","title":"Parser","goal":"Implement parser","files":["pkg/parser.py"],"depends_on":[],"acceptance":[],"estimated_input_tokens":20000,"estimated_output_tokens":10000,"risk":"medium"},
            {"id":"cli","title":"CLI","goal":"Implement CLI","files":["pkg/cli.py"],"depends_on":[],"acceptance":[],"estimated_input_tokens":15000,"estimated_output_tokens":8000,"risk":"low"},
        ]), "parent")
        self.assertEqual([u.unit_id for u in units], ["parser", "cli"])
        self.assertEqual(_work_unit_implementer_budget(units[0]), 40500)

    def test_dependency_chain_is_collapsed_into_one_lane(self):
        units = _extract_adaptive_work_units(self._plan([
            {"id":"core","goal":"core","files":["pkg/core.py"],"depends_on":[]},
            {"id":"api","goal":"api","files":["pkg/api.py"],"depends_on":["core"]},
        ]), "parent")
        self.assertEqual(len(units), 1)
        self.assertIn("core", units[0].unit_id)
        self.assertIn("api", units[0].unit_id)

    def test_shared_file_ownership_is_collapsed(self):
        units = _extract_adaptive_work_units(self._plan([
            {"id":"a","goal":"a","files":["pkg/shared.py"],"depends_on":[]},
            {"id":"b","goal":"b","files":["pkg/shared.py"],"depends_on":[]},
        ]), "parent")
        self.assertEqual(len(units), 1)

    def test_invalid_graph_falls_back_without_losing_parent_task(self):
        units = _extract_adaptive_work_units("ADAPTIVE WORK GRAPH\n```json\n{broken\n```", "KEEP THIS TASK")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].unit_id, "full-task")
        self.assertEqual(units[0].task_text("KEEP THIS TASK"), "KEEP THIS TASK")

    def test_planner_estimates_are_bounded(self):
        self.assertEqual(_work_unit_implementer_budget(CodingWorkUnit("a","a","a", estimated_input_tokens=1, estimated_output_tokens=1)), 40000)
        self.assertEqual(_work_unit_implementer_budget(CodingWorkUnit("b","b","b", estimated_input_tokens=200000, estimated_output_tokens=100000)), 120000)
        self.assertEqual(_work_unit_implementer_budget(CodingWorkUnit("c","c","c")), 120000)

    def test_unit_contract_does_not_inherit_parent_global_acceptance(self):
        unit = CodingWorkUnit(
            "parser", "Parser", "Implement parser",
            acceptance=("python -m unittest tests.test_parser",),
        )
        parent = (
            "Requirements:\n- Must support the complete CLI\n"
            "Acceptance checks:\n- python /tmp/full-system-acceptance.py\n"
        )
        contract = compile_task_contract(unit.task_text(parent))
        self.assertEqual(contract.acceptance_commands, ("python -m unittest tests.test_parser",))
        self.assertNotIn("python /tmp/full-system-acceptance.py", contract.acceptance_commands)


class FinisherAcceptancePolicyTests(unittest.TestCase):
    def test_test_mutation_detection(self):
        self.assertTrue(_candidate_test_mutation("file_edit", {"path": "tests/test_cli.py"}))
        self.assertTrue(_candidate_test_mutation("file_edit", {"path": "/tmp/ws/test_parser.py"}))
        self.assertFalse(_candidate_test_mutation("file_edit", {"path": "jsonl_run_audit/cli.py"}))
        self.assertFalse(_candidate_test_mutation("file_read", {"path": "tests/test_cli.py"}))

    def test_production_delta_excludes_tests_and_bookkeeping(self):
        self.assertFalse(_candidate_has_production_delta({
            "added_files": ["tests/test_app.py", ".aicoder-team/coder-handoff.json"],
            "modified_files": [], "deleted_files": [],
        }))
        self.assertTrue(_candidate_has_production_delta({
            "added_files": ["tests/test_app.py", "pkg/core.py"],
            "modified_files": [], "deleted_files": [],
        }))

    def test_acceptance_command_matching_normalizes_python3(self):
        self.assertTrue(_command_matches_acceptance(
            "python3 /tmp/acceptance.py", "python /tmp/acceptance.py"
        ))
        self.assertFalse(_command_matches_acceptance(
            "python -m unittest discover -s tests -v", "python /tmp/acceptance.py"
        ))

    def test_acceptance_artifact_snapshot_and_external_filter(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()
            acceptance = root / "accept.py"
            acceptance.write_text("assert True\n")
            contract = compile_task_contract(
                f"Acceptance checks:\n- python {acceptance}\n- python -m unittest discover -s tests -v\n"
            )
            self.assertEqual(_acceptance_artifact_paths(contract), (acceptance.resolve(),))
            snapshots = _acceptance_artifact_snapshots(contract)
            self.assertEqual(snapshots[0]["path"], str(acceptance.resolve()))
            self.assertIn("assert True", snapshots[0]["content"])
            evaluation = {"checks": {
                "task-acceptance-1": {"ok": False, "argv": ["python3", str(acceptance)]},
                "task-acceptance-2": {"ok": False, "argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]},
            }}
            self.assertEqual(
                _external_failed_acceptance_commands(evaluation, contract, ws),
                [f"python3 {acceptance}"],
            )

    def test_failed_acceptance_commands_preserve_check_order(self):
        contract = compile_task_contract(
            "Acceptance checks:\n- python /tmp/a.py\n- python -m unittest discover -s tests -v\n"
        )
        evaluation = {"checks": {
            "task-acceptance-2": {"ok": False, "argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]},
            "task-acceptance-1": {"ok": False, "argv": ["python3", "/tmp/a.py"]},
        }}
        self.assertEqual(_failed_task_acceptance_commands(evaluation, contract), [
            "python3 /tmp/a.py", "python3 -m unittest discover -s tests -v"
        ])

class CoderHandoffProjectionTests(unittest.TestCase):
    def test_large_stageoff_is_bounded_without_losing_task_contract_or_acceptance(self):
        task = (
            "Requirements:\n"
            "- Preserve malformed input handling.\n"
            "- Keep deterministic JSON output.\n"
            "Acceptance checks:\n"
            "- python /tmp/external-acceptance.py\n"
        )
        contract = compile_task_contract(task)
        large_noise = "old research evidence " * 6000
        code_contract = (
            "OBJECTIVE:\nImplement parser.\n\n"
            "REQUIREMENTS:\nPreserve malformed input handling.\n\n"
            "ACCEPTANCE TESTS:\npython /tmp/external-acceptance.py\n"
        )
        stageoff = {
            "schema": "aicoder-stageoff-v1",
            "user_task": task,
            "task_contract": contract.as_dict(),
            "repository_context": "repo-root=/tmp/project",
            "session_memory": large_noise,
            "working_memory": {
                "required_changes": "fix parser only",
                "completed_items": "research complete",
                "open_items": "external acceptance still red",
                "risks": "do not regress stdin",
                "next_stage_instructions": "implement then verify",
            },
            "runtime_truth": {"implementation_state": "not_runtime_verified"},
            "stages": [
                {
                    "stage": "plan_code",
                    "output": {"implementation_contract": code_contract},
                    "coordinator_review": "Acceptance is authoritative.",
                }
            ],
        }
        raw = json.dumps(stageoff)
        self.assertGreater(len(raw), 100_000)
        parent = make_handoff("stageoff", raw, max_chars=120_000, source_stage="plan_code")
        handoff = _candidate_execution_handoff(parent, task=task, contract=contract, strategy="minimal")
        self.assertLessEqual(handoff.compact_chars, 24_000)
        self.assertIn("python /tmp/external-acceptance.py", handoff.compact)
        self.assertIn("Preserve malformed input handling", handoff.compact)
        self.assertIn("external acceptance still red", handoff.compact)
        self.assertNotIn("old research evidence old research evidence old research evidence", handoff.compact)

class FreshResearchRecoveryTests(unittest.TestCase):
    def test_no_usable_final_response_is_incomplete_envelope(self):
        reason = (
            "Agent paused because the model returned no usable final response after "
            "a final-response repair request. Existing tool results and plan state were preserved for resume."
        )
        self.assertTrue(_is_incomplete_envelope_reason(reason))

    def test_researcher_restarts_with_fresh_chat_and_bounded_handoff(self):
        reason = (
            "Agent paused because the model returned no usable final response after "
            "a final-response repair request. Existing tool results and plan state were preserved for resume."
        )
        calls = []

        class Runtime:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def run(self):
                if len(calls) == 1:
                    return AgentRunResult(
                        "paused", reason, "test/model",
                        [
                            {"role": "system", "content": "old system"},
                            {"role": "user", "content": "research the task"},
                            {"role": "assistant", "content": "evidence gathered before malformed final"},
                        ],
                        [], "system",
                    )
                return AgentRunResult(
                    "completed", 'FINDINGS:\nrecovered fact\nSOURCES:\nsource-id\nAPPLICABILITY:\napplies\nRISKS:\nnone\nRECOMMENDATIONS:\ncontinue', "test/model",
                    [{"role": "assistant", "content": 'FINDINGS:\nrecovered fact\nSOURCES:\nsource-id\nAPPLICABILITY:\napplies\nRISKS:\nnone\nRECOMMENDATIONS:\ncontinue'}], [], "system",
                )

        events = []
        with tempfile.TemporaryDirectory() as tmp, patch(
            "aicoder.team_orchestrator.NativeLightRuntime", Runtime
        ):
            result = _run_researcher(
                client=MagicMock(), model_client=MagicMock(), model="test/model",
                role="primary_sources", task="research task", source_workspace=tmp, tools=[],
                stop_requested=None, research_plan="find evidence",
                event_fn=lambda kind, payload: events.append((kind, payload)),
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["conversation"], [])
        self.assertIn("FRESH RESEARCH:PRIMARY_SOURCES RECOVERY CHAT 1", calls[1]["initial_prompt"])
        self.assertIn("evidence gathered before malformed final", calls[1]["initial_prompt"])
        recovery = [
            payload for kind, payload in events
            if kind == "team_worker_event" and payload.get("category") == "recovery"
        ]
        self.assertTrue(recovery)
        self.assertEqual(recovery[-1].get("status"), "fresh_chat")


class StageProviderResumeContextTests(unittest.TestCase):
    def test_stage_provider_resume_repeats_authoritative_original_task(self):
        calls = []
        original = "ORIGINAL-STAGE-TASK-UNIQUE-9182"

        class Runtime:
            def __init__(self, **kwargs):
                calls.append(kwargs)
            def run(self):
                if len(calls) == 1:
                    return AgentRunResult(
                        "paused", "provider temporary failure", "test/model",
                        [{"role":"user","content":"partial context"}], [], "system",
                        error="provider temporary failure", failure_category="transient",
                    )
                return AgentRunResult(
                    "completed", "SECTION:\nfinished", "test/model",
                    [{"role":"assistant","content":"SECTION:\nfinished"}], [], "system",
                )

        with tempfile.TemporaryDirectory() as tmp, patch(
            "aicoder.team_orchestrator.NativeLightRuntime", Runtime
        ), patch("aicoder.team_orchestrator._wait_before_resume", return_value=True):
            result = _call_stage_agent_core(
                client=MagicMock(), model_client=MagicMock(), model="test/model",
                system="stage system", prompt=original, tools=[], workspace_root=tmp,
                event_fn=None, role="coordinator:test", stop_requested=None, approval_fn=None,
                required_sections=("SECTION",), max_iterations=4,
            )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 2)
        self.assertIn("AUTHORITATIVE ORIGINAL STAGE TASK", calls[1]["initial_prompt"])
        self.assertIn(original, calls[1]["initial_prompt"])


class FakeIntegrationRuntime:
    calls = 0

    def __init__(self, *, workspace_root: str, model: str, **kwargs):
        self.workspace_root = Path(workspace_root)
        self.model = model

    def run(self):
        FakeIntegrationRuntime.calls += 1
        marker = self.workspace_root / "integrated.txt"
        if FakeIntegrationRuntime.calls == 1:
            evidence_path = self.workspace_root / ".aicoder-team" / "candidates.json"
            if evidence_path.exists():
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                for item in evidence:
                    for rel in item.get("delta", {}).get("added_files", []):
                        if rel == "candidate_one.py":
                            source = self.workspace_root / item["snapshot"] / rel
                            target = self.workspace_root / rel
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source, target)
            marker.write_text("merged\n", encoding="utf-8")
            return _result("DONE: merge", self.model)
        marker.write_text("final\n", encoding="utf-8")
        return _result("DONE: final", self.model)


class TeamOrchestratorFlowTests(unittest.TestCase):
    def test_debug_redaction_masks_inline_secrets(self):
        rendered = _redact_debug_value({"message": "token=super-secret-value"})
        self.assertEqual(rendered["message"], "token=[REDACTED]")

    def test_team_container_workspace_creates_and_persists_task_project_root(self):
        with tempfile.TemporaryDirectory() as temp:
            projects = Path(temp) / "workspace"
            projects.mkdir()
            target = projects / "new-project"
            state = {
                "projects_root": str(projects),
                "workspace_root": str(projects),
                "selected_model": "test/model",
                "team_runtime_mode": "on",
                "workspace_mode": "ram",
                "team_research_model_1": "test/model",
                "team_research_model_2": "",
                "team_research_model_3": "",
                "team_research_model_4": "",
                "team_planner_model": "test/model",
                "team_coordinator_model": "",
                "team_coder_model_1": "test/model",
                "team_coder_model_2": "",
                "team_coder_model_3": "",
                "team_coder_model_4": "",
                "team_merge_model": "",
                "team_test_planner_model": "",
            }
            config = config_from_state(state)
            events = []
            with (
                patch("aicoder.session_state.set_workspace") as persist,
                patch("aicoder.team_orchestrator.load_tools", side_effect=RuntimeError("stop-after-workspace")),
            ):
                result = run_team(
                    task=f"Create project at {target}",
                    state=state, config=config, client=MagicMock(), model_client=MagicMock(),
                    source_workspace=str(projects),
                    event_fn=lambda kind, payload: events.append((kind, payload)),
                )

            self.assertEqual(result.status, "failed")
            self.assertIn("stop-after-workspace", result.error)
            self.assertTrue(target.is_dir())
            persist.assert_called_once_with(str(target.resolve()))
            self.assertEqual(state["workspace_root"], str(target.resolve()))
            project_events = [payload for kind, payload in events if kind == "team_project_workspace"]
            self.assertEqual(project_events[-1]["path"], str(target.resolve()))
            self.assertEqual(project_events[-1]["reason"], "task-project-path")

    def test_completed_candidate_gets_automatic_verification_repair(self):
        class RepairRuntime:
            calls = 0
            prompts = []

            def __init__(self, *, workspace_root: str, initial_prompt: str, model: str, **kwargs):
                self.workspace_root = Path(workspace_root)
                self.initial_prompt = initial_prompt
                self.model = model

            def run(self):
                type(self).calls += 1
                type(self).prompts.append(self.initial_prompt)
                if type(self).calls == 1:
                    (self.workspace_root / "app.py").write_text("value = 1\n", encoding="utf-8")
                else:
                    (self.workspace_root / "tests" / "test_app.py").write_text(
                        "import unittest\nimport app\nclass T(unittest.TestCase):\n"
                        "    def test_value(self): self.assertEqual(app.value, 1)\n",
                        encoding="utf-8",
                    )
                result = AgentRunResult(
                    "completed", "DONE: candidate", self.model,
                    [{"role": "assistant", "content": "DONE: candidate"}], [], "system",
                )
                result.performance = {}
                return result

        RepairRuntime.calls = 0
        RepairRuntime.prompts = []
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "app.py").write_text("value = 0\n", encoding="utf-8")
            (source / "pyproject.toml").write_text(
                '[project]\nname="demo"\nversion="0.1.0"\n', encoding="utf-8"
            )
            (source / "tests").mkdir()
            (source / "tests" / "test_app.py").write_text(
                "import unittest\nimport app\nclass T(unittest.TestCase):\n"
                "    def test_value(self): self.assertGreaterEqual(app.value, 0)\n",
                encoding="utf-8",
            )

            def create_backend(root, mode, **kwargs):
                return RamWorkspace(root, ram_root=ram_dir)

            with (
                patch("aicoder.team_orchestrator.NativeLightRuntime", RepairRuntime),
                patch("aicoder.team_orchestrator.create_isolated_team_workspace", side_effect=create_backend),
            ):
                candidate = _run_candidate(
                    client=MagicMock(), model_client=MagicMock(), source_workspace=str(source),
                    backend_mode="ram", slot=1, model="test/model", strategy="minimal",
                    task="change value", plan="implement and test", coordinator="", tools=[],
                    stop_requested=None,
                )

            self.assertEqual(candidate.run.status, "completed")
            self.assertEqual(RepairRuntime.calls, 2)
            self.assertIn("FRESH TEST + REPAIR CODER PROCESS", RepairRuntime.prompts[1])
            self.assertIn("GEGEBEN", RepairRuntime.prompts[1])
            self.assertIn("FERTIG", RepairRuntime.prompts[1])
            self.assertIn("GESUCHT_ZU_MACHEN", RepairRuntime.prompts[1])
            self.assertIn("test-change-evidence", RepairRuntime.prompts[1])
            self.assertIn("workspace_is_authoritative", RepairRuntime.prompts[1])
            final = evaluate_candidate(candidate)
            self.assertTrue(final["verification_passed"], final)
            self.assertEqual(candidate.run.performance.get("team_coder_phases"), 2)
            candidate.workspace.abort()

    def test_paused_candidate_after_resume_limit_gets_verification_repair(self):
        class PausedRepairRuntime:
            calls = 0
            prompts = []

            def __init__(self, *, workspace_root: str, initial_prompt: str, model: str, **kwargs):
                self.workspace_root = Path(workspace_root)
                self.initial_prompt = initial_prompt
                self.model = model

            def run(self):
                type(self).calls += 1
                type(self).prompts.append(self.initial_prompt)
                if type(self).calls == 1:
                    (self.workspace_root / "app.py").write_text("value = 1\n", encoding="utf-8")
                if type(self).calls == 1:
                    result = AgentRunResult(
                        "paused",
                        "Agent paused because the model returned no usable final response after a final-response repair request.",
                        self.model, [{"role": "assistant", "content": "implementation in progress"}], [], "system",
                    )
                else:
                    (self.workspace_root / "tests" / "test_app.py").write_text(
                        "import unittest\nimport app\nclass T(unittest.TestCase):\n"
                        "    def test_value(self): self.assertEqual(app.value, 1)\n",
                        encoding="utf-8",
                    )
                    result = AgentRunResult(
                        "completed", "DONE: repaired candidate", self.model,
                        [{"role": "assistant", "content": "DONE: repaired candidate"}], [], "system",
                    )
                result.performance = {}
                return result

        PausedRepairRuntime.calls = 0
        PausedRepairRuntime.prompts = []
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "app.py").write_text("value = 0\n", encoding="utf-8")
            (source / "pyproject.toml").write_text(
                '[project]\nname="demo"\nversion="0.1.0"\n', encoding="utf-8"
            )
            (source / "tests").mkdir()
            (source / "tests" / "test_app.py").write_text(
                "import unittest\nimport app\nclass T(unittest.TestCase):\n"
                "    def test_value(self): self.assertEqual(app.value, 0)\n",
                encoding="utf-8",
            )

            def create_backend(root, mode, **kwargs):
                return RamWorkspace(root, ram_root=ram_dir)

            with (
                patch("aicoder.team_orchestrator.NativeLightRuntime", PausedRepairRuntime),
                patch("aicoder.team_orchestrator.create_isolated_team_workspace", side_effect=create_backend),
            ):
                candidate = _run_candidate(
                    client=MagicMock(), model_client=MagicMock(), source_workspace=str(source),
                    backend_mode="ram", slot=1, model="test/model", strategy="minimal",
                    task="change value", plan="implement and test", coordinator="", tools=[],
                    stop_requested=None,
                )

            self.assertEqual(candidate.run.status, "completed")
            self.assertEqual(PausedRepairRuntime.calls, 2)
            self.assertIn("FRESH TEST + REPAIR CODER PROCESS", PausedRepairRuntime.prompts[1])
            self.assertIn("python-tests", PausedRepairRuntime.prompts[1])
            self.assertIn("test-change-evidence", PausedRepairRuntime.prompts[1])
            final = evaluate_candidate(candidate)
            self.assertTrue(final["verification_passed"], final)
            self.assertEqual(candidate.run.performance.get("team_auto_resumes"), 0)
            self.assertEqual(candidate.run.performance.get("team_coder_phases"), 2)
            candidate.workspace.abort()

    def test_paused_candidate_with_real_changes_can_pass_deterministic_verification(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "app.py").write_text("value = 0\n", encoding="utf-8")
            (source / "pyproject.toml").write_text('[project]\nname="demo"\nversion="0.1.0"\n', encoding="utf-8")
            (source / "tests").mkdir()
            (source / "tests" / "test_app.py").write_text(
                "import unittest\nimport app\nclass T(unittest.TestCase):\n    def test_value(self): self.assertEqual(app.value, 0)\n",
                encoding="utf-8",
            )
            backend = RamWorkspace(source, ram_root=ram_dir)
            execution = backend.prepare()
            (execution / "app.py").write_text("value = 1\n", encoding="utf-8")
            (execution / "tests" / "test_app.py").write_text(
                "import unittest\nimport app\nclass T(unittest.TestCase):\n    def test_value(self): self.assertEqual(app.value, 1)\n",
                encoding="utf-8",
            )
            run = AgentRunResult(
                "paused", "no usable final response", "test/model", [], [], "system"
            )
            candidate = CandidateResult(1, "test/model", "minimal", backend, run)
            result = evaluate_candidate(candidate)
            self.assertTrue(result["verification_passed"], result)
            self.assertGreater(result["score"], 0)
            backend.abort()


    def test_candidate_evaluation_enforces_task_acceptance_checks(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "app.py").write_text("value = 0\n", encoding="utf-8")
            (source / "pyproject.toml").write_text('[project]\nname="demo"\nversion="0.1.0"\n', encoding="utf-8")
            (source / "tests").mkdir()
            (source / "tests" / "test_app.py").write_text(
                "import unittest\nimport app\nclass T(unittest.TestCase):\n    def test_value(self): self.assertEqual(app.value, 0)\n",
                encoding="utf-8",
            )
            backend = RamWorkspace(source, ram_root=ram_dir)
            execution = backend.prepare()
            (execution / "app.py").write_text("value = 1\n", encoding="utf-8")
            (execution / "tests" / "test_app.py").write_text(
                "import unittest\nimport app\nclass T(unittest.TestCase):\n    def test_value(self): self.assertEqual(app.value, 1)\n",
                encoding="utf-8",
            )
            contract = compile_task_contract(
                'Acceptance checks:\n1. python -c "raise SystemExit(7)"\n'
            )
            candidate = CandidateResult(
                1, "test/model", "minimal", backend,
                AgentRunResult("completed", "DONE", "test/model", [], [], "system"),
                task_contract=contract,
            )
            result = evaluate_candidate(candidate)
            self.assertFalse(result["verification_passed"], result)
            self.assertIn("task-acceptance-1", result["checks"])
            self.assertEqual(result["checks"]["task-acceptance-1"]["exit_code"], 7)
            backend.abort()

    def test_failed_candidate_cannot_score_from_unchanged_passing_workspace(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "app.py").write_text("value = 0\n", encoding="utf-8")
            backend = RamWorkspace(source, ram_root=ram_dir)
            backend.prepare()
            run = AgentRunResult(
                "failed", "", "test/model", [], [], "system",
                error='400 "message content must be a string or content-block list"',
            )
            candidate = CandidateResult(1, "test/model", "conservative/minimal-change", backend, run)
            result = evaluate_candidate(candidate)
            self.assertEqual(result["score"], 0)
            self.assertFalse(result["verification_passed"])
            self.assertEqual(result["delta"].get("changed_count", 0), 0)
            backend.abort()

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
            research_source_workspaces = []
            coder_source_workspaces = []
            coder_tasks = []

            def researcher(**kwargs):
                research_source_workspaces.append(kwargs["source_workspace"])
                return AgentStageResult(
                    role=f"research:{kwargs['role']}", model=kwargs["model"], status="completed",
                    response=f"evidence {kwargs['role']}", elapsed_ms=1,
                )

            def stage_agent(**kwargs):
                labels = tuple(kwargs.get("required_sections") or ())
                response = "\n".join(f"{label}:\nvalidated {label.lower()}" for label in labels) or "validated stage"
                return AgentStageResult(str(kwargs.get("role") or "stage"), str(kwargs.get("model") or "test/model"), "completed", response, 1)

            candidates = []
            def candidate(**kwargs):
                coder_source_workspaces.append(kwargs["source_workspace"])
                coder_tasks.append(kwargs.get("task"))
                backend = RamWorkspace(source, ram_root=ram_dir)
                backend.prepare()
                slot = kwargs["slot"]
                (backend.info.execution_root / "app.py").write_text(f"value = {slot}\n", encoding="utf-8")
                if slot == 1:
                    (backend.info.execution_root / "candidate_one.py").write_text("from_non_winner = True\n", encoding="utf-8")
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

            events = []
            with (
                patch("aicoder.team_orchestrator.load_tools", return_value=[]),
                patch("aicoder.team_orchestrator._run_researcher", side_effect=researcher),
                patch("aicoder.team_orchestrator._call_stage_agent", side_effect=stage_agent),
                patch("aicoder.team_orchestrator._run_candidate", side_effect=candidate),
                patch("aicoder.team_orchestrator.evaluate_candidate", side_effect=evaluate),
                patch("aicoder.team_orchestrator.create_isolated_team_workspace", side_effect=create_backend),
                patch("aicoder.team_orchestrator.NativeLightRuntime", FakeIntegrationRuntime),
            ):
                result = run_team(
                    task="Implement feature", state=state, config=config, client=MagicMock(),
                    model_client=MagicMock(), source_workspace=str(source),
                    event_fn=lambda kind, payload: events.append((kind, payload)),
                )

            self.assertEqual(result.status, "completed", result.error)
            expected_workspace = str(source.resolve())
            self.assertEqual(set(research_source_workspaces), {expected_workspace})
            self.assertEqual(set(coder_source_workspaces), {expected_workspace})
            self.assertEqual(set(coder_tasks), {"Implement feature"})
            stageoffs = [payload for kind, payload in events if kind == "team_stageoff"]
            self.assertTrue(stageoffs)
            self.assertEqual(
                stageoffs[0]["stageoff"]["repository_context"].splitlines()[0],
                f"workspace={expected_workspace}",
            )
            self.assertEqual(result.performance["stageoff"]["repository_context"].splitlines()[0], f"workspace={expected_workspace}")
            self.assertEqual(result.performance["winner_candidate_id"], "cand-2")
            self.assertEqual(result.performance["ledger"]["completed"], [
                "plan_research", "research", "brainstorm", "plan_code", "code", "merge_plan", "merge",
                "plan_tests", "tests_function_ok", "atomic_disk_write",
            ])
            self.assertEqual((source / "app.py").read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual((source / "integrated.txt").read_text(encoding="utf-8"), "merged\n")
            self.assertEqual(
                (source / "candidate_one.py").read_text(encoding="utf-8"),
                "from_non_winner = True\n",
            )
            self.assertEqual(result.performance["change_manifest"], {
                "created": ["candidate_one.py", "integrated.txt"],
                "modified": ["app.py"],
                "deleted": [],
            })
            self.assertEqual(events[-1][0], "team_terminal")
            self.assertEqual(events[-1][1]["status"], "completed")
            self.assertEqual(events[-1][1]["progress"], 100)
            self.assertTrue(events[-1][1]["run_id"].startswith("team-"))
            self.assertFalse((source / ".aicoder-team").exists())

    def test_all_coder_failure_releases_candidate_workspaces(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "app.py").write_text("value = 0\n", encoding="utf-8")
            state = {
                "selected_model": "test/model", "team_runtime_mode": "on", "workspace_mode": "ram",
                "team_research_model_1": "test/model", "team_research_model_2": "",
                "team_research_model_3": "", "team_research_model_4": "",
                "team_planner_model": "test/model", "team_coordinator_model": "",
                "team_coder_model_1": "test/model", "team_coder_model_2": "",
                "team_coder_model_3": "", "team_coder_model_4": "",
                "team_merge_model": "", "team_test_planner_model": "",
            }
            config = config_from_state(state)
            created = []

            def researcher(**kwargs):
                return AgentStageResult(f"research:{kwargs['role']}", kwargs["model"], "completed", "evidence", 1)

            def stage_agent(**kwargs):
                labels = tuple(kwargs.get("required_sections") or ())
                response = "\n".join(f"{label}:\nvalidated {label.lower()}" for label in labels) or "validated stage"
                return AgentStageResult(str(kwargs.get("role") or "stage"), str(kwargs.get("model") or "test/model"), "completed", response, 1)

            def candidate(**kwargs):
                backend = RamWorkspace(source, ram_root=ram_dir)
                backend.prepare()
                created.append(backend.info.execution_root)
                run = AgentRunResult("paused", "waiting", kwargs["model"], [], [], "system", error="paused")
                return CandidateResult(kwargs["slot"], kwargs["model"], kwargs["strategy"], backend, run)

            with (
                patch("aicoder.team_orchestrator.load_tools", return_value=[]),
                patch("aicoder.team_orchestrator._run_researcher", side_effect=researcher),
                patch("aicoder.team_orchestrator._call_stage_agent", side_effect=stage_agent),
                patch("aicoder.team_orchestrator._run_candidate", side_effect=candidate),
                patch("aicoder.team_orchestrator.evaluate_candidate", return_value={
                    "score": 0, "delta": {}, "checks": {}, "diff": "", "candidate_id": "cand-fail",
                    "verification_passed": False,
                }),
            ):
                result = run_team(
                    task="task", state=state, config=config, client=MagicMock(),
                    model_client=MagicMock(), source_workspace=str(source),
                )

            self.assertEqual(result.status, "failed")
            self.assertIn("no verified coding candidate completed", result.error)
            self.assertTrue(created)
            self.assertTrue(all(not path.exists() for path in created))

    def test_merge_failure_releases_candidate_and_integration_workspaces(self):
        class FailingMergeRuntime:
            def __init__(self, *, model: str, **kwargs):
                self.model = model

            def run(self):
                return AgentRunResult("failed", "", self.model, [], [], "system", error="merge exhausted")

        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "app.py").write_text("value = 0\n", encoding="utf-8")
            state = {
                "selected_model": "test/model", "team_runtime_mode": "on", "workspace_mode": "ram",
                "team_research_model_1": "test/model", "team_research_model_2": "",
                "team_research_model_3": "", "team_research_model_4": "",
                "team_planner_model": "test/model", "team_coordinator_model": "",
                "team_coder_model_1": "test/model", "team_coder_model_2": "",
                "team_coder_model_3": "", "team_coder_model_4": "",
                "team_merge_model": "test/model", "team_test_planner_model": "",
            }
            config = config_from_state(state)
            candidate_paths = []
            integration_paths = []

            def researcher(**kwargs):
                return AgentStageResult(f"research:{kwargs['role']}", kwargs["model"], "completed", "evidence", 1)

            def stage_agent(**kwargs):
                labels = tuple(kwargs.get("required_sections") or ())
                response = "\n".join(f"{label}:\nvalidated {label.lower()}" for label in labels) or "validated stage"
                return AgentStageResult(str(kwargs.get("role") or "stage"), str(kwargs.get("model") or "test/model"), "completed", response, 1)

            def candidate(**kwargs):
                backend = RamWorkspace(source, ram_root=ram_dir)
                backend.prepare()
                (backend.info.execution_root / "app.py").write_text("value = 1\n", encoding="utf-8")
                candidate_paths.append(backend.info.execution_root)
                return CandidateResult(
                    kwargs["slot"], kwargs["model"], kwargs["strategy"], backend,
                    AgentRunResult("completed", "DONE", kwargs["model"], [], [], "system"),
                )

            def create_backend(root, mode, **kwargs):
                backend = RamWorkspace(root, ram_root=ram_dir)
                integration_paths.append(backend.info.execution_root)
                return backend

            with (
                patch("aicoder.team_orchestrator.load_tools", return_value=[]),
                patch("aicoder.team_orchestrator._run_researcher", side_effect=researcher),
                patch("aicoder.team_orchestrator._call_stage_agent", side_effect=stage_agent),
                patch("aicoder.team_orchestrator._run_candidate", side_effect=candidate),
                patch("aicoder.team_orchestrator.evaluate_candidate", return_value={
                    "score": 100, "delta": {"changed_count": 1, "deleted_count": 0},
                    "checks": {}, "diff": "diff", "candidate_id": "cand-good", "verification_passed": True,
                }),
                patch("aicoder.team_orchestrator.create_isolated_team_workspace", side_effect=create_backend),
                patch("aicoder.team_orchestrator._attach_blind_candidate_snapshots", return_value=[]),
                patch("aicoder.team_orchestrator.NativeLightRuntime", FailingMergeRuntime),
            ):
                result = run_team(
                    task="task", state=state, config=config, client=MagicMock(),
                    model_client=MagicMock(), source_workspace=str(source),
                )

            self.assertEqual(result.status, "failed")
            self.assertIn("merge exhausted", result.error)
            self.assertTrue(candidate_paths and integration_paths)
            self.assertTrue(all(not path.exists() for path in candidate_paths + integration_paths))


    def test_merge_pause_auto_resumes_in_same_integration_workspace(self):
        class PausingMergeRuntime:
            calls = 0
            roots = []
            prompts = []

            def __init__(self, *, workspace_root: str, model: str, initial_prompt: str, **kwargs):
                self.workspace_root = Path(workspace_root)
                self.model = model
                self.initial_prompt = initial_prompt
                self.conversation = kwargs.get("conversation") or []

            def run(self):
                PausingMergeRuntime.calls += 1
                PausingMergeRuntime.roots.append(self.workspace_root)
                PausingMergeRuntime.prompts.append(self.initial_prompt)
                marker = self.workspace_root / "integrated.txt"
                if PausingMergeRuntime.calls == 1:
                    marker.write_text("partial\n", encoding="utf-8")
                    return AgentRunResult(
                        "paused", "Agent paused: state changed successfully, but verification is still required.",
                        self.model, [{"role": "assistant", "content": "partial merge"}], [], "system",
                    )
                marker.write_text("complete\n", encoding="utf-8")
                return _result("DONE: merge complete", self.model)

        PausingMergeRuntime.calls = 0
        PausingMergeRuntime.roots = []
        PausingMergeRuntime.prompts = []
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
            source = Path(source_dir)
            (source / "app.py").write_text("value = 0\n", encoding="utf-8")
            state = {
                "selected_model": "test/model", "team_runtime_mode": "on", "workspace_mode": "ram",
                "team_research_model_1": "test/model", "team_research_model_2": "",
                "team_research_model_3": "", "team_research_model_4": "",
                "team_planner_model": "test/model", "team_coordinator_model": "",
                "team_coder_model_1": "test/model", "team_coder_model_2": "",
                "team_coder_model_3": "", "team_coder_model_4": "",
                "team_merge_model": "test/model", "team_test_planner_model": "",
            }
            config = config_from_state(state)

            def researcher(**kwargs):
                return AgentStageResult(f"research:{kwargs['role']}", kwargs["model"], "completed", "evidence", 1)

            def stage_agent(**kwargs):
                labels = tuple(kwargs.get("required_sections") or ())
                response = "\n".join(f"{label}:\nvalidated {label.lower()}" for label in labels) or "validated stage"
                return AgentStageResult(str(kwargs.get("role") or "stage"), str(kwargs.get("model") or "test/model"), "completed", response, 1)

            def candidate(**kwargs):
                backend = RamWorkspace(source, ram_root=ram_dir)
                backend.prepare()
                (backend.info.execution_root / "app.py").write_text("value = 1\n", encoding="utf-8")
                return CandidateResult(
                    kwargs["slot"], kwargs["model"], kwargs["strategy"], backend,
                    AgentRunResult("completed", "DONE", kwargs["model"], [], [], "system"),
                )

            def create_backend(root, mode, **kwargs):
                return RamWorkspace(root, ram_root=ram_dir)

            events = []
            with (
                patch("aicoder.team_orchestrator.load_tools", return_value=[]),
                patch("aicoder.team_orchestrator._run_researcher", side_effect=researcher),
                patch("aicoder.team_orchestrator._call_stage_agent", side_effect=stage_agent),
                patch("aicoder.team_orchestrator._run_candidate", side_effect=candidate),
                patch("aicoder.team_orchestrator.evaluate_candidate", return_value={
                    "score": 100, "delta": {"changed_count": 1, "deleted_count": 0},
                    "checks": {}, "diff": "diff", "candidate_id": "cand-good", "verification_passed": True,
                }),
                patch("aicoder.team_orchestrator.create_isolated_team_workspace", side_effect=create_backend),
                patch("aicoder.team_orchestrator._attach_blind_candidate_snapshots", return_value=[]),
                patch("aicoder.team_orchestrator.NativeLightRuntime", PausingMergeRuntime),
            ):
                result = run_team(
                    task="task", state=state, config=config, client=MagicMock(),
                    model_client=MagicMock(), source_workspace=str(source),
                    event_fn=lambda kind, payload: events.append((kind, payload)),
                )

            self.assertEqual(result.status, "completed", result.error)
            self.assertEqual(PausingMergeRuntime.calls, 2)
            self.assertEqual(PausingMergeRuntime.roots[0], PausingMergeRuntime.roots[1])
            self.assertIn("AUTONOMOUS MERGE RESUME 1/4", PausingMergeRuntime.prompts[1])
            self.assertTrue(any(kind == "team_merge_resume" for kind, _ in events))
            merge_results = [payload for kind, payload in events if kind == "team_merge_result"]
            self.assertEqual(merge_results[-1]["status"], "completed")
            self.assertEqual(merge_results[-1]["auto_resumes"], 1)
            self.assertEqual((source / "integrated.txt").read_text(encoding="utf-8"), "complete\n")

    def test_keyboard_interrupt_returns_cancelled_terminal_state(self):
        events = []
        with patch(
            "aicoder.team_orchestrator._run_team_pipeline", side_effect=KeyboardInterrupt
        ):
            result = run_team(
                task="task", state={}, config=MagicMock(), client=MagicMock(),
                model_client=MagicMock(), source_workspace=".",
                event_fn=lambda kind, payload: events.append((kind, payload)),
            )

        self.assertEqual(result.status, "cancelled")
        self.assertIn("cancelled by user", result.error)
        self.assertEqual(events[-1][0], "team_terminal")
        self.assertEqual(events[-1][1]["status"], "cancelled")
        self.assertIsNone(events[-1][1]["progress"])


if __name__ == "__main__":
    unittest.main()

class ObservationalWorkspaceIsolationTests(unittest.TestCase):
    def test_stage_agent_discards_accidental_mutation(self):
        from aicoder.team_orchestrator import AgentStageResult, _call_stage_agent

        with tempfile.TemporaryDirectory() as source_dir:
            source = Path(source_dir)
            target = source / "state.txt"
            target.write_text("original\n", encoding="utf-8")

            def fake_core(**kwargs):
                execution = Path(kwargs["workspace_root"])
                (execution / "state.txt").write_text("mutated by planner\n", encoding="utf-8")
                return AgentStageResult(
                    "coordinator:test", "test/model", "completed",
                    f"STAGE SUMMARY:\nread {execution / 'state.txt'}", 1,
                )

            with patch("aicoder.team_orchestrator._call_stage_agent_core", side_effect=fake_core):
                result = _call_stage_agent(
                    client=MagicMock(), model_client=MagicMock(), model="test/model",
                    system="observe", prompt=f"inspect {source}", tools=[], workspace_root=str(source),
                    event_fn=None, role="coordinator:test", stop_requested=None,
                    approval_fn=None,
                )

            self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
            self.assertIn(str(source / "state.txt"), result.response)
            self.assertTrue(result.evidence.get("isolated_observational_workspace"))

    def test_researcher_discards_accidental_mutation(self):
        from aicoder.team_orchestrator import AgentStageResult, _run_researcher

        with tempfile.TemporaryDirectory() as source_dir:
            source = Path(source_dir)
            target = source / "facts.txt"
            target.write_text("original\n", encoding="utf-8")

            def fake_core(**kwargs):
                execution = Path(kwargs["source_workspace"])
                (execution / "facts.txt").write_text("mutated by researcher\n", encoding="utf-8")
                return AgentStageResult(
                    "research:R1", "test/model", "completed",
                    f"FINDINGS:\nread {execution / 'facts.txt'}", 1,
                    evidence={},
                )

            with patch("aicoder.team_orchestrator._run_researcher_core", side_effect=fake_core):
                result = _run_researcher(
                    client=MagicMock(), model_client=MagicMock(), model="test/model", role="R1",
                    source_workspace=str(source), tools=[], stop_requested=None,
                )

            self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
            self.assertIn(str(source / "facts.txt"), result.response)
            self.assertTrue(result.evidence.get("isolated_observational_workspace"))


def test_candidate_conversation_can_be_bounded_without_orphaning_tool_result():
    from aicoder.agent_runtime import AgentRunResult
    from aicoder.team_orchestrator import _candidate_conversation
    messages = [
        {"role":"system","content":"sys"},
        {"role":"user","content":"old" * 10000},
        {"role":"assistant","content":"" ,"tool_calls":[{"id":"c1","type":"function","function":{"name":"file_read","arguments":"{}"}}]},
        {"role":"tool","tool_call_id":"c1","name":"file_read","content":"latest-result"},
        {"role":"user","content":"continue"},
    ]
    run = AgentRunResult("paused", "", "m", messages, [], "sys")
    bounded = _candidate_conversation(run, max_chars=12000)
    assert bounded[-1]["content"] == "continue"
    tool_index = next(i for i,m in enumerate(bounded) if m.get("role") == "tool")
    assert tool_index > 0
    assert bounded[tool_index-1].get("role") == "assistant"
    assert bounded[tool_index-1].get("tool_calls")


def test_observational_approvals_block_mutations_but_allow_reads(tmp_path):
    from aicoder.team_orchestrator import _planning_approval, _research_approval
    for approval in (_planning_approval, _research_approval):
        assert approval("file_tree", {"path": str(tmp_path)}) is True
        assert approval("directory_create", {"path": str(tmp_path / "new")}) is False
        assert approval("file_write", {"path": str(tmp_path / "x.txt"), "content": "x"}) is False
        assert approval("binary_exec", {"program": "python3", "arguments": ["-m", "pytest", "--version"]}) is True
        assert approval("binary_exec", {"program": "python3", "arguments": ["-c", "import sys; print(sys.version)"]}) is True
        assert approval("binary_exec", {"program": "pip3", "arguments": ["list"]}) is True
        assert approval("shell", {"command": "python3 --version && python3 -m pytest --version 2>&1"}) is True
        assert approval("crawl", {"url": "https://docs.python.org/3/library/random.html"}) is True
        assert approval("crawl_url", {"url": "https://docs.python.org/3/library/argparse.html"}) is True
        assert approval("binary_exec", {"program": "python3", "arguments": ["-c", "open('x','w').write('y')"]}) is False


def test_observational_policy_denials_are_non_error_hints(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.executor import run_tool
    from aicoder.team_orchestrator import _research_approval
    with patch('aicoder.executor.get_state', return_value={'workspace_root': str(tmp_path)}):
        result, is_error = run_tool(
            MagicMock(), 'directory_create', {'path': str(tmp_path / 'blocked')},
            approval_fn=_research_approval, allowed_tools={'directory_create'},
        )
    assert is_error is False
    assert 'stage_policy_denied' in result
    assert not (tmp_path / 'blocked').exists()


def test_other_autonomous_policy_denials_remain_errors(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.executor import run_tool

    def deny(_name, _args):
        return False
    deny._aicoder_autonomous_policy = True

    with patch('aicoder.executor.get_state', return_value={'workspace_root': str(tmp_path)}):
        result, is_error = run_tool(
            MagicMock(), 'directory_create', {'path': str(tmp_path / 'blocked')},
            approval_fn=deny, allowed_tools={'directory_create'},
        )
    assert is_error is True
    assert 'blocked by autonomous policy' in result


def test_stage_provider_resume_preserves_active_contract_repair_prompt():
    from unittest.mock import MagicMock, patch
    from aicoder.agent_runtime import AgentRunResult
    from aicoder.team_orchestrator import _call_stage_agent_core
    calls = []

    class Runtime:
        def __init__(self, **kwargs): calls.append(kwargs)
        def run(self):
            if len(calls) == 1:
                return AgentRunResult('completed','SECTION_A:\nok','test/model',[],[],'system')
            if len(calls) == 2:
                return AgentRunResult('paused','provider fail','test/model',[],[],'system',error='provider fail',failure_category='transient')
            return AgentRunResult('completed','SECTION_A:\nok\nSECTION_B:\nok','test/model',[],[],'system')

    with patch('aicoder.team_orchestrator.NativeLightRuntime', Runtime), patch('aicoder.team_orchestrator._wait_before_resume', return_value=True):
        result = _call_stage_agent_core(client=MagicMock(), model_client=MagicMock(), model='test/model', system='sys', prompt='ORIGINAL-TASK', tools=[], workspace_root='.', event_fn=None, role='coordinator:test', stop_requested=None, approval_fn=None, required_sections=('SECTION_A','SECTION_B'), max_iterations=4)
    assert result.status == 'completed'
    assert 'CONTRACT REPAIR' in calls[2]['initial_prompt']
    assert 'missing section: SECTION_B' in calls[2]['initial_prompt']
    assert 'MANDATORY FINAL FORMAT' in calls[2]['initial_prompt']
    assert '## SECTION_A' in calls[2]['initial_prompt']
    assert '## SECTION_B' in calls[2]['initial_prompt']
    assert 'AUTHORITATIVE ORIGINAL STAGE TASK' in calls[2]['initial_prompt']


def test_research_provider_resume_preserves_active_contract_repair_prompt(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.agent_runtime import AgentRunResult
    from aicoder.team_orchestrator import _run_researcher_core
    calls=[]

    class Runtime:
        def __init__(self, **kwargs): calls.append(kwargs)
        def run(self):
            if len(calls)==1:
                return AgentRunResult('completed','FINDINGS:\nok','test/model',[],[],'system')
            if len(calls)==2:
                return AgentRunResult('paused','provider fail','test/model',[],[],'system',error='provider fail',failure_category='transient')
            return AgentRunResult('completed','FINDINGS:\na\nSOURCES:\nb\nAPPLICABILITY:\nc\nRISKS:\nd\nRECOMMENDATIONS:\ne','test/model',[],[],'system')

    with patch('aicoder.team_orchestrator.NativeLightRuntime', Runtime), patch('aicoder.team_orchestrator._wait_before_resume', return_value=True):
        result=_run_researcher_core(client=MagicMock(),model_client=MagicMock(),model='test/model',role='primary_sources',source_workspace=str(tmp_path),tools=[],stop_requested=None,task='task',research_plan='plan')
    assert result.status=='completed'
    assert 'RESEARCH CONTRACT REPAIR' in calls[2]['initial_prompt']
    assert 'AUTHORITATIVE ORIGINAL RESEARCH ASSIGNMENT' in calls[2]['initial_prompt']


def test_planning_blocks_duplicate_subagent_fanout_but_research_policy_does_not():
    from aicoder.team_orchestrator import _planning_approval, _research_approval
    args = {"task": "duplicate R1 research", "role": "researcher"}
    assert _planning_approval("subagent_run", args) is False
    # Do not globally hide subagents from the dedicated research stage; this check is
    # specifically about duplicate planner fan-out.
    assert _research_approval("subagent_run", args) is True


def test_research_evidence_excludes_stage_policy_denials(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.agent_runtime import AgentRunResult
    from aicoder.team_orchestrator import _run_researcher_core
    calls=[]

    class Runtime:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.event_fn=kwargs.get('event_fn')
        def run(self):
            self.event_fn('tool_call', {'name':'file_edit','arguments':{'path':'x'}})
            self.event_fn('tool_result', {'name':'file_edit','result':'file_edit: stage_policy_denied — this observational stage is read-only','is_error':False})
            self.event_fn('tool_call', {'name':'file_tree','arguments':{'path':'.'}})
            self.event_fn('tool_result', {'name':'file_tree','result':'ok','is_error':False})
            return AgentRunResult('completed','FINDINGS:\na\nSOURCES:\nb\nAPPLICABILITY:\nc\nRISKS:\nd\nRECOMMENDATIONS:\ne','test/model',[],[],'system')

    with patch('aicoder.team_orchestrator.NativeLightRuntime', Runtime):
        result=_run_researcher_core(client=MagicMock(),model_client=MagicMock(),model='test/model',role='best_practices',source_workspace=str(tmp_path),tools=[],stop_requested=None,task='task',research_plan='plan')
    assert result.status=='completed'
    assert result.evidence['successful_tools']==['file_tree']
    assert result.evidence['external_tools']==[]


def test_observational_missing_file_read_is_non_error_hint(tmp_path):
    from unittest.mock import MagicMock
    from aicoder.executor import run_tool
    from aicoder.team_orchestrator import _research_approval
    result, is_error = run_tool(
        MagicMock(), "file_read", {"path": "definitely-missing.py"},
        approval_fn=_research_approval, workspace_root=tmp_path,
    )
    assert is_error is False
    assert "observational_not_found" in result
    assert "definitely-missing.py" in result


def test_normal_missing_file_read_remains_error(tmp_path):
    from unittest.mock import MagicMock
    from aicoder.executor import run_tool
    result, is_error = run_tool(
        MagicMock(), "file_read", {"path": "definitely-missing.py"},
        approval_fn=None, workspace_root=tmp_path,
    )
    assert is_error is True
    assert "path does not exist" in result


def test_observational_missing_code_tree_is_non_error_hint(tmp_path):
    from unittest.mock import MagicMock
    from aicoder.executor import run_tool
    from aicoder.team_orchestrator import _research_approval
    result, is_error = run_tool(
        MagicMock(), "code_tree", {"path": "definitely-missing-package"},
        approval_fn=_research_approval, workspace_root=tmp_path,
    )
    assert is_error is False
    assert "observational_not_found" in result
    assert "definitely-missing-package" in result


def test_normal_missing_code_tree_remains_error(tmp_path):
    from unittest.mock import MagicMock
    from aicoder.executor import run_tool
    result, is_error = run_tool(
        MagicMock(), "code_tree", {"path": "definitely-missing-package"},
        approval_fn=None, workspace_root=tmp_path,
    )
    assert is_error is True
    assert "path does not exist" in result


def test_observational_missing_file_tree_is_non_error_hint(tmp_path):
    from aicoder.executor import run_tool

    def approval(name, args):
        return True
    approval._aicoder_autonomous_policy = True
    approval._aicoder_policy_denial_is_error = False

    result, is_error = run_tool(
        None, "file_tree", {"path": "definitely-missing"},
        workspace_root=str(tmp_path), approval_fn=approval,
        allowed_tools={"file_tree"},
    )
    assert is_error is False
    assert "observational_not_found" in result


def test_deterministic_contract_fallback_preserves_existing_sections_without_inventing():
    from aicoder.team_orchestrator import _contract_issues, _deterministic_contract_fallback

    required = ("OBJECTIVE", "RISKS", "NEXT STAGE INSTRUCTIONS")
    normalized = _deterministic_contract_fallback("## OBJECTIVE\nBuild the requested package.", required)
    assert _contract_issues(normalized, required) == []
    assert "Build the requested package." in normalized
    assert "treat this item as unresolved" in normalized
    assert "do not infer missing facts" in normalized


def test_task_handoff_preserves_detailed_user_contract():
    from aicoder.team_orchestrator import _task_handoff
    task = "A" * 12000 + " ACCEPTANCE_SENTINEL"
    handoff = _task_handoff(task)
    assert "ACCEPTANCE_SENTINEL" in handoff.raw
    assert "ACCEPTANCE_SENTINEL" in handoff.compact


def test_team_prompts_pin_original_task_and_bounded_coordinator_budget():
    from pathlib import Path
    source = Path("aicoder/team_orchestrator.py").read_text(encoding="utf-8")
    runtime = Path("aicoder/team_runtime.py").read_text(encoding="utf-8")
    assert "AUTHORITATIVE ORIGINAL USER TASK" in source
    assert "max_tokens=2200, max_iterations=30" in source
    assert "SOURCE RELEVANCE RULE" in runtime
    assert "Generic homepages" in runtime


def test_self_contained_research_task_blocks_external_web_tools():
    from aicoder.team_orchestrator import _research_approval_for_task, _task_requires_external_research
    task = "Build a deterministic Python standard-library terminal game in this empty workspace."
    assert _task_requires_external_research(task) is False
    approval = _research_approval_for_task(task)
    # Not required does not mean forbidden: researchers may still use read-only web research.
    assert approval("search", {"query": "python game examples"}) is True
    assert approval("crawl", {"url": "https://example.com"}) is True
    assert approval("file_tree", {"path": "."}) is True


def test_fresh_external_task_keeps_web_research_capability():
    from aicoder.team_orchestrator import _research_approval_for_task, _task_requires_external_research
    task = "Check the latest API compatibility and official documentation for provider version changes."
    assert _task_requires_external_research(task) is True
    approval = _research_approval_for_task(task)
    assert approval("search", {"query": "official API compatibility"}) is True


def test_research_prompt_carries_immutable_original_task(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.agent_runtime import AgentRunResult
    from aicoder.team_orchestrator import _run_researcher_core
    calls = []
    sentinel = "ORIGINAL-USER-ACCEPTANCE-SENTINEL-7319"

    class Runtime:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def run(self):
            return AgentRunResult(
                "completed",
                "FINDINGS:\na\nSOURCES:\nNo external source required; task/repository evidence only.\nAPPLICABILITY:\nc\nRISKS:\nd\nRECOMMENDATIONS:\ne",
                "test/model", [], [], "system"
            )

    with patch("aicoder.team_orchestrator.NativeLightRuntime", Runtime):
        result = _run_researcher_core(
            client=MagicMock(), model_client=MagicMock(), model="test/model",
            role="primary_sources", source_workspace=str(tmp_path), tools=[],
            stop_requested=None, task="Build local game " + sentinel, research_plan="plan",
        )
    assert result.status == "completed"
    assert sentinel in calls[0]["initial_prompt"]
    assert "EXTERNAL RESEARCH OPTIONAL" in calls[0]["initial_prompt"]


def test_research_constraint_guard_blocks_positive_recommendation_of_forbidden_tool():
    from aicoder.team_orchestrator import _research_constraint_issues, _sanitize_research_constraints
    task = "Text-only UI; do not require curses or any third-party package."
    report = (
        "FINDINGS:\na\nSOURCES:\nlocal evidence\nAPPLICABILITY:\nc\nRISKS:\nd\n"
        "RECOMMENDATIONS:\n1. Consider using Python's curses library.\n2. Use a simple input loop."
    )
    issues = _research_constraint_issues(report, task)
    assert any("curses" in issue for issue in issues)
    cleaned = _sanitize_research_constraints(report, task)
    assert "curses" not in cleaned.lower()
    assert "simple input loop" in cleaned.lower()


def test_research_constraint_guard_allows_negative_reference_to_forbidden_tool():
    from aicoder.team_orchestrator import _research_constraint_issues
    task = "Do not require curses."
    report = (
        "FINDINGS:\na\nSOURCES:\nlocal evidence\nAPPLICABILITY:\nc\nRISKS:\nd\n"
        "RECOMMENDATIONS:\n1. Avoid curses and use plain input/output."
    )
    assert _research_constraint_issues(report, task) == []


def test_empty_bootstrap_workspace_rejects_fake_created_files_claim(tmp_path):
    from aicoder.team_orchestrator import _observational_state_issues
    text = "DONE: The task has been completed. The following files have been created in the workspace."
    issues = _observational_state_issues(text, role="coordinator:plan_research", workspace_root=str(tmp_path))
    assert issues
    assert "observational state contradiction" in issues[0]


def test_non_bootstrap_role_does_not_use_bootstrap_state_guard(tmp_path):
    from aicoder.team_orchestrator import _observational_state_issues
    text = "Files have been created in the workspace."
    assert _observational_state_issues(text, role="coordinator:merge", workspace_root=str(tmp_path)) == []


def test_self_contained_research_policy_blocks_safe_remote_search_in_executor(tmp_path):
    from unittest.mock import MagicMock
    from aicoder.executor import run_tool
    from aicoder.team_orchestrator import _research_approval_for_task

    client = MagicMock()
    client.mcp_call.return_value = {"result": {"content": [{"type": "text", "text": "research ok"}]}}
    approval = _research_approval_for_task(
        "Build a deterministic Python standard-library terminal game."
    )
    result, is_error = run_tool(
        client, "search", {"query": "python game examples"},
        approval_fn=approval, allowed_tools={"search"}, workspace_root=tmp_path,
    )
    assert is_error is False
    assert result == "research ok"
    client.mcp_call.assert_called_once()

    tree_result, tree_error = run_tool(
        client, "file_tree", {"path": "."},
        approval_fn=approval, allowed_tools={"file_tree"}, workspace_root=tmp_path,
    )
    assert tree_error is False
    assert "empty directory" in tree_result


def test_self_contained_research_rejects_unverified_external_url_claim():
    from aicoder.team_orchestrator import _research_grounding_issues
    report = (
        "FINDINGS:\na\n"
        "SOURCES:\nPython docs https://docs.python.org/3/\n"
        "APPLICABILITY:\nc\nRISKS:\nd\nRECOMMENDATIONS:\ne"
    )
    issues = _research_grounding_issues(
        report, external_allowed=False,
        evidence_events=[{
            "kind": "tool_result", "name": "web_fetch_local",
            "result": "stage_policy_denied", "is_error": False,
        }],
    )
    assert issues
    assert "source grounding violation" in issues[0]


def test_self_contained_research_sanitizer_replaces_external_sources():
    from aicoder.team_orchestrator import _sanitize_self_contained_research
    report = (
        "FINDINGS:\nUse local requirements.\n"
        "SOURCES:\nPython docs https://docs.python.org/3/\n"
        "APPLICABILITY:\nLocal task.\nRISKS:\nNone.\nRECOMMENDATIONS:\nProceed locally."
    )
    cleaned = _sanitize_self_contained_research(report)
    assert "https://" not in cleaned
    assert "external research was disabled" in cleaned
    assert "Use local requirements" in cleaned


def test_deterministic_research_fallback_is_valid_contract():
    from aicoder.team_orchestrator import _deterministic_research_fallback, _contract_issues
    from aicoder.team_handoff import RESEARCH_SECTIONS
    text = _deterministic_research_fallback(external_allowed=False)
    assert _contract_issues(text, RESEARCH_SECTIONS) == []
    assert "do not infer missing facts" in text


def test_greenfield_self_contained_research_uses_deterministic_evidence(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.team_handoff import make_handoff
    from aicoder.team_orchestrator import _run_researcher

    (tmp_path / ".aicoder-team").mkdir()
    (tmp_path / ".aicoder-team" / "stageoff.json").write_text("{}")
    stage = make_handoff("stageoff", '{"user_task":"build local game"}', max_chars=120000)
    with patch("aicoder.team_orchestrator._run_researcher_core") as core:
        result = _run_researcher(
            client=MagicMock(), model_client=MagicMock(), model="test/model",
            role="primary_sources", source_workspace=str(tmp_path), tools=[],
            stop_requested=None, stage_input=stage,
            task="Build a deterministic Python standard-library terminal game.",
        )
    assert result.status == "completed"
    assert result.evidence["deterministic_greenfield"] is True
    assert "Original user task and local workspace inspection only" in result.response
    assert "do not claim" in result.response
    core.assert_not_called()


def test_greenfield_external_research_task_still_uses_model_path(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.team_handoff import make_handoff
    from aicoder.team_orchestrator import AgentStageResult, _run_researcher

    stage = make_handoff("stageoff", '{"user_task":"check API"}', max_chars=120000)
    fake = AgentStageResult("research:primary_sources", "test/model", "completed", "ok", 1)
    with patch("aicoder.team_orchestrator._run_researcher_core", return_value=fake) as core:
        result = _run_researcher(
            client=MagicMock(), model_client=MagicMock(), model="test/model",
            role="primary_sources", source_workspace=str(tmp_path), tools=[],
            stop_requested=None, stage_input=stage,
            task="Check the latest API compatibility and official documentation.",
        )
    assert result.status == "completed"
    core.assert_called_once()


def test_existing_project_files_disable_greenfield_shortcut(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.team_handoff import make_handoff
    from aicoder.team_orchestrator import AgentStageResult, _run_researcher

    (tmp_path / "app.py").write_text("print('x')")
    stage = make_handoff("stageoff", '{"user_task":"review local code"}', max_chars=120000)
    fake = AgentStageResult("research:best_practices", "test/model", "completed", "ok", 1)
    with patch("aicoder.team_orchestrator._run_researcher_core", return_value=fake) as core:
        result = _run_researcher(
            client=MagicMock(), model_client=MagicMock(), model="test/model",
            role="best_practices", source_workspace=str(tmp_path), tools=[],
            stop_requested=None, stage_input=stage,
            task="Review and improve this local Python project.",
        )
    assert result.status == "completed"
    core.assert_called_once()


def test_deterministic_greenfield_research_stageoff_review_is_valid_contract():
    from aicoder.team_orchestrator import (
        _contract_issues,
        _deterministic_greenfield_research_stageoff_review,
        _STAGEOFF_COORDINATOR_SECTIONS,
    )
    text = _deterministic_greenfield_research_stageoff_review()
    assert _contract_issues(text, _STAGEOFF_COORDINATOR_SECTIONS) == []
    assert "No implementation item is marked complete" in text
    assert "unrelated setup work" in text


def test_deterministic_greenfield_research_stageoff_skips_model_coordinator(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.team_orchestrator import _coordinate_stageoff
    from aicoder.team_pipeline import TeamStage

    current = {
        "schema": "aicoder-stageoff-v1",
        "user_task": "Build Brumo's Dungeon",
        "stages": [],
        "handoff_id": "root",
    }
    payload = {
        "reports": [
            {"role": "research:primary_sources", "evidence": {"deterministic_greenfield": True}},
            {"role": "research:best_practices", "evidence": {"deterministic_greenfield": True}},
        ]
    }
    events = []
    with patch("aicoder.team_orchestrator._call_stage_agent") as model_call:
        updated, coordinator, handoff = _coordinate_stageoff(
            current=current, stage=TeamStage.RESEARCH, stage_payload=payload,
            client=MagicMock(), model_client=MagicMock(), coordinator_model="test/model",
            tools=[], workspace_root=str(tmp_path),
            event_fn=lambda kind, payload: events.append((kind, payload)),
            stop_requested=None,
        )
    model_call.assert_not_called()
    assert coordinator is not None
    assert coordinator.status == "completed"
    assert coordinator.model == "deterministic"
    assert coordinator.evidence["model_skipped"] is True
    assert "Research completed deterministically" in updated["working_memory"]["stage_summary"]
    assert "No implementation item is runtime-confirmed complete" in updated["working_memory"]["completed_items"]
    assert handoff.source_stage == "research"
    assert any(
        kind == "team_worker_event"
        and payload.get("category") == "research"
        and payload.get("status") == "deterministic"
        for kind, payload in events
    )


def test_mixed_research_payload_keeps_model_coordinator_path(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.team_orchestrator import AgentStageResult, _coordinate_stageoff
    from aicoder.team_pipeline import TeamStage

    payload = {
        "reports": [
            {"role": "research:primary_sources", "evidence": {"deterministic_greenfield": True}},
            {"role": "research:best_practices", "evidence": {"deterministic_greenfield": False}},
        ]
    }
    response = (
        "## STAGE SUMMARY\nok\n\n## NEW FACTS\nfacts\n\n"
        "## REQUIRED CHANGES\nchanges\n\n## COMPLETED ITEMS\nnone\n\n"
        "## OPEN ITEMS\nopen\n\n## RISKS\nrisks\n\n"
        "## NEXT STAGE INSTRUCTIONS\ncontinue"
    )
    fake = AgentStageResult("coordinator:research", "test/model", "completed", response, 1)
    with patch("aicoder.team_orchestrator._call_stage_agent", return_value=fake) as model_call:
        _, coordinator, _ = _coordinate_stageoff(
            current={"stages": [], "handoff_id": ""},
            stage=TeamStage.RESEARCH, stage_payload=payload,
            client=MagicMock(), model_client=MagicMock(), coordinator_model="test/model",
            tools=[], workspace_root=str(tmp_path), event_fn=None, stop_requested=None,
        )
    model_call.assert_called_once()
    assert coordinator.model == "test/model"


def test_task_aware_research_policy_denies_empty_config_probe():
    from aicoder.team_orchestrator import _research_approval_for_task
    approval = _research_approval_for_task("Build a standard library only terminal game")
    assert approval("config", {}) is False
    assert approval("config", {"key": "runtime"}) is True
    assert approval("search", {"query": "pygame"}) is True


def test_deterministic_greenfield_bootstrap_preserves_verbatim_task():
    from aicoder.team_orchestrator import (
        _BOOTSTRAP_SECTIONS,
        _contract_issues,
        _deterministic_greenfield_bootstrap_plan,
    )
    sentinel = "MUST-PRESERVE-ACCEPTANCE-SENTINEL-8842"
    task = "Build the local game.\n- " + sentinel + "\n- Do not require curses."
    text = _deterministic_greenfield_bootstrap_plan(task)
    assert _contract_issues(text, _BOOTSTRAP_SECTIONS) == []
    assert task in text
    assert sentinel in text
    assert "No implementation" in text
    assert "NEXT STAGE INSTRUCTIONS" in text


def test_greenfield_bootstrap_gate_is_conservative(tmp_path):
    from aicoder.team_orchestrator import (
        _task_requires_external_research,
        _workspace_has_meaningful_project_files,
    )
    local_task = "Build a deterministic Python standard-library terminal game."
    external_task = "Check the latest API compatibility and official documentation."
    assert _task_requires_external_research(local_task) is False
    assert _workspace_has_meaningful_project_files(tmp_path) is False
    assert _task_requires_external_research(external_task) is True
    (tmp_path / "app.py").write_text("print('x')")
    assert _workspace_has_meaningful_project_files(tmp_path) is True


def test_same_model_keeps_distinct_brainstorm_perspectives():
    from aicoder.team_orchestrator import _brainstorm_participants
    from aicoder.team_runtime import ResearchSlot, TeamConfig

    model = "mistral/codestral-latest"
    config = TeamConfig(
        mode="on",
        research=(
            ResearchSlot(1, "primary_sources", model),
            ResearchSlot(2, "best_practices", model),
            ResearchSlot(3, "security_reliability", model),
            ResearchSlot(4, "alternative_architectures", model),
        ),
        coders=(),
        planner_model=model,
        coordinator_model=model,
        merge_model=model,
        test_planner_model=model,
    )
    participants = _brainstorm_participants(config, limit=6)
    labels = [label for label, _, _ in participants]
    assert labels[:4] == [
        "research:primary_sources",
        "research:best_practices",
        "research:security_reliability",
        "research:alternative_architectures",
    ]
    assert len(participants) == 6
    assert all(participant_model == model for _, participant_model, _ in participants)
    assert len({perspective for _, _, perspective in participants}) == 6


def test_brainstorm_participant_limit_still_applies_with_same_model():
    from aicoder.team_orchestrator import _brainstorm_participants
    from aicoder.team_runtime import ResearchSlot, TeamConfig

    model = "same/model"
    config = TeamConfig(
        mode="on",
        research=(
            ResearchSlot(1, "primary_sources", model),
            ResearchSlot(2, "best_practices", model),
            ResearchSlot(3, "security_reliability", model),
            ResearchSlot(4, "alternative_architectures", model),
        ),
        coders=(), planner_model=model, coordinator_model=model,
        merge_model=model, test_planner_model=model,
    )
    assert len(_brainstorm_participants(config, limit=3)) == 3


def test_explicit_task_triforce_prohibition_is_propagated_to_stage_policies(tmp_path):
    from unittest.mock import MagicMock
    from aicoder.executor import run_tool
    from aicoder.team_orchestrator import _research_approval_for_task

    approval = _research_approval_for_task(
        "Use local AICoder tools only. Do not target or inspect the TriForce backend, services, or processes."
    )
    assert getattr(approval, "_aicoder_forbid_triforce_backend", False) is True

    client = MagicMock()
    result, is_error = run_tool(
        client, "status", {}, approval_fn=approval,
        allowed_tools={"status"}, workspace_root=tmp_path,
    )
    assert is_error is False
    assert "TriForce backend targeting is disabled" in result
    client.mcp_call.assert_not_called()

    tree, tree_error = run_tool(
        client, "file_tree", {"path": "."}, approval_fn=approval,
        allowed_tools={"file_tree"}, workspace_root=tmp_path,
    )
    assert tree_error is False
    assert "empty directory" in tree
    client.mcp_call.assert_not_called()


def test_explicit_task_triforce_prohibition_blocks_external_triforce_profile(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.executor import run_tool
    from aicoder.team_orchestrator import _research_approval_for_task

    approval = _research_approval_for_task(
        "Never inspect the TriForce backend; operate only on the local workspace."
    )
    with patch("aicoder.mcp_service.call_external_tool") as external_call:
        result, is_error = run_tool(
            MagicMock(), "mcp.triforce_remote.status", {}, approval_fn=approval,
            allowed_tools={"mcp.triforce_remote.status"}, workspace_root=tmp_path,
        )
    assert is_error is False
    assert "TriForce backend MCP access is disabled" in result
    external_call.assert_not_called()


def test_task_backend_policy_does_not_disable_triforce_without_explicit_prohibition():
    from aicoder.team_orchestrator import _research_approval_for_task

    approval = _research_approval_for_task(
        "Check the latest TriForce API status and official provider compatibility."
    )
    assert getattr(approval, "_aicoder_forbid_triforce_backend", False) is False


def test_observational_stage_runtime_allows_complete_contract_without_tool_use(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.team_orchestrator import _call_stage_agent_core

    model_client = MagicMock()
    model_client.chat.return_value = {
        "response": (
            "## DIRECTIONS\n- keep it small\n\n"
            "## IDEAS\n- one package\n\n"
            "## TRADEOFFS\n- simplicity over abstraction\n\n"
            "## RISKS\n- scope creep\n\n"
            "## OPEN QUESTIONS\n- none\n\n"
            "## RECOMMENDATIONS\n- proceed"
        ),
        "model": "mistral/codestral-latest",
        "finish_reason": "stop",
        "tool_calls": [],
    }
    tool = {"name": "file_tree", "description": "tree", "inputSchema": {"type": "object"}}
    result = _call_stage_agent_core(
        client=MagicMock(), model_client=model_client, model="mistral/codestral-latest",
        system="Brainstorm only.", prompt="Build something useful.", tools=[tool],
        workspace_root=str(tmp_path), event_fn=None, role="brainstorm_state:r1",
        stop_requested=None, approval_fn=lambda _n, _a: True,
        required_sections=("DIRECTIONS", "IDEAS", "TRADEOFFS", "RISKS", "OPEN QUESTIONS", "RECOMMENDATIONS"),
        max_tokens=1000, max_iterations=10,
    )
    assert result.status == "completed"
    assert result.evidence["iterations"] == 1
    assert model_client.chat.call_count == 1


def test_brainstorm_operator_and_synthesis_are_tool_free():
    from pathlib import Path
    source = Path('aicoder/team_orchestrator.py').read_text()
    operator_anchor = 'system=BRAINSTORM_OPERATOR_SYSTEM_PROMPT,\n            tools=[], workspace_root=source_workspace,'
    synthesis_anchor = 'system=BRAINSTORM_SYNTHESIS_SYSTEM_PROMPT,\n            tools=[], workspace_root=source_workspace,'
    retry_anchor = 'system=BRAINSTORM_SYNTHESIS_SYSTEM_PROMPT, tools=[],'
    assert operator_anchor in source
    assert synthesis_anchor in source
    assert retry_anchor in source


def test_ensemble_merge_falls_back_when_dedicated_merge_model_is_disabled():
    from pathlib import Path
    source = Path('aicoder/team_orchestrator.py').read_text()
    assert 'config.merge_model\n            or config.coordinator_model\n            or config.planner_model' in source
    assert 'or winner.run.model\n            or winner.model' in source
    assert 'without LLM merge' not in source


def test_merge_prompts_make_winner_a_base_not_exclusive_source():
    from aicoder.team_runtime import MERGE_PLANNER_SYSTEM_PROMPT, MERGE_SYSTEM_PROMPT
    assert 'stable base, not an exclusive source' in MERGE_PLANNER_SYSTEM_PROMPT
    assert 'stable base, not a winner-takes-all result' in MERGE_SYSTEM_PROMPT
    assert 'every other verified candidate' in MERGE_SYSTEM_PROMPT



def test_candidate_verification_stall_pause_is_terminal_but_provider_pause_is_resumable():
    from aicoder.agent_runtime import AgentRunResult
    from aicoder.team_orchestrator import _candidate_pause_is_resumable

    stalled = AgentRunResult(
        status="paused",
        response=(
            "Agent paused because authoritative verification reproduced the same "
            "non-transient failure at least five times despite intervening mutations. "
            "The edits are not changing the failing behavior; resume only with a different "
            "root-cause strategy. Failure signature: code:example"
        ),
        model="mistral/codestral-latest",
        messages=[], tools=[], system_prompt="",
    )
    assert _candidate_pause_is_resumable(stalled, None) is False

    provider_pause = AgentRunResult(
        status="paused",
        response="Transient model/backend failure after request retries were exhausted: ReadTimeout",
        model="mistral/codestral-latest",
        messages=[], tools=[], system_prompt="",
        failure_category="transient",
    )
    assert _candidate_pause_is_resumable(provider_pause, None) is True


def test_task_contract_blocks_web_tools_before_transport_but_keeps_local_tools(tmp_path):
    from unittest.mock import MagicMock
    from aicoder.executor import run_tool
    from aicoder.team_orchestrator import _research_approval_for_task

    (tmp_path / "README.md").write_text("local evidence\n", encoding="utf-8")
    approval = _research_approval_for_task(
        "Analyze only local evidence. Researchers must not browse the web or internet. Do not inspect the TriForce backend."
    )
    client = MagicMock()

    search_result, search_error = run_tool(
        client, "search", {"query": "AICoder", "mode": "all"}, approval_fn=approval,
        allowed_tools={"search"}, workspace_root=tmp_path,
    )
    assert search_error is False
    assert "task_contract_denied" in search_result

    fetch_result, fetch_error = run_tool(
        client, "web_fetch_local", {"url": "https://example.com"}, approval_fn=approval,
        allowed_tools={"web_fetch_local"}, workspace_root=tmp_path,
    )
    assert fetch_error is False
    assert "task_contract_denied" in fetch_result

    local_result, local_error = run_tool(
        client, "file_read", {"path": "README.md"}, approval_fn=approval,
        allowed_tools={"file_read"}, workspace_root=tmp_path,
    )
    assert local_error is False
    assert "local evidence" in local_result
    client.mcp_call.assert_not_called()


def test_research_stage_may_use_web_even_when_implementation_web_is_forbidden(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.executor import run_tool
    from aicoder.team_orchestrator import _research_approval_for_task

    approval = _research_approval_for_task(
        "Do not browse the web during implementation. Researchers should investigate the topic thoroughly."
    )
    with patch("aicoder.executor.run_mcp_tool", return_value=("research result", False)) as remote:
        result, is_error = run_tool(
            MagicMock(), "search", {"query": "topic"}, approval_fn=approval,
            allowed_tools={"search"}, workspace_root=tmp_path,
        )
    assert is_error is False
    assert result == "research result"
    remote.assert_called_once()


def test_explicit_research_no_web_blocks_research_web_tool(tmp_path):
    from unittest.mock import MagicMock, patch
    from aicoder.executor import run_tool
    from aicoder.team_orchestrator import _research_approval_for_task

    approval = _research_approval_for_task("Researchers must not browse the web; use local evidence only.")
    with patch("aicoder.executor.run_mcp_tool") as remote:
        result, is_error = run_tool(
            MagicMock(), "search", {"query": "topic"}, approval_fn=approval,
            allowed_tools={"search"}, workspace_root=tmp_path,
        )
    assert is_error is False
    assert "task_contract_denied" in result or "stage_policy_denied" in result
    remote.assert_not_called()


class TeamRunLockTests(unittest.TestCase):
    def test_team_run_lock_rejects_same_task_workspace_pair(self):
        from aicoder.team_orchestrator import _team_run_lock
        with tempfile.TemporaryDirectory() as tmp:
            with _team_run_lock(tmp, "Build   X") as first:
                self.assertTrue(first)
                with _team_run_lock(tmp, "build x") as second:
                    self.assertFalse(second)

    def test_team_run_lock_distinguishes_different_tasks(self):
        from aicoder.team_orchestrator import _team_run_lock
        with tempfile.TemporaryDirectory() as tmp:
            with _team_run_lock(tmp, "build x") as first:
                self.assertTrue(first)
                with _team_run_lock(tmp, "build y") as second:
                    self.assertTrue(second)


def test_candidate_policy_enforces_all_tools_and_blocks_web():
    from aicoder.team_orchestrator import _candidate_approval

    assert getattr(_candidate_approval, "_aicoder_enforce_all_tools", False) is True
    assert _candidate_approval("search", {"query": "unrelated web search"}) is False
    assert _candidate_approval("web_fetch_local", {"url": "https://example.com"}) is False
    assert _candidate_approval("file_read", {"path": "README.md"}) is True


def test_brainstorm_policy_is_post_research_and_blocks_web():
    from aicoder.team_orchestrator import _brainstorm_approval_for_task

    approval = _brainstorm_approval_for_task("Build a dependency-free local CLI. Research may browse if useful.")
    assert getattr(approval, "_aicoder_enforce_all_tools", False) is True
    assert getattr(approval, "_aicoder_allow_research_web", True) is False
    assert approval("search", {"query": "FastAPI PostgreSQL"}) is False
    assert approval("web_fetch_local", {"url": "https://docs.python.org/3/"}) is False
    assert approval("file_read", {"path": "README.md"}) is True

def test_adaptive_unit_contract_preserves_safety_but_scopes_requirements():
    from aicoder.team_orchestrator import CodingWorkUnit, _work_unit_task_contract
    parent = compile_task_contract("Requirements:\n- build CLI\n- build parser\n\nNever use web.\n\nAcceptance checks:\n- python /tmp/full.py")
    unit = CodingWorkUnit("parser", "Parser", "Implement parser only", acceptance=("python -m unittest tests.test_parser",))
    contract = _work_unit_task_contract(unit, parent)
    assert contract.requirements == ("Implement parser only",)
    assert contract.forbid_web is True
    assert contract.acceptance_commands == ("python -m unittest tests.test_parser",)
    assert "python /tmp/full.py" not in contract.acceptance_commands


def test_adaptive_actual_conflicts_detects_runtime_overlap():
    from aicoder.team_orchestrator import CandidateResult, _adaptive_lane_actual_conflicts
    from unittest.mock import MagicMock
    a = CandidateResult(1, "m", "s", MagicMock(), AgentRunResult("completed", "DONE", "m", [], [], ""), work_unit_id="a")
    b = CandidateResult(2, "m", "s", MagicMock(), AgentRunResult("completed", "DONE", "m", [], [], ""), work_unit_id="b")
    a.evaluation = {"delta": {"modified_files": ["pkg/shared.py"], "added_files": [], "deleted_files": []}}
    b.evaluation = {"delta": {"modified_files": ["pkg/shared.py"], "added_files": [], "deleted_files": []}}
    assert _adaptive_lane_actual_conflicts([a, b]) == {"pkg/shared.py": ["a", "b"]}

def test_apply_candidate_delta_integrates_disjoint_lane_files():
    from aicoder.team_orchestrator import CandidateResult, _apply_candidate_delta
    with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
        source = Path(source_dir)
        (source / "base.txt").write_text("base\n", encoding="utf-8")
        lane = RamWorkspace(source, ram_root=ram_dir); lane.prepare()
        (lane.info.execution_root / "pkg").mkdir()
        (lane.info.execution_root / "pkg" / "lane.py").write_text("value = 1\n", encoding="utf-8")
        run = AgentRunResult("completed", "DONE", "m", [], [], "")
        candidate = CandidateResult(1, "m", "s", lane, run, work_unit_id="lane")
        candidate.evaluation = {"delta": lane.delta_summary()}
        integration = RamWorkspace(source, ram_root=ram_dir); integration.prepare()
        _apply_candidate_delta(integration, candidate)
        assert (integration.info.execution_root / "pkg" / "lane.py").read_text() == "value = 1\n"
        lane.abort(); integration.abort()


def test_final_repair_runtime_is_fresh_and_failure_focused():
    from aicoder.team_orchestrator import _run_final_repair
    captured = {}
    class Runtime:
        def __init__(self, **kwargs): captured.update(kwargs)
        def run(self): return AgentRunResult("completed", "DONE: fixed", "m", [], [], "")
    with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as ram_dir:
        source = Path(source_dir); (source / "app.py").write_text("x=1\n", encoding="utf-8")
        integration = RamWorkspace(source, ram_root=ram_dir); integration.prepare()
        contract = compile_task_contract("Requirements:\n- fix app")
        verification = [{"name":"python-tests","ok":False,"required":True,"output":"AssertionError: expected 2"}]
        with patch("aicoder.team_orchestrator.NativeLightRuntime", Runtime):
            result = _run_final_repair(
                client=MagicMock(), model_client=MagicMock(), model="m", workspace=integration,
                task="fix app", contract=contract, verification=verification, tools=[],
                source_workspace=str(source), stop_requested=None, request_timeout=30,
                event_fn=None, native_openrouter_tool_calling=False,
            )
        assert result.status == "completed"
        assert captured["conversation"] == []
        assert "FRESH FINAL INTEGRATION REPAIR" in captured["initial_prompt"]
        assert "AssertionError: expected 2" in captured["initial_prompt"]
        assert captured["protected_workspace_root"] == str(source)
        integration.abort()
