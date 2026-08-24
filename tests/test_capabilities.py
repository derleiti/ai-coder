import unittest
from aicoder.capabilities import MAX_ACTIVE_TOOLS, resolve_capabilities, select_tools

class CapabilityTests(unittest.TestCase):
    def test_url_starts_web_only_family(self):
        r=resolve_capabilities('Prüfe https://example.com/docs')
        self.assertIn('web',r.capabilities); self.assertNotIn('local_code_write',r.capabilities)
    def test_traceback_requests_debug_and_read(self):
        r=resolve_capabilities('Traceback: ValueError in app.py')
        self.assertIn('debug',r.capabilities); self.assertIn('local_code_read',r.capabilities)
    def test_docker_maps_system_and_containers(self):
        r=resolve_capabilities('Warum ist Docker langsam?')
        self.assertIn('containers',r.capabilities); self.assertIn('system_diagnostics',r.capabilities)
    def test_settings_is_narrow(self):
        r=resolve_capabilities('ändere die AICoder settings')
        self.assertIn('settings',r.capabilities); self.assertIn('local_code_write',r.capabilities)
    def test_budget_is_enforced(self):
        tools=[{'name':'file_read'},{'name':'file_tree'},{'name':'code_grep'},{'name':'file_edit'},{'name':'shell'},{'name':'test'},{'name':'lint'}]
        selected=select_tools(tools,resolve_capabilities('fix error in app.py and test it'),budget=4)
        self.assertLessEqual(len(selected),4)
    def test_budget_cannot_exceed_global_max(self):
        tools=[{'name':'file_read'} for _ in range(40)]
        self.assertLessEqual(len(select_tools(tools,resolve_capabilities('error app.py'),budget=999)),MAX_ACTIVE_TOOLS)
    def test_unknown_tool_not_auto_selected(self):
        self.assertEqual(select_tools([{'name':'mystery_root_tool'}],resolve_capabilities('fix error')),[])


class WorkingSetTests(unittest.TestCase):
    def setUp(self):
        from aicoder.capabilities import runtime_meta_tools
        self.tools = [
            {'name':'shell','description':'local shell'},
            {'name':'file_read','description':'read files'},
            {'name':'file_edit','description':'edit files'},
            {'name':'test','description':'run tests'},
            {'name':'search','description':'search the web'},
            {'name':'crawl','description':'crawl web pages'},
            {'name':'memory_search','description':'search memory'},
            *runtime_meta_tools(),
        ]

    def test_working_set_keeps_primitives_and_task_specific_tools(self):
        from aicoder.capabilities import build_working_set
        selected = build_working_set(self.tools, resolve_capabilities('research latest release'), budget=12)
        names = {tool['name'] for tool in selected}
        self.assertIn('shell', names)
        self.assertIn('file_read', names)
        self.assertIn('search', names)

    def test_toolbox_search_hides_active_tools(self):
        from aicoder.capabilities import search_toolbox
        matches = search_toolbox(self.tools, 'web search', active_names={'shell','search'})
        names = [item['name'] for item in matches]
        self.assertNotIn('search', names)
        self.assertIn('crawl', names)

    def test_expansion_accepts_capability_name(self):
        from aicoder.capabilities import expansion_tools
        added = expansion_tools(self.tools, ['memory'], active_names={'shell'}, slots=2)
        self.assertEqual([tool['name'] for tool in added], ['memory_search'])

    def test_improvisation_never_auto_activates_code(self):
        from aicoder.capabilities import improvisation_advice
        advice = improvisation_advice('special missing thing', [])
        self.assertEqual(advice['action'], 'improvise')
        self.assertIn('disabled plugin/MCP', advice['reason'])

if __name__ == '__main__': unittest.main()
