from __future__ import annotations
import json, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from aicoder.change_journal import ChangeJournal
import aicoder.executor as executor
import aicoder.settings as settings
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

    def test_file_snapshot_restore_requires_approval_and_restores_exact_content(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); journal=ChangeJournal(root/'journal'); target=root/'file.txt'; target.write_text('before')
            metadata=journal.prepare_file_change(target)
            target.write_text('after')
            metadata=journal.finalize_restore_metadata(metadata)
            row=journal.record(tool='file_edit',arguments={'path':str(target)},risk='write',approved=True,result='ok',is_error=False,reversible=metadata)
            with self.assertRaises(PermissionError):
                journal.rollback(row['id'])
            result=journal.rollback(row['id'],approved=True)
            self.assertTrue(result['ok']); self.assertEqual(target.read_text(),'before')
            saved=journal.get(row['id']); self.assertEqual(saved['rollback_status'],'completed'); self.assertEqual(saved['verification'],'rolled_back')

    def test_file_rollback_refuses_to_overwrite_newer_work(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); journal=ChangeJournal(root/'journal'); target=root/'file.txt'; target.write_text('before')
            metadata=journal.prepare_file_change(target); target.write_text('after'); metadata=journal.finalize_restore_metadata(metadata)
            row=journal.record(tool='file_edit',arguments={},risk='write',approved=True,result='ok',is_error=False,reversible=metadata)
            target.write_text('newer')
            with self.assertRaisesRegex(ValueError,'newer work'):
                journal.rollback(row['id'],approved=True)
            self.assertEqual(target.read_text(),'newer')
            self.assertEqual(journal.get(row['id'])['rollback_status'],'failed')

    def test_created_file_and_empty_directory_are_reversible(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); journal=ChangeJournal(root/'journal')
            target=root/'created.txt'; metadata=journal.prepare_file_change(target); target.write_text('created'); metadata=journal.finalize_restore_metadata(metadata)
            row=journal.record(tool='file_edit',arguments={},risk='write',approved=True,result='ok',is_error=False,reversible=metadata)
            journal.rollback(row['id'],approved=True); self.assertFalse(target.exists())
            directory=root/'created-dir'; dmeta=journal.prepare_directory_create(directory); directory.mkdir()
            drow=journal.record(tool='directory_create',arguments={},risk='write',approved=True,result='ok',is_error=False,reversible=dmeta)
            journal.rollback(drow['id'],approved=True); self.assertFalse(directory.exists())

    def test_directory_rollback_refuses_nonempty_directory(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); journal=ChangeJournal(root/'journal'); directory=root/'created-dir'
            metadata=journal.prepare_directory_create(directory); directory.mkdir(); (directory/'later.txt').write_text('keep')
            row=journal.record(tool='directory_create',arguments={},risk='write',approved=True,result='ok',is_error=False,reversible=metadata)
            with self.assertRaises(OSError): journal.rollback(row['id'],approved=True)
            self.assertTrue((directory/'later.txt').exists())

    def test_arbitrary_restore_kind_is_never_executed(self):
        with tempfile.TemporaryDirectory() as t:
            journal=ChangeJournal(Path(t)); row=journal.record(tool='demo',arguments={},risk='write',approved=True,result='ok',is_error=False,reversible={'kind':'command','value':'anything'})
            with self.assertRaisesRegex(ValueError,'unsupported rollback kind'):
                journal.rollback(row['id'],approved=True)

    def test_executor_file_edit_records_private_snapshot_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); workspace=root/'workspace'; workspace.mkdir(); target=workspace/'x.txt'; target.write_text('before')
            journal_root=root/'config'/'changes'
            client=MagicMock()
            with patch.object(executor,'_workspace_root',return_value=workspace), patch('aicoder.change_journal.CONFIG_DIR',root/'config'), patch.object(executor.audit,'log_tool'):
                result,is_error=executor.run_tool(
                    client,'file_edit',{'path':'x.txt','operation':'write','content':'after'},
                    approval_fn=lambda _n,_a: True,allowed_tools={'file_edit'}
                )
            self.assertFalse(is_error); self.assertIn('updated',result); self.assertEqual(target.read_text(),'after')
            journal=ChangeJournal(journal_root); rows=journal.list(10); self.assertTrue(rows)
            row=next(item for item in rows if item['tool']=='file_edit')
            self.assertTrue(row['reversible']); self.assertEqual(row['restore_metadata']['kind'],'restore_file')
            backup=Path(row['restore_metadata']['backup_path']); self.assertTrue(backup.is_file()); self.assertEqual(backup.stat().st_mode & 0o777,0o600)
            journal.rollback(row['id'],approved=True); self.assertEqual(target.read_text(),'before')

    def test_executor_settings_change_records_typed_previous_state_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); state_path=root/'state.json'; store=settings.SettingsStore(lambda: state_path)
            store.set('request_timeout',120)
            journal_root=root/'config'/'changes'; client=MagicMock()
            with patch.object(settings,'STORE',store), patch('aicoder.change_journal.CONFIG_DIR',root/'config'), patch.object(executor.audit,'log_tool'):
                result,is_error=executor.run_tool(
                    client,'settings_apply_patch',{'patch':{'request_timeout':180},'reason':'test'},
                    approval_fn=lambda _n,_a: True,allowed_tools={'settings_apply_patch'}
                )
                self.assertFalse(is_error); self.assertEqual(store.get('request_timeout'),180)
                journal=ChangeJournal(journal_root); row=next(item for item in journal.list(10) if item['tool']=='settings_apply_patch')
                self.assertTrue(row['reversible']); self.assertEqual(row['restore_metadata']['kind'],'settings_patch')
                self.assertEqual(row['restore_metadata']['previous']['request_timeout'],120)
                journal.rollback(row['id'],approved=True)
                self.assertEqual(store.get('request_timeout'),120)

    def test_settings_rollback_refuses_to_clobber_later_setting_change(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); store=settings.SettingsStore(lambda: root/'state.json'); store.set('request_timeout',120)
            journal=ChangeJournal(root/'journal')
            metadata={'kind':'settings_patch','previous':{'request_timeout':120},'post':{'request_timeout':180}}
            row=journal.record(tool='settings_apply_patch',arguments={},risk='write',approved=True,result='ok',is_error=False,reversible=metadata)
            store.set('request_timeout',200)
            with patch.object(settings,'STORE',store):
                with self.assertRaisesRegex(ValueError,"changed after"):
                    journal.rollback(row['id'],approved=True)
            self.assertEqual(store.get('request_timeout'),200)

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
