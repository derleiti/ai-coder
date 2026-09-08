from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import AgentRunResult
from aicoder.team_orchestrator import (
    AgentStageResult, CandidateResult, _is_incomplete_envelope_reason, _redact_debug_value,
    _call_stage_agent_core, _run_candidate, _run_researcher, evaluate_candidate, run_team,
)
from aicoder.team_runtime import config_from_state
from aicoder.workspace_backend import RamWorkspace


def _result(text: str, model: str = "test/model") -> AgentRunResult:
    return AgentRunResult(
        status="completed", response=text, model=model, messages=[], tools=[], system_prompt="",
    )


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
            self.assertIn("AUTONOMOUS CANDIDATE VERIFICATION REPAIR 1/2", RepairRuntime.prompts[1])
            self.assertIn("regression-test-evidence", RepairRuntime.prompts[1])
            final = evaluate_candidate(candidate)
            self.assertTrue(final["verification_passed"], final)
            self.assertEqual(candidate.run.performance.get("team_verification_repairs"), 1)
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
            self.assertIn("AUTONOMOUS CANDIDATE VERIFICATION REPAIR 1/2", PausedRepairRuntime.prompts[1])
            self.assertIn("python-tests", PausedRepairRuntime.prompts[1])
            self.assertIn("regression-test-evidence", PausedRepairRuntime.prompts[1])
            final = evaluate_candidate(candidate)
            self.assertTrue(final["verification_passed"], final)
            self.assertEqual(candidate.run.performance.get("team_auto_resumes"), 0)
            self.assertEqual(candidate.run.performance.get("team_verification_repairs"), 1)
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

            def researcher(**kwargs):
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
