from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import NativeLightRuntime
from aicoder.capabilities import META_TOOL_NAMES


CATALOG = [
    {"name": "shell", "description": "shell", "capabilities": ["local_code_write"], "inputSchema": {"type": "object"}},
    {"name": "file_read", "description": "read", "capabilities": ["local_code_read"], "inputSchema": {"type": "object"}},
    {"name": "search", "description": "web search", "capabilities": ["web", "research"], "inputSchema": {"type": "object"}},
    {"name": "crawl", "description": "web crawl", "capabilities": ["web", "research"], "inputSchema": {"type": "object"}},
    {"name": "docker_list", "description": "containers", "capabilities": ["containers"], "inputSchema": {"type": "object"}},
]


class DummyClient:
    base_url = "http://example.invalid"
    token = "x"
    timeout = 30


class CapabilityRuntimeTests(unittest.TestCase):
    def runtime(self, prompt="https://example.com/docs", **kwargs):
        return NativeLightRuntime(
            client=DummyClient(), initial_prompt=prompt, model=None, fallback_model=None,
            workspace_root=".", persistent_plan=False, **kwargs,
        )

    def test_prepare_tools_uses_progressive_working_set(self):
        runtime=self.runtime(tool_budget=6)
        with patch("aicoder.agent_runtime.load_tools", return_value=list(CATALOG)):
            tools=runtime._prepare_tools()
        names={tool["name"] for tool in tools}
        self.assertTrue(set(META_TOOL_NAMES).issubset(names))
        self.assertIn("search",names)
        self.assertIn("crawl",names)
        self.assertNotIn("shell",names)
        self.assertEqual(len(runtime._tool_catalog),len(CATALOG))

    def test_always_mode_keeps_full_catalog(self):
        runtime=self.runtime(progressive_tool_disclosure=False)
        with patch("aicoder.agent_runtime.load_tools", return_value=list(CATALOG)):
            tools=runtime._prepare_tools()
        self.assertEqual([t["name"] for t in tools],[t["name"] for t in CATALOG])

    def test_capability_request_expands_from_inactive_catalog(self):
        runtime=self.runtime(tool_budget=5)
        with patch("aicoder.agent_runtime.load_tools", return_value=list(CATALOG)):
            tools=runtime._prepare_tools()
        result,is_error,changed=runtime._run_meta_tool(
            "capability_request", {"capabilities":["containers"],"reason":"need docker state"}, tools
        )
        self.assertFalse(is_error)
        self.assertTrue(changed)
        self.assertIn("docker_list",{t["name"] for t in tools})
        self.assertIn("docker_list",json.loads(result)["added"])

    def test_disabled_tool_cannot_be_reactivated(self):
        runtime=self.runtime(tool_budget=5, enabled_tool_names=["search","crawl"])
        with patch("aicoder.agent_runtime.load_tools", return_value=list(CATALOG)):
            tools=runtime._prepare_tools()
        result,is_error,changed=runtime._run_meta_tool(
            "capability_request", {"tools":["shell"]}, tools
        )
        self.assertTrue(is_error)
        self.assertFalse(changed)
        self.assertNotIn("shell",{t["name"] for t in tools})
        self.assertIn("no enabled inactive tools",result)

    def test_expansion_round_limit_is_enforced(self):
        runtime=self.runtime(tool_budget=4, max_expansion_rounds=1)
        with patch("aicoder.agent_runtime.load_tools", return_value=list(CATALOG)):
            tools=runtime._prepare_tools()
        _,err1,changed1=runtime._run_meta_tool("capability_request",{"tools":["docker_list"]},tools)
        self.assertFalse(err1); self.assertTrue(changed1)
        result,err2,changed2=runtime._run_meta_tool("capability_request",{"tools":["shell"]},tools)
        self.assertTrue(err2); self.assertFalse(changed2)
        self.assertIn("expansion limit",result)

    def test_agent_loop_sends_expanded_tools_on_next_model_turn(self):
        client=MagicMock()
        client.timeout=30
        client.base_url="http://example.invalid"
        client.token="x"
        client.chat.side_effect=[
            {"response": '<tool_call>{"name":"capability_request","arguments":{"tools":["docker_list"],"reason":"inspect containers"}}</tool_call>', "model":"test/model"},
            {"response": "DONE: capability expanded", "model":"test/model"},
        ]
        runtime=NativeLightRuntime(
            client=client, initial_prompt="Check https://example.com/docs",
            model="test/model", fallback_model=None, workspace_root=".",
            persistent_plan=False, tool_budget=5, max_iterations=3,
        )
        with patch("aicoder.agent_runtime.load_tools", return_value=list(CATALOG)), \
             patch("aicoder.agent_runtime.supports_tools", return_value=True):
            result=runtime.run()
        self.assertEqual(result.status,"completed")
        self.assertEqual(client.chat.call_count,2)
        self.assertIsNone(client.chat.call_args_list[0].kwargs["tools"])
        self.assertIsNone(client.chat.call_args_list[1].kwargs["tools"])
        self.assertIn("docker_list", result.system_prompt)
        self.assertIn("capability_request", result.system_prompt)


    def test_resume_capabilities_use_original_plan_task(self):
        import tempfile
        from pathlib import Path
        from aicoder.agent_plan import PlanStore

        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            store = PlanStore(Path(temp) / "plans")
            plan = store.create(
                "Debug failing tests in src/foo.py and verify the fix",
                str(workspace),
                "test/model",
            )
            plan.status = "paused"
            store.save(plan)
            runtime = NativeLightRuntime(
                client=DummyClient(), initial_prompt="continue", model="test/model",
                fallback_model=None, workspace_root=str(workspace), plan_store=store,
                resume=True, resume_plan_id=plan.id, tool_budget=8,
            )
            catalog = list(CATALOG) + [
                {"name": "test", "description": "run tests", "capabilities": ["testing", "debug"], "inputSchema": {"type": "object"}},
            ]
            with patch("aicoder.agent_runtime.load_tools", return_value=catalog):
                tools = runtime._prepare_tools()
            names = {tool["name"] for tool in tools}
            self.assertIn("file_read", names)
            self.assertIn("test", names)
            self.assertIn("shell", names)



if __name__ == "__main__":
    unittest.main()
