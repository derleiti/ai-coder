from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from aicoder.team_pipeline import (
    STAGE_ORDER, StageLedger, TeamStage, blind_candidate_id, content_fingerprint, objective_rank_key,
    configured_project_python, execute_verification_plan, merge_verification_plans, normalize_project_test_argv,
    project_verification_plan, task_acceptance_verification_plan,
)


class StageGateTests(unittest.TestCase):
    def test_exact_pipeline_order_is_enforced(self):
        ledger = StageLedger()
        for stage in STAGE_ORDER:
            ledger.start(stage)
            ledger.complete(stage)
        self.assertEqual(ledger.completed, [stage.value for stage in STAGE_ORDER])

    def test_brainstorm_is_between_research_and_plan_code(self):
        self.assertEqual(
            [stage.value for stage in STAGE_ORDER[:4]],
            ["plan_research", "research", "brainstorm", "plan_code"],
        )

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
        self.assertRegex(first, r"^cand-[0-9a-f]{12}$")
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


class ProjectPythonRuntimeTests(unittest.TestCase):
    def test_explicit_test_python_routes_pytest_and_unittest(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"AICODER_TEST_PYTHON": sys.executable}):
            root = Path(tmp)
            self.assertEqual(configured_project_python(root), str(Path(sys.executable).resolve()))
            self.assertEqual(
                normalize_project_test_argv(["pytest", "-q"], root),
                [str(Path(sys.executable).resolve()), "-m", "pytest", "-q"],
            )
            self.assertEqual(
                normalize_project_test_argv(["python3", "-m", "unittest", "discover"], root),
                [str(Path(sys.executable).resolve()), "-m", "unittest", "discover"],
            )

    def test_verification_plan_uses_explicit_test_python(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"AICODER_TEST_PYTHON": sys.executable}):
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[project]\nname="x"\nversion="0.1"\n[tool.pytest.ini_options]\n', encoding="utf-8")
            (root / "tests").mkdir()
            plan = project_verification_plan(root)
            python_commands = [item for item in plan if item.name.startswith("python-")]
            self.assertTrue(python_commands)
            self.assertTrue(all(item.argv[0] == str(Path(sys.executable).resolve()) for item in python_commands))


class ProjectPlanTests(unittest.TestCase):
    def test_behavior_change_requires_test_change_evidence(self):
        from aicoder.team_pipeline import test_change_evidence as change_evidence

        missing = change_evidence({"changed": ["aicoder/runtime.py"], "deleted": []})
        self.assertTrue(missing["behavior_change"])
        self.assertFalse(missing["coverage_evidence_ok"])
        covered = change_evidence({"changed": ["aicoder/runtime.py", "tests/test_runtime.py"], "deleted": []})
        self.assertTrue(covered["coverage_evidence_ok"])
        weakened = change_evidence({
            "changed": ["aicoder/runtime.py"],
            "deleted": ["tests/test_runtime.py"],
            "added_files": [],
        })
        self.assertTrue(weakened["tests_weakened"])
        self.assertFalse(weakened["coverage_evidence_ok"])

    def test_fresh_non_git_project_uses_content_gate_instead_of_git_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.txt").write_text("fresh project\n", encoding="utf-8")
            plan = project_verification_plan(root)
            self.assertEqual([item.name for item in plan], ["workspace-content"])
            self.assertNotIn("git", plan[0].argv[0].lower())

    def test_git_project_without_metadata_keeps_git_diff_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            plan = project_verification_plan(root)
            self.assertEqual([item.name for item in plan], ["git-diff-check"])

    def test_python_verification_ignores_stale_same_size_pyc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[project]\nname="x"\nversion="0.1"\n', encoding="utf-8")
            (root / "app.py").write_text("value = 0\n", encoding="utf-8")
            tests = root / "tests"
            tests.mkdir()
            test_file = tests / "test_app.py"
            passing = (
                "import unittest\nimport app\nclass T(unittest.TestCase):\n"
                "    def test_value(self): self.assertEqual(app.value, 0)\n"
            )
            failing = passing.replace("app.value, 0", "app.value, 1")
            self.assertEqual(len(passing), len(failing))
            test_file.write_text(passing, encoding="utf-8")
            original_stat = test_file.stat()
            plan = project_verification_plan(root)
            first = execute_verification_plan(root, plan)
            self.assertTrue(all(row.ok for row in first if row.required), first)

            test_file.write_text(failing, encoding="utf-8")
            os.utime(test_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            second = execute_verification_plan(root, plan)
            python_tests = next(row for row in second if row.name == "python-tests")
            self.assertFalse(python_tests.ok, python_tests.output)


    def test_task_acceptance_commands_are_extracted_without_shell(self):
        task = """Acceptance checks that must actually execute successfully before completion:
1. python -m pytest -q
2. python -m sample --seed 42 --demo
3. python -c "import sample"
4. README documents controls
Important runtime constraints:
- Do not browse the web.
"""
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"AICODER_TEST_PYTHON": sys.executable}):
            plan = task_acceptance_verification_plan(task, tmp)
            self.assertEqual(len(plan), 3)
            self.assertEqual(plan[0].argv[:3], (str(Path(sys.executable).resolve()), "-m", "pytest"))
            self.assertEqual(plan[1].argv[1:3], ("-m", "sample"))
            self.assertEqual(plan[2].argv[1:], ("-c", "import sample"))

    def test_task_acceptance_rejects_shell_operators_and_unrelated_commands(self):
        task = """ACCEPTANCE TESTS:
- python -m pytest -q && rm -rf /
- sudo apt update
- curl https://example.invalid
- cargo test --all-targets
"""
        with tempfile.TemporaryDirectory() as tmp:
            plan = task_acceptance_verification_plan(task, tmp)
            self.assertEqual([item.argv[0] for item in plan], ["cargo"])

    def test_acceptance_expected_nonzero_is_success(self):
        task = """Acceptance checks:
1. python probe.py
#1 EXPECTED NONZERO; this is success.
"""
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"AICODER_TEST_PYTHON": sys.executable}):
            Path(tmp, "probe.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
            plan = task_acceptance_verification_plan(task, tmp)
            self.assertEqual(len(plan), 1)
            self.assertTrue(plan[0].expected_nonzero)
            results = execute_verification_plan(tmp, plan)
            self.assertTrue(results[0].ok)
            self.assertEqual(results[0].exit_code, 2)

    def test_acceptance_exact_exit_code_is_enforced(self):
        task = """Acceptance checks:
1. python probe.py
Check 1 expected exit code 7.
"""
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"AICODER_TEST_PYTHON": sys.executable}):
            Path(tmp, "probe.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
            plan = task_acceptance_verification_plan(task, tmp)
            self.assertEqual(plan[0].expected_exit_codes, (7,))
            results = execute_verification_plan(tmp, plan)
            self.assertTrue(results[0].ok)

    def test_merge_verification_plans_deduplicates_same_command(self):
        from aicoder.team_pipeline import VerificationCommand
        one = VerificationCommand("repo", ("python3", "-m", "pytest", "-q"))
        two = VerificationCommand("task", ("python3", "-m", "pytest", "-q"))
        merged = merge_verification_plans([one], [two])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].name, "repo")

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
