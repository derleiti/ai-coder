from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import AgentRunResult
from aicoder.team_orchestrator import AgentStageResult, CandidateResult, run_team
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

            def advisor(_model_client, *, model, system, prompt, max_tokens=0):
                return AgentStageResult("advisor", model, "completed", "shared plan", 1)

            candidates = []
            def candidate(**kwargs):
                backend = RamWorkspace(source, ram_root=ram_dir)
                backend.prepare()
                slot = kwargs["slot"]
                (backend.info.execution_root / "app.py").write_text(f"value = {slot}\n", encoding="utf-8")
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

            original_create = __import__("aicoder.workspace_backend", fromlist=["create_workspace_backend"]).create_workspace_backend
            def create_backend(root, mode, **kwargs):
                return RamWorkspace(root, ram_root=ram_dir)

            with (
                patch("aicoder.team_orchestrator.load_tools", return_value=[]),
                patch("aicoder.team_orchestrator._run_researcher", side_effect=researcher),
                patch("aicoder.team_orchestrator._call_advisor", side_effect=advisor),
                patch("aicoder.team_orchestrator._run_candidate", side_effect=candidate),
                patch("aicoder.team_orchestrator.evaluate_candidate", side_effect=evaluate),
                patch("aicoder.team_orchestrator.create_workspace_backend", side_effect=create_backend),
                patch("aicoder.team_orchestrator.NativeLightRuntime", FakeIntegrationRuntime),
            ):
                result = run_team(
                    task="Implement feature", state=state, config=config, client=MagicMock(),
                    model_client=MagicMock(), source_workspace=str(source),
                )

            self.assertEqual(result.status, "completed", result.error)
            self.assertEqual(result.performance["winner_candidate_id"], "cand-2")
            self.assertEqual(result.performance["ledger"]["completed"], [
                "plan_research", "research", "plan_code", "code", "merge_plan", "merge",
                "plan_tests", "tests_function_ok", "atomic_disk_write",
            ])
            self.assertEqual((source / "app.py").read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual((source / "integrated.txt").read_text(encoding="utf-8"), "merged\n")
            self.assertFalse((source / ".aicoder-team").exists())


if __name__ == "__main__":
    unittest.main()
