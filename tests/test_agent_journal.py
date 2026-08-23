from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_journal import (
    MAX_JOURNAL_MESSAGES,
    MAX_TOTAL_MESSAGE_CHARS,
    ContinuationJournalStore,
)
from aicoder.agent_plan import PlanStore
from aicoder.agent_runtime import NativeLightRuntime
from aicoder.executor import LOCAL_FILE_READ_SCHEMA


class AgentJournalTests(unittest.TestCase):
    def test_process_restart_resume_restores_sanitized_context_and_clears_on_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            plans = PlanStore(root / "plans")
            journals = ContinuationJournalStore(root / "journals")

            first_client = MagicMock()
            first_client.timeout = 30
            first_client.chat.return_value = {
                "response": "",
                "model": "test/model",
                "tool_calls": [{
                    "id": "call-abc-123",
                    "type": "function",
                    "function": {
                        "name": "file_read",
                        "arguments": json.dumps({"path": "README.md"}),
                    },
                }],
            }
            first = NativeLightRuntime(
                client=first_client,
                initial_prompt="Inspect README and continue the migration",
                model="openrouter/test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[LOCAL_FILE_READ_SCHEMA],
                load_tools_on_start=True,
                plan_store=plans,
                journal_store=journals,
                base_timeout=30,
                native_openrouter_tool_calling=True,
            )
            with (
                patch("aicoder.agent_runtime.MAX_ITERATIONS", 1),
                patch("aicoder.agent_runtime.run_tool", return_value=("RAW README CONTENT", False)),
            ):
                first_result = first.run()

            self.assertEqual(first_result.status, "paused")
            journal = journals.load(str(workspace), first_result.plan_id)
            self.assertIsNotNone(journal)
            self.assertEqual(journal.tool_batches[0]["calls"][0]["id"], "call-abc-123")
            self.assertEqual(journal.tool_batches[0]["calls"][0]["name"], "file_read")
            journal_text = journals._path(str(workspace), first_result.plan_id).read_text(encoding="utf-8")
            self.assertNotIn("RAW README CONTENT", journal_text)

            second_client = MagicMock()
            second_client.timeout = 30
            second_client.chat.return_value = {"response": "DONE: migration context restored", "model": "test/model"}
            second = NativeLightRuntime(
                client=second_client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                quick_chat=True,
                plan_store=plans,
                journal_store=journals,
                resume=True,
                resume_plan_id=first_result.plan_id,
                base_timeout=30,
            )
            second_result = second.run()

            self.assertEqual(second_result.status, "completed")
            request_messages = second_client.chat.call_args.kwargs["messages"]
            contents = "\n".join(str(item.get("content", "")) for item in request_messages)
            self.assertIn("Inspect README and continue the migration", contents)
            self.assertIn("Persistent continuation checkpoint", contents)
            self.assertIn("file_read(ok)", contents)
            self.assertNotIn("RAW README CONTENT", contents)
            self.assertIsNone(journals.load(str(workspace), first_result.plan_id))

    def test_journal_redacts_secrets_and_drops_raw_tool_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = ContinuationJournalStore(root / "journals")
            path = store.save_checkpoint(
                plan_id="plan-secret",
                workspace=str(workspace),
                messages=[
                    {"role": "system", "content": "Authorization: Bearer SYSTEMSECRET"},
                    {"role": "user", "content": "api_key=SUPERSECRET please inspect"},
                    {"role": "assistant", "content": "I will inspect without exposing password=hunter2"},
                    {"role": "user", "content": "UNTRUSTED_TOOL_OUTPUT_BEGIN_x\nTOKEN_FROM_FILE\nUNTRUSTED_TOOL_OUTPUT_END_x"},
                ],
                pending_input="UNTRUSTED_TOOL_OUTPUT_BEGIN_y\nANOTHER_TOOL_SECRET",
                tool_batches=[{
                    "iteration": 1,
                    "calls": [{
                        "id": "call-1",
                        "name": "file_read",
                        "provider": "openai",
                        "arguments": {
                            "path": "x.txt",
                            "Authorization": "Bearer ARGSECRET",
                            "api_key": "ARGKEY",
                        },
                        "is_error": False,
                    }],
                }],
            )
            raw = path.read_text(encoding="utf-8")
            for secret in ("SUPERSECRET", "hunter2", "TOKEN_FROM_FILE", "ANOTHER_TOOL_SECRET", "ARGSECRET", "ARGKEY", "SYSTEMSECRET"):
                self.assertNotIn(secret, raw)
            self.assertIn("[REDACTED]", raw)
            loaded = store.load(str(workspace), "plan-secret")
            self.assertEqual(loaded.tool_batches[0]["calls"][0]["id"], "call-1")
            self.assertEqual(loaded.pending_input, "")

    def test_corrupted_journal_is_ignored_during_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            plans = PlanStore(root / "plans")
            journals = ContinuationJournalStore(root / "journals")
            plan = plans.create("Finish parser", str(workspace), "test/model")
            plan.status = "paused"
            plan.pause_reason = "process restart"
            plans.save(plan)
            path = journals._path(str(workspace), plan.id)
            path.write_text("{not valid json", encoding="utf-8")

            client = MagicMock()
            client.timeout = 30
            client.chat.return_value = {"response": "DONE: recovered without journal", "model": "test/model"}
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                quick_chat=True,
                plan_store=plans,
                journal_store=journals,
                resume=True,
                resume_plan_id=plan.id,
                base_timeout=30,
            )
            result = runtime.run()
            self.assertEqual(result.status, "completed")
            self.assertFalse(path.exists())

    def test_journal_message_bounds_are_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = ContinuationJournalStore(root / "journals")
            messages = [
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}-" + ("x" * 5000)}
                for i in range(60)
            ]
            store.save_checkpoint(plan_id="bounded", workspace=str(workspace), messages=messages)
            loaded = store.load(str(workspace), "bounded")
            self.assertLessEqual(len(loaded.messages), MAX_JOURNAL_MESSAGES)
            self.assertLessEqual(sum(len(item["content"]) for item in loaded.messages), MAX_TOTAL_MESSAGE_CHARS)


if __name__ == "__main__":
    unittest.main()
