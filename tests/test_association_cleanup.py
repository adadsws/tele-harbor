"""真实残留的中性回归：重复辅助清单归档、冲突拒绝、不齐及用户文件保留。"""
import json
import unittest
from pathlib import Path
from uuid import uuid4

from matrix_harness.isolation import files, protected_state
from telegg.association_cleanup import archive_duplicate_associations
from telegg.browser import staged_batch_complete
from telegg.content_archive import save_group_manifest
from telegg.errors import LocalDataError
from telegg.layout import Message, group_folder
from telegg.reply_groups import linked_group_names
from telegg.scan_record import ScanRecord


class AssociationCleanupTests(unittest.TestCase):
    def fixture(self):
        base=Path(__file__).resolve().parents[1]/'~temp/tests/association-cleanup'/uuid4().hex
        root,staging=base/'output',base/'staging'
        record=ScanRecord(base/'scans/-1.jsonl')
        items=[Message(str(mid),'neutral',1700000000+mid,0,
                       ((str(mid),'neutral',1700000000+mid,False),),((str(mid),'text'),),
                       reply_targets=((str(mid),'100'),) if mid==101 else ()) for mid in (100,101)]
        record.record_scan(items,reached_oldest=True)
        name=linked_group_names(record)['100']
        for item in items:
            folder=root/name/group_folder(item);folder.mkdir(parents=True)
            save_group_manifest(folder,[item])
            text=folder/f'{item.mid}.txt';text.write_bytes(b'neutral')
            record.record_content_file(item.mid,text);record.record_result(item.mid,'saved_content')
        original=root/name/'关联清单.json'
        body=json.dumps({'replies':[{'source_message_id':'101','target_message_id':'100'}]},indent=2).encode()
        original.write_bytes(body)
        pending=staging/name/'关联清单.json';pending.parent.mkdir(parents=True);pending.write_bytes(body)
        return base,root,staging,record,pending,original

    def run_cleanup(self,base,root,staging,record):
        return archive_duplicate_associations(root,staging,record,staged_batch_complete,base/'archive')

    def test_identical_complete_component_archives_manifest_and_is_idempotent(self):
        base,root,stage,record,pending,original=self.fixture()
        before=protected_state({'output':root},list(files(root)))
        folders=self.run_cleanup(base,root,stage,record)
        self.assertEqual(folders,{pending.parent});self.assertFalse(pending.exists())
        archived=list((base/'archive').rglob('关联清单.json'));self.assertEqual(len(archived),1)
        self.assertEqual(archived[0].read_bytes(),original.read_bytes())
        self.assertTrue(archived[0].with_name('archive-record.json').is_file())
        self.assertEqual(before,protected_state({'output':root},list(files(root))))
        self.assertFalse(self.run_cleanup(base,root,stage,record))

    def test_conflicting_or_invalid_relations_fail_before_any_move(self):
        for kind in ('different','bad_same','wrong_same'):
            with self.subTest(kind=kind):
                base,root,stage,record,pending,original=self.fixture()
                body=b'{bad' if kind=='bad_same' else b'{"replies":[]}'
                pending.write_bytes(body)
                if kind!='different':original.write_bytes(body)
                before=protected_state({'all':base},list(files(base)))
                with self.assertRaises(LocalDataError):self.run_cleanup(base,root,stage,record)
                self.assertEqual(before,protected_state({'all':base},list(files(base))))

    def test_incomplete_component_or_unknown_file_stays(self):
        for kind in ('failed','missing_content','user_file'):
            with self.subTest(kind=kind):
                base,root,stage,record,pending,original=self.fixture()
                if kind=='failed':record.record_result('101','failed')
                elif kind=='missing_content':Path(record.content_files['101']).rename(base/'held-content')
                else:(pending.parent/'user.bin').write_bytes(b'keep')
                before=protected_state({'all':base},list(files(base)))
                self.assertFalse(self.run_cleanup(base,root,stage,record))
                self.assertEqual(before,protected_state({'all':base},list(files(base))))

    def test_bad_final_segment_manifest_is_not_silently_archived(self):
        base,root,stage,record,pending,original=self.fixture()
        manifest=next(root.rglob('段内清单.json'));manifest.write_bytes(b'{}')
        before=protected_state({'all':base},list(files(base)))
        with self.assertRaises(LocalDataError):self.run_cleanup(base,root,stage,record)
        self.assertEqual(before,protected_state({'all':base},list(files(base))))

    def test_preexisting_manifest_can_be_deferred_until_final_confirmation(self):
        base,root,stage,record,pending,original=self.fixture()
        before=protected_state({'all':base},list(files(base)))
        result=archive_duplicate_associations(root,stage,record,staged_batch_complete,base/'archive',exclude={pending})
        self.assertFalse(result)
        self.assertEqual(before,protected_state({'all':base},list(files(base))))
