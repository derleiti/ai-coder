from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from aicoder import cli
from aicoder.team_runtime import (
    config_from_state,
    state_with_team_overrides,
    team_model_rows,
    team_role_key,
)


class TeamRoleMappingTests(unittest.TestCase):
    def test_role_aliases_are_shared_and_stable(self):
        self.assertEqual(team_role_key("r1"), "team_research_model_1")
        self.assertEqual(team_role_key("coder4"), "team_coder_model_4")
        self.assertEqual(team_role_key("tests"), "team_test_planner_model")
        self.assertEqual(team_role_key("primary"), "selected_model")
        with self.assertRaises(ValueError):
            team_role_key("unknown-role")

    def test_per_run_primary_resolves_all_primary_slots_without_mutating_input(self):
        original = {
            "selected_model": "provider/saved",
            "team_runtime_mode": "on",
            "team_research_model_1": "@primary",
            "team_research_model_2": "provider/research",
            "team_research_model_3": "",
            "team_research_model_4": "",
            "team_coder_model_1": "@primary",
            "team_coder_model_2": "provider/coder",
            "team_coder_model_3": "",
            "team_coder_model_4": "",
            "team_planner_model": "@primary",
            "team_coordinator_model": "",
            "team_merge_model": "",
            "team_test_planner_model": "",
        }
        effective = state_with_team_overrides(
            original,
            {"team_coder_model_2": "provider/override"},
            primary_model="provider/run-primary",
        )
        self.assertEqual(original["selected_model"], "provider/saved")
        self.assertEqual(effective["selected_model"], "provider/run-primary")
        config = config_from_state(effective)
        self.assertEqual(config.research[0].model, "provider/run-primary")
        self.assertEqual(config.planner_model, "provider/run-primary")
        self.assertEqual(config.coders[0].model, "provider/run-primary")
        self.assertEqual(config.coders[1].model, "provider/override")

    def test_team_rows_show_configured_and_resolved_models(self):
        rows = team_model_rows({
            "selected_model": "provider/base",
            "team_research_model_1": "@primary",
            "team_research_model_2": "provider/other",
        })
        by_alias = {row["alias"]: row for row in rows}
        self.assertEqual(by_alias["r1"]["configured"], "@primary")
        self.assertEqual(by_alias["r1"]["resolved"], "provider/base")
        self.assertEqual(by_alias["r2"]["resolved"], "provider/other")


class PeterEasterEggTests(unittest.TestCase):
    def test_peter_is_not_advertised_in_normal_cli_help(self):
        help_text = cli.build_parser().format_help()
        self.assertNotIn("peter", help_text.lower())

    def test_peter_reports_active_codename_without_side_effects(self):
        output = io.StringIO()
        with patch("aicoder.cli._peter_run_state", return_value=(True, "run-42")), redirect_stdout(output):
            rc = cli.cmd_peter(argparse.Namespace())
        self.assertEqual(rc, 0)
        text = output.getvalue()
        self.assertIn("Codename: PETER", text)
        self.assertIn("Peter arbeitet", text)
        self.assertIn("run-42", text)


class TeamCliTests(unittest.TestCase):
    def test_parser_exposes_team_configuration_and_per_run_overrides(self):
        parser = cli.build_parser()
        args = parser.parse_args([
            "agent", "--model", "provider/base", "--team-mode", "on",
            "--team-role", "r1=provider/research",
            "--team-role", "c1=@primary", "fix", "the", "repo",
        ])
        self.assertEqual(args.model, "provider/base")
        self.assertEqual(args.team_mode, "on")
        self.assertEqual(args.team_role, ["r1=provider/research", "c1=@primary"])
        self.assertEqual(args.prompt, ["fix", "the", "repo"])

        team_args = parser.parse_args([
            "team", "configure", "--mode", "on", "--r1", "provider/a",
            "--c1", "provider/b", "--merge", "off",
        ])
        self.assertEqual(team_args.team_action, "configure")
        self.assertEqual(team_args.r1, "provider/a")
        self.assertEqual(team_args.c1, "provider/b")
        self.assertEqual(team_args.merge, "off")

    def test_assignment_parser_accepts_mixed_provider_ids_and_off(self):
        parsed = cli._parse_team_role_assignments([
            "r1=ollama/gemma4:cloud",
            "c2=openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
            "merge=off",
        ])
        self.assertEqual(parsed["team_research_model_1"], "ollama/gemma4:cloud")
        self.assertEqual(
            parsed["team_coder_model_2"],
            "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        )
        self.assertEqual(parsed["team_merge_model"], "")

    def test_agent_command_passes_temporary_team_overrides_without_persisting(self):
        args = argparse.Namespace(
            setup=False,
            prompt=["fix", "repository", "bug"],
            resume=False,
            plan_id=None,
            json_out=False,
            json_events=False,
            model="provider/run-primary",
            verbose=False,
            team_mode="on",
            team_role=["r1=provider/research", "c1=@primary", "merge=off"],
        )
        saved_state = {"selected_model": "provider/saved"}
        with (
            patch("aicoder.session_state.get_state", return_value=saved_state),
            patch("aicoder.agent.run_agent", return_value=0) as run_agent,
        ):
            rc = cli.cmd_agent(args)
        self.assertEqual(rc, 0)
        kwargs = run_agent.call_args.kwargs
        self.assertEqual(kwargs["model"], "provider/run-primary")
        self.assertEqual(kwargs["team_overrides"]["team_runtime_mode"], "on")
        self.assertEqual(kwargs["team_overrides"]["team_research_model_1"], "provider/research")
        self.assertEqual(kwargs["team_overrides"]["team_coder_model_1"], "@primary")
        self.assertEqual(kwargs["team_overrides"]["team_merge_model"], "")
        self.assertEqual(saved_state, {"selected_model": "provider/saved"})


if __name__ == "__main__":
    unittest.main()
