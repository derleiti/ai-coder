from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicoder.team_pipeline import (
    STAGE_ORDER, StageLedger, TeamStage, blind_candidate_id, content_fingerprint, objective_rank_key,
    project_verification_plan, execute_verification_plan,
)


class StageGateTests(unittest.TestCase):
    def test_exact_pipeline_order_is_enforced(self):
        ledger = StageLedger()
        for stage in STAGE_ORDER:
            ledger.start(stage)
            ledger.complete(stage)
        self.assertEqual(ledger.completed, [stage.value for stage in STAGE_ORDER])

    def test_pipeline_cannot_skip_tests_to_disk_write(self):
        ledger = StageLedger()
        ledger.start(TeamStage.PLAN_RESEARCH); ledger.complete(TeamStage.PLAN_RESEARCH)
        with self.assertRaises(RuntimeError):
            ledger.start(TeamStage.ATOMIC_DISK_WRITE)


class BlindRankingTests(unittest.TestCase):
    def test_candidate_run_ids_are_random_and_content_fingerprint_is_separate(self):
        first = blind_candidate_id("same diff")
        second = blind_candidate_id("same diff")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("cand-"))
        self.assertEqual(content_fingerprint("same diff"), content_fingerprint("same diff"))
        self.assertNotEqual(content_fingerprint("diff a"), content_fingerprint("diff b"))

    def test_objective_ranking_ignores_model_identity(self):
        base = {
            "score": 90,
            "checks": {"tests": {"ok": True}, "build": {"ok": True}},
            "delta": {"changed_count": 2, "deleted_count": 0},
            "diff": "same",
        }
        with_model_a = dict(base, model="famous/model", slot=1)
        with_model_b = dict(base, model="unknown/model", slot=4)
        self.assertEqual(objective_rank_key(with_model_a), objective_rank_key(with_model_b))

    def test_fewer_failures_beat_higher_raw_score(self):
        safe = {"score": 60, "checks": {"tests": {"ok": True}}, "delta": {}, "diff": "a"}
        broken = {"score": 999, "checks": {"tests": {"ok": False}}, "delta": {}, "diff": "b"}
        self.assertGreater(objective_rank_key(safe), objective_rank_key(broken))


class ResearchEvidenceBiasTests(unittest.TestCase):
    def test_planner_research_prompt_does_not_expose_model_identity(self):
        from aicoder.team_orchestrator import AgentStageResult, _build_planner_prompt
        report = AgentStageResult(
            role="research:primary_sources", model="famous/provider-model", status="completed",
            response="finding", elapsed_ms=1,
            evidence={"externally_verified": True, "successful_tools": ["search"]},
        )
        prompt = _build_planner_prompt("task", "repo", [report])
        self.assertNotIn("famous/provider-model", prompt)
        self.assertIn("verified-tool-evidence", prompt)
        self.assertIn("tools=search", prompt)

    def test_unverified_research_is_explicitly_marked(self):
        from aicoder.team_orchestrator import AgentStageResult, _build_planner_prompt
        report = AgentStageResult(
            role="research:best_practices", model="model-a", status="completed",
            response="claim", elapsed_ms=1, evidence={},
        )
        prompt = _build_planner_prompt("task", "repo", [report])
        self.assertIn("unverified-or-local-only", prompt)


class ProjectPlanTests(unittest.TestCase):
    def test_python_project_gets_compile_and_test_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[project]\nname="x"\nversion="0.1"\n', encoding="utf-8")
            (root / "tests").mkdir()
            plan = project_verification_plan(root)
            names = [item.name for item in plan]
            self.assertIn("python-compile", names)
            self.assertIn("python-tests", names)

    def test_compile_gate_ignores_internal_candidate_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[project]\nname="x"\nversion="0.1"\n', encoding="utf-8")
            (root / "good.py").write_text('VALUE = 1\n', encoding="utf-8")
            rejected = root / ".aicoder-team" / "candidates" / "rejected"
            rejected.mkdir(parents=True)
            (rejected / "bad.py").write_text('def broken(:\n', encoding="utf-8")
            compile_command = next(item for item in project_verification_plan(root) if item.name == "python-compile")
            result = execute_verification_plan(root, [compile_command])[0]
            self.assertTrue(result.ok, result.output)


if __name__ == "__main__":
    unittest.main()
