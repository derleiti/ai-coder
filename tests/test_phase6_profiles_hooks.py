from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aicoder.agent_runtime import NativeLightRuntime
from aicoder.hooks import HookBus, HookDecision
from aicoder.subagents import _profile_tools, get_subagent_profile


class SubagentProfileTests(unittest.TestCase):
    def test_explore_is_read_only_and_code_scoped(self):
        profile=get_subagent_profile('explore')
        self.assertIsNotNone(profile)
        tools=[
            {'name':'file_read','capabilities':['local_code_read']},
            {'name':'file_edit','capabilities':['local_code_write']},
            {'name':'git','capabilities':['git']},
            {'name':'os_system_overview','capabilities':['system_diagnostics'],'annotations':{'readOnlyHint':True}},
        ]
        names={tool['name'] for tool in _profile_tools(tools,profile)}
        self.assertEqual(names,{'file_read','git'})

    def test_system_diagnostician_gets_only_read_only_system_tools(self):
        profile=get_subagent_profile('system-diagnostician')
        tools=[
            {'name':'os_system_overview','capabilities':['system_diagnostics'],'annotations':{'readOnlyHint':True}},
            {'name':'os_network_ports','capabilities':['network'],'annotations':{'readOnlyHint':True}},
            {'name':'os_service_action','capabilities':['services'],'annotations':{'readOnlyHint':False}},
            {'name':'file_read','capabilities':['local_code_read']},
        ]
        names={tool['name'] for tool in _profile_tools(tools,profile)}
        self.assertEqual(names,{'os_system_overview','os_network_ports'})

    def test_legacy_debug_remains_tool_capable(self):
        profile=get_subagent_profile('debug')
        self.assertTrue(profile.tool_capable)
        tools=[
            {'name':'file_read','capabilities':['local_code_read']},
            {'name':'file_edit','capabilities':['local_code_write']},
        ]
        self.assertEqual({t['name'] for t in _profile_tools(tools,profile)},{'file_read','file_edit'})


class HookBusTests(unittest.TestCase):
    def test_pre_tool_exception_fails_closed(self):
        bus=HookBus()
        def boom(_payload): raise RuntimeError('broken hook')
        bus.register('PreToolUse',boom)
        decision=bus.emit('PreToolUse',{'name':'file_read'})
        self.assertTrue(decision.blocked)
        self.assertIn('failed closed',decision.reason)
        self.assertTrue(decision.diagnostics)

    def test_post_tool_exception_is_diagnostic_only(self):
        bus=HookBus()
        def boom(_payload): raise RuntimeError('broken hook')
        bus.register('PostToolUse',boom)
        decision=bus.emit('PostToolUse',{'name':'file_read'})
        self.assertFalse(decision.blocked)
        self.assertTrue(decision.diagnostics)

    def test_context_is_bounded(self):
        bus=HookBus()
        bus.register('SessionStart',lambda _p: {'context':'x'*10000})
        decision=bus.emit('SessionStart',{})
        self.assertLessEqual(sum(len(x) for x in decision.context),4000)


class HookRuntimeTests(unittest.TestCase):
    def test_pre_tool_hook_blocks_before_executor(self):
        with tempfile.TemporaryDirectory() as temp:
            client=MagicMock(); client.timeout=30
            client.chat.side_effect=[
                {'response':'<tool_call>{"name":"file_read","arguments":{"path":"x.txt"}}</tool_call>','model':'test/model'},
                {'response':'DONE: hook policy observed','model':'test/model'},
            ]
            bus=HookBus()
            bus.register('PreToolUse',lambda payload: HookDecision(blocked=payload.get('name')=='file_read',reason='test policy'))
            runtime=NativeLightRuntime(
                client=client,initial_prompt='Inspect x.txt',model='test/model',fallback_model=None,
                workspace_root=temp,tools=[{'name':'file_read','inputSchema':{'type':'object','properties':{'path':{'type':'string'}}}}],
                persistent_plan=False,hooks=bus,base_timeout=30,max_iterations=3,
            )
            with patch('aicoder.agent_runtime.supports_tools',return_value=True), patch('aicoder.agent_runtime.run_tool') as run:
                result=runtime.run()
            run.assert_not_called()
            self.assertEqual(result.status,'completed')
            self.assertTrue(any('blocked by hook' in str(msg.get('content','')) for msg in result.messages))

    def test_session_hook_context_reaches_system_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            client=MagicMock(); client.timeout=30
            client.chat.return_value={'response':'done','model':'test/model'}
            bus=HookBus(); bus.register('SessionStart',lambda _p:{'context':'PROJECT_POLICY_MARKER'})
            runtime=NativeLightRuntime(
                client=client,initial_prompt='Explain state',model='test/model',fallback_model=None,
                workspace_root=temp,tools=[],load_tools_on_start=False,persistent_plan=False,hooks=bus,base_timeout=30,max_iterations=1,
            )
            result=runtime.run()
            self.assertEqual(result.status,'completed')
            self.assertIn('PROJECT_POLICY_MARKER',result.system_prompt)


if __name__=='__main__': unittest.main()
