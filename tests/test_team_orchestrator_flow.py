from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import AgentRunResult
from aicoder.team_orchestrator import AgentStageResult, CandidateResult, _redact_debug_value, evaluate_candidate, run_team
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

            def advisor(_model_client, *, model, system, prompt, max_tokens=0, **kwargs):
                return AgentStageResult("advisor", model, "completed", "shared plan", 1)

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
                patch("aicoder.team_orchestrator._call_advisor", side_effect=advisor),
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

            def advisor(_model_client, *, model, system, prompt, max_tokens=0, **kwargs):
                return AgentStageResult("advisor", model, "completed", "shared plan", 1)

            def candidate(**kwargs):
                backend = RamWorkspace(source, ram_root=ram_dir)
                backend.prepare()
                created.append(backend.info.execution_root)
                run = AgentRunResult("paused", "waiting", kwargs["model"], [], [], "system", error="paused")
                return CandidateResult(kwargs["slot"], kwargs["model"], kwargs["strategy"], backend, run)

            with (
                patch("aicoder.team_orchestrator.load_tools", return_value=[]),
                patch("aicoder.team_orchestrator._run_researcher", side_effect=researcher),
                patch("aicoder.team_orchestrator._call_advisor", side_effect=advisor),
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

            def advisor(_model_client, *, model, system, prompt, max_tokens=0, **kwargs):
                return AgentStageResult("advisor", model, "completed", "shared plan", 1)

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
                patch("aicoder.team_orchestrator._call_advisor", side_effect=advisor),
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

            def advisor(_model_client, *, model, system, prompt, max_tokens=0, **kwargs):
                return AgentStageResult("advisor", model, "completed", "shared plan", 1)

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
                patch("aicoder.team_orchestrator._call_advisor", side_effect=advisor),
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
