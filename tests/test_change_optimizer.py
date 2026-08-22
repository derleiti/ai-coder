from __future__ import annotations
import json, tempfile, unittest
from pathlib import Path
from aicoder.change_journal import ChangeJournal
from aicoder.optimizer import build_plan, inspect_system

class FakeProvider:
    def execute(self,name,args):
        data={
            'os_system_overview': {'hostname':'test','release':'9.9','memory':{'MemAvailable':'8 GiB'}},
            'os_kernel_info': {'uname':{'release':'9.9'}},
            'os_storage_overview': {'filesystems':{'stdout':'ok'}},
        }[name]
        return json.dumps(data),False

class ChangeJournalTests(unittest.TestCase):
    def test_private_record_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as t:
            journal=ChangeJournal(Path(t))
            row=journal.record(tool='demo_change',arguments={'token':'abc','note':'password=hunter2'},risk='test',approved=True,result='token=secretvalue done',is_error=False)
            path=Path(t)/f"change-{row['id']}.json"
            text=path.read_text()
            self.assertNotIn('hunter2',text)
            self.assertNotIn('secretvalue',text)
            self.assertNotIn('"abc"',text)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(journal.get(row['id'])['verification'],'pending')
    def test_list_and_mark_verified(self):
        with tempfile.TemporaryDirectory() as t:
            journal=ChangeJournal(Path(t)); row=journal.record(tool='demo',arguments={},risk='test',approved=True,result='ok',is_error=False)
            self.assertEqual(journal.list(1)[0]['id'],row['id'])
            self.assertTrue(journal.mark_verified(row['id'],'passed'))
            self.assertEqual(journal.get(row['id'])['verification'],'passed')

class OptimizerTests(unittest.TestCase):
    def test_inspect_is_read_only_evidence(self):
        evidence=inspect_system(FakeProvider())
        self.assertTrue(all(item['ok'] for item in evidence.values()))
        self.assertEqual(evidence['os_system_overview']['data']['hostname'],'test')
    def test_plan_is_evidence_first_and_non_mutating(self):
        plan=build_plan('Python Docker local AI, stability first',FakeProvider()).to_dict()
        self.assertEqual(plan['priorities']['stability'],'very_high')
        self.assertEqual(plan['priorities']['containers'],'high')
        self.assertEqual(plan['priorities']['development'],'high')
        self.assertTrue(plan['proposed_actions'])
        self.assertTrue(all(action['mutation'] is False for action in plan['proposed_actions']))

if __name__=='__main__': unittest.main()
