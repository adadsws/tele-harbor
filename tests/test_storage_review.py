"""三模式新边界：只查登记路径、一次确认、服务器补齐、额外归档及拒绝保护。"""
import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from matrix_harness.isolation import files, protected_state, sha
from telegg.content_archive import save_group_manifest, save_message_content, save_text_file
from telegg.errors import LocalDataError
from telegg.layout import Message, group_folder
from telegg.scan_record import FoundMedia, ScanRecord
from telegg.storage_review import inspect_group, finish_review, repair_one
from telegg.verification_manifest import verified_digest


class StorageReviewTests(unittest.TestCase):
    def fixture(self, base=None, peer='-1000000005'):
        base=base or Path(__file__).resolve().parents[1]/'~temp/tests/storage-review'/uuid4().hex
        output,staging,partials=base/'output',base/'staging',base/'partials'
        mid='4294967338';part=(mid,'',1700000000,True)
        item=Message(mid,'',1700000000,1,(part,),((mid,'video'),),album_id='7777')
        folder=output/f'neutral [{peer}]'/group_folder(item)/'2023-11-15-g7777'
        folder.mkdir(parents=True);save_group_manifest(folder.parent,[item])
        media=folder/f'2023-11-15-m{mid}_1_999.bin';media.write_bytes(b'abc')
        identity={'peer':peer,'mid':mid,'kind':'document','id':'999','dc':1,'thumb':''}
        verified_digest(media,identity,partials/peer/f'{mid}.verified.jsonl',sha)
        record=ScanRecord(base/'scans'/f'{peer}.jsonl');record.record_scan([item],reached_oldest=False)
        record.record_content_file(mid,save_message_content(base/'state',item,part))
        record.record_discovered(mid,[FoundMedia(mid,1,mid,3)])
        record.record_result(mid,'downloaded',detail=str(media),naming_revision=3)
        page=AsyncMock();page.evaluate.return_value=[{'id':42,'type':'message','media_id':'999','has_text':False,'peer_id':abs(int(peer))}]
        return base,output,staging,partials,media,identity,record,page

    def inspect(self,f,output=None):
        base,out,stage,parts,media,identity,record,page=f
        with (patch('telegg.storage_review.load_paths',create=True),
              patch('telegg.local_config.load_paths',return_value={'scan_records':base/'scans'}),
              patch('telegg.extra_files.load_paths',return_value={'scan_records':base/'scans'}),
              patch('telegg.browser.open_group',new_callable=AsyncMock),redirect_stdout(output or io.StringIO())):
            return asyncio.run(inspect_group(page,f'https://web.telegram.org/k/#{identity["peer"]}',out,stage,parts))

    def test_moved_copy_is_extra_and_original_path_missing_without_reuse(self):
        f=self.fixture();base,out,stage,parts,media,identity,record,page=f
        moved=media.parent/'other'/media.name;moved.parent.mkdir();media.rename(moved)
        before=protected_state({'all':base},list(files(base)));review=self.inspect(f)
        self.assertEqual(review.missing,[media]);self.assertEqual(review.extras,[moved])
        self.assertEqual(before,protected_state({'all':base},list(files(base))))

    def test_missing_and_extras_use_one_inventory_and_are_reported_together(self):
        f=self.fixture();media=f[4];moved=media.parent/'other'/media.name
        moved.parent.mkdir();media.rename(moved);output=io.StringIO()
        review=self.inspect(f,output);text=output.getvalue()
        self.assertEqual((review.missing,review.extras),([media],[moved]))
        self.assertIn('同轮结果：已下载缺失 1 个，额外/重复 1 个',text)
        self.assertIn('缺失/额外联合检查',text)
        self.assertEqual(f[7].evaluate.await_count,1)

    def test_structure_snapshots_are_reused_by_hash_stage(self):
        from telegg.verification_manifest import read_snapshot
        f=self.fixture()
        with (patch('telegg.local_validation.read_snapshot',wraps=read_snapshot) as read,
              patch('telegg.review_progress.read_snapshot',side_effect=AssertionError('duplicate snapshot read'))):
            self.inspect(f)
        self.assertEqual(read.call_count,1)

    def test_no_eof_and_blank_do_not_modify_or_repeat_question(self):
        for answer in ('n','',EOFError()):
            with self.subTest(answer=type(answer).__name__):
                f=self.fixture();base,out,stage,parts,media,identity,record,page=f
                media.rename(base/'held.bin');review=self.inspect(f)
                read=Mock(side_effect=answer if isinstance(answer,Exception) else None,return_value=answer)
                before=protected_state({'all':base},list(files(base)))
                with redirect_stdout(io.StringIO()):result=asyncio.run(finish_review(page,[review],[],read=read))
                self.assertEqual(result,(0,False));read.assert_called_once()
                self.assertEqual(before,protected_state({'all':base},list(files(base))))

    def test_yes_downloads_remotely_and_archives_moved_copy(self):
        f=self.fixture();base,out,stage,parts,media,identity,record,page=f
        moved=media.parent/'other'/media.name;moved.parent.mkdir();media.rename(moved)
        review=self.inspect(f);get=AsyncMock(return_value={**identity,'size':3})
        async def fresh(_page,_target,task,index,**kw):
            self.assertEqual(index,{})
            self.assertIn('recovery',task.partial_root.parts)
            candidate=task.folder/'fresh.bin';candidate.parent.mkdir(parents=True);candidate.write_bytes(b'abc')
            return 'downloaded',candidate,3,sha(candidate),None
        read=Mock(return_value='y')
        with (patch('telegg.browser.server_media_source',get),patch('telegg.browser.save_one',side_effect=fresh) as save,
              redirect_stdout(io.StringIO())):
            result=asyncio.run(finish_review(page,[review],[],read=read))
        self.assertEqual(result,(0,True));read.assert_called_once();get.assert_awaited_once();self.assertEqual(save.call_count,1)
        self.assertEqual(media.read_bytes(),b'abc');self.assertFalse(moved.exists())
        archived=list((base/'~archive').rglob(moved.name));self.assertEqual(len(archived),1)
        self.assertEqual(archived[0].read_bytes(),b'abc')

    def test_change_during_confirmation_stops_before_archive_or_download(self):
        f=self.fixture();base,out,stage,parts,media,identity,record,page=f
        extra=media.parent/'extra.bin';extra.write_bytes(b'old');review=self.inspect(f)
        def read(_):extra.write_bytes(b'user changed');return 'y'
        with redirect_stdout(io.StringIO()),self.assertRaises(LocalDataError):
            asyncio.run(finish_review(page,[review],[],read=read))
        self.assertEqual(extra.read_bytes(),b'user changed');self.assertFalse((base/'~archive').exists())

    def test_changed_server_identity_is_not_published(self):
        f=self.fixture();base,out,stage,parts,media,identity,record,page=f
        media.rename(base/'held.bin');review=self.inspect(f)
        with (patch('telegg.browser.server_media_source',new_callable=AsyncMock,return_value={**identity,'id':'1000','size':3}),
              patch('telegg.browser.save_one',new_callable=AsyncMock) as save,redirect_stdout(io.StringIO())):
            failed,changed=asyncio.run(finish_review(page,[review],[],read=lambda _:'y'))
        self.assertEqual(failed,1);self.assertTrue(changed);save.assert_not_awaited();self.assertFalse(media.exists())

    def test_multigroup_confirmation_is_once_and_empty_review_never_asks(self):
        f=self.fixture();f2=self.fixture(f[0],'-1000000006')
        for values in (f,f2):values[4].rename(values[0]/(values[5]['peer']+'-held.bin'))
        reviews=[self.inspect(f),self.inspect(f2)];read=Mock(return_value='n')
        with redirect_stdout(io.StringIO()):asyncio.run(finish_review(f[7],reviews,[],read=read))
        read.assert_called_once()
        read.reset_mock()
        with redirect_stdout(io.StringIO()):asyncio.run(finish_review(f[7],[],[],read=read))
        read.assert_not_called()

    def test_existing_corruption_is_rejected_without_repair(self):
        f=self.fixture();base=f[0];f[4].write_bytes(b'xyz')
        before=protected_state({'all':base},list(files(base)))
        with self.assertRaisesRegex(LocalDataError,'SHA-256'):
            self.inspect(f)
        self.assertEqual(before,protected_state({'all':base},list(files(base))))

    def test_missing_original_text_comes_from_server_without_new_message_record(self):
        f=self.fixture();base,out,stage,parts,media,identity,record,page=f
        mid=identity['mid'];part=(mid,'neutral text',1700000000,True)
        item=Message(mid,part[1],part[2],1,(part,),((mid,'video'),),album_id='7777')
        record.record_scan([item],reached_oldest=False)
        text=save_text_file(media.parent,part);record.record_content_file(mid,text)
        text.rename(base/'held-text.txt');page.evaluate.return_value[0]['has_text']=True
        review=self.inspect(f);before=sha(record.path)
        with (patch('telegg.browser.server_older_batch',new_callable=AsyncMock,return_value=[item]) as server,
              patch('telegg.browser.server_media_source',new_callable=AsyncMock) as media_source,
              redirect_stdout(io.StringIO())):
            failed,changed=asyncio.run(finish_review(page,[review],[],read=lambda _:'y'))
        self.assertEqual((failed,changed),(0,True));self.assertEqual(text.read_text(encoding='utf-8'),'neutral text')
        self.assertEqual(sha(record.path),before);server.assert_awaited_once();media_source.assert_not_awaited()

    def test_legal_media_versions_are_not_added_as_misplaced_extras(self):
        f=self.fixture();base,out,stage,parts,media,identity,record,page=f
        older=media.with_name(media.stem+'.v1'+media.suffix);media.rename(older)
        latest=media.with_name(media.stem+'.v2'+media.suffix);latest.write_bytes(b'abc')
        record.record_result(identity['mid'],'downloaded',detail=str(latest),naming_revision=3)
        verified_digest(latest,identity,parts/identity['peer']/f'{identity["mid"]}.verified.jsonl',sha)
        review=self.inspect(f)
        self.assertFalse(review.missing);self.assertFalse(review.extras)
