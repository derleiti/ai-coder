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

if __name__ == '__main__': unittest.main()
