from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_plan import PlanStore
from aicoder.agent_runtime import NativeLightRuntime
from aicoder.executor import LOCAL_FILE_EDIT_SCHEMA


class ResumeCapabilitiesTests(unittest.TestCase):
    """Tests for resume capabilities using original plan.task context.
    
    Ensures that during resume operations, the original plan.task is used for
    capability resolution and decision making.
    """

    def test_resume_uses_original_plan_task_for_capability_resolution(self):
        """During resume, the original plan.task should be available for capability resolution."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            # Create a plan with a specific task
            original_task = "Implement a REST API endpoint for user management with authentication"
            plan = store.create(original_task, str(workspace), "test/model")
            plan.status = "paused"
            plan.pause_reason = "needs implementation details"
            store.save(plan)
            
            client = MagicMock()
            client.timeout = 30
            client.chat.return_value = {
                "response": "DONE: implemented",
                "model": "test/model"
            }
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[LOCAL_FILE_EDIT_SCHEMA],
                load_tools_on_start=True,
                approval_fn=lambda _name, _args: True,
                plan_store=store,
                resume=True,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            # Should complete successfully
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.plan_id, plan.id)
            
            # Verify the original task was used in the conversation
            messages = result.messages
            user_messages = [m for m in messages if m.get("role") == "user"]
            
            # Check that resume context includes the original task
            resume_contexts = [
                str(msg.get("content", "")) for msg in user_messages
                if "Resume persistent plan" in str(msg.get("content", ""))
            ]
            
            self.assertTrue(len(resume_contexts) > 0, "Resume context should be present")
            self.assertIn(original_task, resume_contexts[0], 
                         "Original task should be included in resume context")

    def test_resume_with_complex_original_task(self):
        """Test resume with a complex multi-part original task."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            # Create a complex task
            complex_task = (
                "Analyze the codebase for security vulnerabilities, fix critical issues, "
                "and add comprehensive test coverage for the authentication module. "
                "Ensure all changes are properly documented and verified."
            )
            plan = store.create(complex_task, str(workspace), "test/model")
            plan.status = "paused"
            plan.pause_reason = "security review required"
            store.save(plan)
            
            client = MagicMock()
            client.timeout = 30
            client.chat.return_value = {
                "response": "DONE: completed security analysis",
                "model": "test/model"
            }
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue with security fixes",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[LOCAL_FILE_EDIT_SCHEMA],
                load_tools_on_start=True,
                approval_fn=lambda _name, _args: True,
                plan_store=store,
                resume=True,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            self.assertEqual(result.status, "completed")
            
            # Verify complex task is in the context
            messages = result.messages
            user_messages = [m for m in messages if m.get("role") == "user"]
            resume_contexts = [
                str(msg.get("content", "")) for msg in user_messages
                if "Resume persistent plan" in str(msg.get("content", ""))
            ]
            
            self.assertTrue(len(resume_contexts) > 0)
            # The original task should be present in the resume context
            self.assertIn("security vulnerabilities", resume_contexts[0].lower())

    def test_resume_preserves_plan_id_and_iteration(self):
        """Test that resume preserves plan ID and updates iteration count."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            # Create a plan with existing iteration
            plan = store.create("Test iteration preservation", str(workspace), "test/model")
            plan.status = "paused"
            plan.iteration = 5
            plan.pause_reason = "iteration test"
            store.save(plan)
            
            client = MagicMock()
            client.timeout = 30
            client.chat.return_value = {
                "response": "DONE: completed",
                "model": "test/model"
            }
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                resume=True,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.plan_id, plan.id)
            
            # Verify iteration was updated
            saved_plan = store.load_current(str(workspace))
            self.assertEqual(saved_plan.iteration, 6)  # Should be incremented
            self.assertEqual(saved_plan.resume_count, 1)

    def test_resume_with_original_task_in_run_start_event(self):
        """Test that run_start event includes plan.task information."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            original_task = "Build a complete web application with database integration"
            plan = store.create(original_task, str(workspace), "test/model")
            plan.status = "paused"
            store.save(plan)
            
            client = MagicMock()
            client.timeout = 30
            client.chat.return_value = {
                "response": "DONE: built",
                "model": "test/model"
            }
            
            # Track events
            events = []
            def track_event(kind, payload):
                events.append((kind, payload))
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                resume=True,
                event_fn=track_event,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            # Check that run_start event includes plan_task
            run_start_events = [e for e in events if e[0] == "run_start"]
            self.assertTrue(len(run_start_events) > 0, "run_start event should be emitted")
            
            payload = run_start_events[0][1]
            self.assertIn("plan_task", payload)
            self.assertEqual(payload["plan_task"], original_task)

    def test_resume_task_context_available_for_model_decision_making(self):
        """Test that the original task context is available when the model makes decisions."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            store = PlanStore(root / "plans")
            
            task = "Implement user authentication system"
            plan = store.create(task, str(workspace), "test/model")
            plan.status = "paused"
            store.save(plan)
            
            client = MagicMock()
            client.timeout = 30
            
            # Track what messages are sent to the model
            sent_messages = []
            def track_chat(**kwargs):
                sent_messages.append(kwargs.get("messages", []))
                return {"response": "DONE: implemented", "model": "test/model"}
            
            client.chat.side_effect = track_chat
            
            runtime = NativeLightRuntime(
                client=client,
                initial_prompt="continue",
                model="test/model",
                fallback_model=None,
                workspace_root=str(workspace),
                tools=[],
                load_tools_on_start=False,
                plan_store=store,
                resume=True,
                base_timeout=30,
            )
            
            result = runtime.run()
            
            self.assertEqual(result.status, "completed")
            
            # Verify messages were sent to the model
            self.assertTrue(len(sent_messages) > 0)
            messages = sent_messages[0]
            
            # Check that system message includes plan context with original task
            system_messages = [m for m in messages if m.get("role") == "system"]
            self.assertTrue(len(system_messages) > 0)
            
            system_content = system_messages[0].get("content", "")
            self.assertIn("Original task:", system_content)
            self.assertIn(task, system_content)


if __name__ == "__main__":
    unittest.main()
