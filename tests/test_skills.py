from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_plan import PlanStore
from aicoder.agent_runtime import NativeLightRuntime
from aicoder.executor import (
    LOCAL_FILE_EDIT_SCHEMA,
    LOCAL_FILE_READ_SCHEMA,
    LOCAL_SKILL_READ_SCHEMA,
    LOCAL_TEST_SCHEMA,
    build_system_prompt,
    run_tool,
)
from aicoder.skills import MAX_SKILL_BYTES, discover_skills, read_skill, render_skill_catalog


def _write_skill(root: Path, name: str, description: str, body: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


class SkillDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._saved_active_workspace = os.environ.pop("AICODER_ACTIVE_WORKSPACE", None)

    def tearDown(self):
        if self._saved_active_workspace is not None:
            os.environ["AICODER_ACTIVE_WORKSPACE"] = self._saved_active_workspace
        else:
            os.environ.pop("AICODER_ACTIVE_WORKSPACE", None)

    def test_workspace_native_skill_overrides_agents_and_global(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            config = root / "config"
            workspace.mkdir()
            _write_skill(config / "skills", "review", "global", "GLOBAL BODY")
            _write_skill(workspace / ".agents" / "skills", "review", "agents", "AGENTS BODY")
            _write_skill(workspace / ".aicoder" / "skills", "review", "native", "NATIVE BODY")

            skills = discover_skills(workspace, config_dir=config)
            self.assertEqual(len(skills), 1)
            self.assertEqual(skills[0].scope, "workspace-aicoder")
            self.assertEqual(skills[0].description, "native")
            text, is_error = read_skill(workspace, "review", config_dir=config)
            self.assertFalse(is_error)
            self.assertIn("NATIVE BODY", text)
            self.assertNotIn("GLOBAL BODY", text)

    def test_catalog_contains_metadata_but_not_skill_body(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            _write_skill(
                workspace / ".aicoder" / "skills",
                "release-check",
                "Verify a release candidate",
                "RAW_INTERNAL_WORKFLOW_BODY",
            )
            catalog = render_skill_catalog(workspace)
            self.assertIn("release-check", catalog)
            self.assertIn("Verify a release candidate", catalog)
            self.assertNotIn("RAW_INTERNAL_WORKFLOW_BODY", catalog)

    def test_declared_name_must_match_directory_name(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            skill = workspace / ".aicoder" / "skills" / "safe" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: other\ndescription: mismatch\n---\nbody\n",
                encoding="utf-8",
            )
            self.assertEqual(discover_skills(workspace), [])

    def test_symlink_escape_is_not_discovered(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            _write_skill(outside, "escape", "outside", "DO NOT LOAD")
            skills_root = workspace / ".aicoder" / "skills"
            skills_root.mkdir(parents=True)
            try:
                (skills_root / "escape").symlink_to(outside / "escape", target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            self.assertEqual(discover_skills(workspace), [])

    def test_oversized_skill_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            path = workspace / ".aicoder" / "skills" / "huge" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("x" * (MAX_SKILL_BYTES + 1), encoding="utf-8")
            self.assertEqual(discover_skills(workspace), [])

    def test_skill_read_accepts_only_catalog_names_not_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            _write_skill(workspace / ".aicoder" / "skills", "safe", "safe", "SAFE BODY")
            text, is_error = read_skill(workspace, "../../safe")
            self.assertTrue(is_error)
            self.assertIn("invalid skill name", text)

    def test_system_prompt_exposes_catalog_and_skill_read_tool_only(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            _write_skill(
                workspace / ".aicoder" / "skills", "debug", "Debug systematically", "PRIVATE BODY"
            )
            prompt = build_system_prompt([LOCAL_SKILL_READ_SCHEMA], str(workspace))
            self.assertIn("## Available Skills", prompt)
            self.assertIn("debug [workspace-aicoder]: Debug systematically", prompt)
            self.assertIn("skill_read(name*)", prompt)
            self.assertNotIn("PRIVATE BODY", prompt)

    def test_executor_reads_skill_without_approval_or_arbitrary_path(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            _write_skill(workspace / ".aicoder" / "skills", "debug", "Debug", "READ ME")
            client = MagicMock()
            with patch("aicoder.executor.get_state", return_value={"workspace_root": str(workspace)}):
                result, is_error = run_tool(
                    client,
                    "skill_read",
                    {"name": "debug"},
                    allowed_tools={"skill_read"},
                )
            self.assertFalse(is_error)
            self.assertIn("READ ME", result)
            client._request.assert_not_called()


class SkillRuntimeTests(unittest.TestCase):
    def test_skill_read_does_not_satisfy_fresh_workspace_inspection_after_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            plan = store.create("Continue a prior edit", str(workspace), "test/model")
            plan.status = "paused"
            plan.set_step("inspect", "completed", "prior inspection")
            plan.set_step("implement", "completed", "prior edit")
            plan.set_step("verify", "in_progress", "pending")
            store.save(plan)

            client = MagicMock()
            client.timeout = 30
            client.chat.side_effect = [
                {
                    "response": '<tool_call>{"name":"skill_read","arguments":{"name":"debug"}}</tool_call>',
                    "model": "test/model",
                },
                {
                    "response": '<tool_call>{"name":"file_edit","arguments":{"path":"x.txt","operation":"write","content":"again"}}</tool_call>',
                    "model": "test/model",
                },
                {
                    "response": '<tool_call>{"name":"file_read","arguments":{"path":"x.txt"}}</tool_call>',
                    "model": "test/model",
                },
                {
                    "response": '<tool_call>{"name":"test","arguments":{"command":"python3 -m unittest"}}</tool_call>',
                    "model": "test/model",
                },
                {"response": "DONE: verified", "model": "test/model"},
            ]
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[LOCAL_SKILL_READ_SCHEMA, LOCAL_FILE_EDIT_SCHEMA, LOCAL_FILE_READ_SCHEMA, LOCAL_TEST_SCHEMA],
                load_tools_on_start=True,
                approval_fn=lambda _name, _args: True,
                plan_store=store,
                resume=True,
                base_timeout=30,
            )
            with patch("aicoder.agent_runtime.run_tool", return_value=("ok", False)) as run:
                result = runtime.run()

            self.assertEqual(result.status, "completed")
            executed_names = [call.args[1] for call in run.call_args_list]
            self.assertEqual(executed_names, ["skill_read", "file_read", "test"])
            self.assertTrue(any(
                "require a fresh successful read/check" in str(message.get("content", ""))
                for message in result.messages
            ))


if __name__ == "__main__":
    unittest.main()
