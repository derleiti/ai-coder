import unittest
from aicoder.capabilities import MAX_ACTIVE_TOOLS, META_TOOL_NAMES, build_working_set, resolve_capabilities, select_tools

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

    def test_url_working_set_is_small_and_has_no_shell(self):
        tools=[
            {"name":"shell","capabilities":["local_code_write"]},
            {"name":"file_read","capabilities":["local_code_read"]},
            {"name":"search","capabilities":["web","research"]},
            {"name":"crawl","capabilities":["web","research"]},
        ]
        active=build_working_set(tools,resolve_capabilities("https://example.com/docs"),budget=6)
        names={tool["name"] for tool in active}
        self.assertTrue(set(META_TOOL_NAMES).issubset(names))
        self.assertIn("search",names)
        self.assertIn("crawl",names)
        self.assertNotIn("shell",names)
        self.assertNotIn("file_read",names)
        self.assertLessEqual(len(active),6)

    def test_working_set_budget_includes_meta_tools(self):
        tools=[{"name":"search","capabilities":["web"]}]
        active=build_working_set(tools,resolve_capabilities("https://example.com"),budget=4)
        self.assertEqual(len(active),4)
        self.assertEqual([tool["name"] for tool in active[:3]],list(META_TOOL_NAMES))

if __name__ == '__main__': unittest.main()
