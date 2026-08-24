from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicoder.team_runtime import config_from_state, should_use_team
from aicoder.workspace_backend import RamWorkspace


class TeamConfigurationTests(unittest.TestCase):
    def test_duplicate_models_and_disabled_slots_are_supported(self):
        state = {
            "selected_model": "provider/main",
            "team_runtime_mode": "on",
            "team_research_model_1": "provider/main",
            "team_research_model_2": "provider/main",
            "team_research_model_3": "",
            "team_research_model_4": "off",
            "team_planner_model": "provider/main",
            "team_coordinator_model": "",
            "team_coder_model_1": "provider/main",
            "team_coder_model_2": "provider/main",
            "team_coder_model_3": "",
            "team_coder_model_4": "provider/other",
            "team_merge_model": "provider/main",
            "team_finalizer_model": "provider/main",
        }
        config = config_from_state(state)
        self.assertEqual(len(config.research), 2)
        self.assertEqual(len(config.coders), 3)
        self.assertEqual(config.coders[0].model, config.coders[1].model)
        self.assertIsNone(config.coordinator_model)
        self.assertEqual(config.validate(), [])

    def test_primary_alias_resolves_per_role(self):
        state = {
            "selected_model": "provider/base",
            "team_runtime_mode": "on",
            "team_planner_model": "@primary",
            "team_coder_model_1": "@primary",
        }
        config = config_from_state(state)
        self.assertEqual(config.planner_model, "provider/base")
        self.assertEqual(config.coders[0].model, "provider/base")

    def test_auto_team_only_triggers_for_substantive_coding_work(self):
        self.assertFalse(should_use_team("Hallo", "auto"))
        self.assertFalse(should_use_team("Erkläre mir Python Listen.", "auto"))
        prompt = "Implementiere eine robuste neue Architektur im Repository, füge Tests hinzu und verifiziere die Änderungen vollständig. " * 2
        self.assertTrue(should_use_team(prompt, "auto"))


class RamCandidateIsolationTests(unittest.TestCase):
    def test_two_candidates_can_diverge_from_same_source(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as ram:
            root = Path(src)
            (root / "app.py").write_text("value = 0\n", encoding="utf-8")
            first = RamWorkspace(root, ram_root=ram); first.prepare()
            second = RamWorkspace(root, ram_root=ram); second.prepare()
            (first.info.execution_root / "app.py").write_text("value = 1\n", encoding="utf-8")
            (second.info.execution_root / "app.py").write_text("value = 2\n", encoding="utf-8")
            self.assertEqual((root / "app.py").read_text(), "value = 0\n")
            self.assertNotEqual(
                (first.info.execution_root / "app.py").read_text(),
                (second.info.execution_root / "app.py").read_text(),
            )
            first.abort(); second.abort()

    def test_internal_team_artifacts_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as ram:
            root = Path(src)
            (root / "app.py").write_text("old\n", encoding="utf-8")
            backend = RamWorkspace(root, ram_root=ram); backend.prepare()
            backend.write_candidate_artifact(".aicoder-team/candidates.json", "{}")
            (backend.info.execution_root / "app.py").write_text("new\n", encoding="utf-8")
            backend.finalize(verified=True)
            self.assertEqual((root / "app.py").read_text(), "new\n")
            self.assertFalse((root / ".aicoder-team").exists())


if __name__ == "__main__":
    unittest.main()
