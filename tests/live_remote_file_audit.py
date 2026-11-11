"""只读定位 Telegram 单文件 RPC 500；不显示原文、文件名、缩略图或媒体字节。"""
import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from telegg.browser import open_group, server_media_source, wait_for_login
from telegg.group_menu import parse_groups
from telegg.local_config import CONFIG_FILE
from matrix_harness.isolation import write_json
from live_test_policy import assert_live_test_allowed

CASES = ('original-4k', 'neighbor-4k', 'original-empty-reference',
         'original-64k', 'original-1m', 'original-middle', 'original-tail',
         'original-after-end', 'original-precise-1k', 'original-cdn',
         'original-thumbnail', 'original-hashes', 'neighbor-hashes',
         'original-native', 'neighbor-native', 'original-client', 'neighbor-client')


INITIALIZE = '''async ({peerId, mid, neighbor}) => {
    const managers=window.appDownloadManager.managers, api=managers.apiManager;
    const peer=await managers.appPeersManager.getInputPeerById(peerId);
    const ids=[mid,neighbor].map(id=>({_:'inputMessageID',id:Number(id)%4294967296}));
    const result=await api.invokeApi('channels.getMessages',{
        channel:{_:'inputChannel',channel_id:peer.channel_id,access_hash:peer.access_hash},id:ids
    },{noErrorBox:true,noRetry:true});
    const doc=result.messages.find(m=>Number(m.id)===Number(mid)%4294967296)?.media?.document;
    const other=result.messages.find(m=>Number(m.id)===Number(neighbor)%4294967296)?.media?.document;
    const old=window.__teleggServerMedia.get(mid).media.document;
    if(!doc || !other || String(doc.id)!==String(old.id) || Number(doc.size)!==Number(old.size))
        throw Error('fresh identity mismatch');
    const remoteThumbs=(doc.thumbs||[]).filter(t=>['photoSize','photoSizeProgressive'].includes(t._));
    window.__teleggRootProbe={doc,other,remoteThumbs};
    return {same_id:String(doc.id)===String(old.id),same_access_hash:String(doc.access_hash)===String(old.access_hash),
        reference_changed:String([...doc.file_reference])!==String([...old.file_reference]),
        size:Number(doc.size),dc:Number(doc.dc_id),remote_thumbs:remoteThumbs.length,
        document_type:doc._,video:(doc.attributes||[]).some(a=>a._==='documentAttributeVideo')};
}'''

CHECK = '''async name => {
    const {doc,other,remoteThumbs}=window.__teleggRootProbe;
    const managers=window.appDownloadManager.managers,api=managers.apiManager;
    const media=name.startsWith('neighbor')?other:doc;
    const location={_:'inputDocumentFileLocation',id:media.id,access_hash:media.access_hash,
        file_reference:media.file_reference,thumb_size:''};
    const params={location,offset:0,limit:4096};
    let method='upload.getFile',native=name.includes('native');
    const options={dcId:media.dc_id,fileDownload:!name.includes('client'),
        noRetry:true,noErrorBox:true,rawError:true,floodMaxTimeout:0};
    if(name==='original-64k')params.limit=65536;
    if(name==='original-1m')params.limit=1048576;
    if(name==='original-middle')params.offset=1048576;
    if(name==='original-tail')params.offset=Math.floor((Number(media.size)-4096)/4096)*4096;
    if(name==='original-after-end')params.offset=Math.ceil(Number(media.size)/1048576)*1048576;
    if(name==='original-precise-1k'){params.limit=1024;params.precise=true;}
    if(name==='original-cdn')params.cdn_supported=true;
    if(name==='original-empty-reference')location.file_reference=new Uint8Array(0);
    if(name==='original-thumbnail') {
        if(!remoteThumbs.length)return {name,not_applicable:true};
        location.thumb_size=remoteThumbs[0].type;
    }
    if(name.includes('hashes')) {method='upload.getFileHashes';delete params.limit;}
    const invoke=api.invokeApi;
    const started=performance.now();
    try {
        if(native)api.invokeApi=function(method,args,opts){
            return invoke.call(this,method,args,{...opts,noRetry:true,noErrorBox:true,rawError:true});
        };
        const request=native?managers.apiFileManager.requestFilePart({
            dcId:media.dc_id,location,offset:params.offset,limit:params.limit
        }):api.invokeApi(method,params,options);
        const result=await Promise.race([request,new Promise((_,reject)=>
            setTimeout(()=>reject(Error('probe_timeout')),12000))]);
        return {name,seconds:(performance.now()-started)/1000,type:result?._||'',
            bytes:result?.bytes?.length||0,hash_count:Array.isArray(result)?result.length:0,
            hash_coverage:Array.isArray(result)?result.reduce((sum,h)=>sum+Number(h.limit||0),0):0};
    } catch(error) {
        return {name,seconds:(performance.now()-started)/1000,code:Number(error?.code)||0,
            type:/^[A-Z_0-9]+$/.test(error?.type||'')?error.type:'',
            timeout:String(error).includes('probe_timeout')};
    } finally {if(native)api.invokeApi=invoke;}
}'''


async def audit(args):
    groups = parse_groups(json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig'))['groups'])
    group = groups[args.source_group - 1]
    assert_live_test_allowed(group.identity, args.mid)
    records = []
    report = args.suite / ('remote-file-root-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(str(args.suite / 'profile'),
            channel='chrome', headless=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('https://web.telegram.org/k/', wait_until='domcontentloaded')
            await wait_for_login(page, timeout=30_000)
            await open_group(page, group.url, require_message=False)
            await server_media_source(page, int(group.identity), args.mid)
            metadata = await page.evaluate(INITIALIZE, {'peerId': int(group.identity),
                'mid': args.mid, 'neighbor': str(int(args.mid) + 1)})
            print(json.dumps({'metadata': metadata}), flush=True)
            names = args.case or CASES
            for name in names:
                result = await page.evaluate(CHECK, name)
                records.append(result)
                write_json(report, {'metadata': metadata, 'checks': records, 'media_files_written': 0})
                print(json.dumps(result), flush=True)
                if result.get('code') == 420:
                    print(json.dumps({'stopped': 'rate_limit', 'retry_after': result['type']}), flush=True)
                    return
                if result.get('timeout') and 'native' in name:
                    print(json.dumps({'stopped': 'native_timeout',
                                      'pending_cancelled_by_context_close': True}), flush=True)
                    return
            if args.https or not args.case:
                await page.evaluate("window.appDownloadManager.managers.apiManager.changeTransportType('https')")
                for name in ('original-4k', 'neighbor-4k'):
                    result = await page.evaluate(CHECK, name)
                    result['transport'] = 'https'
                    records.append(result)
                    print(json.dumps(result), flush=True)
            write_json(report, {'metadata': metadata,
                       'checks': records, 'media_files_written': 0})
        finally:
            await context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--source-group', type=int, required=True)
    parser.add_argument('--mid', required=True)
    parser.add_argument('--case', nargs='+', choices=CASES)
    parser.add_argument('--https', action='store_true', help='追加 HTTPS 对照；默认完整诊断已包含')
    args = parser.parse_args()
    args.suite = args.suite.resolve()
    root = Path(__file__).resolve().parents[1] / '~temp/remaining-live'
    if not args.suite.is_relative_to(root.resolve()) or not (args.suite / 'profile').is_dir():
        parser.error('suite 必须是项目 ~temp/remaining-live 下的既有登录副本')
    if not args.mid.isdecimal():
        parser.error('mid 必须为原消息号')
    asyncio.run(audit(args))


if __name__ == '__main__':
    main()
