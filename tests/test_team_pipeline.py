from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicoder.team_pipeline import (
    STAGE_ORDER, StageLedger, TeamStage, blind_candidate_id, objective_rank_key,
    project_verification_plan,
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
    def test_candidate_id_depends_on_content_not_model_or_slot(self):
        self.assertEqual(blind_candidate_id("same diff"), blind_candidate_id("same diff"))
        self.assertNotEqual(blind_candidate_id("diff a"), blind_candidate_id("diff b"))

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


if __name__ == "__main__":
    unittest.main()
