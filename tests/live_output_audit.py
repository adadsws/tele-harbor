"""Audit finished manifests and publication state without displaying content."""

import json
import re
from pathlib import Path
from telegg.local_config import load_paths
from telegg.scan_record import ScanRecord
from telegg.layout import grouped, group_folder
from telegg.reply_groups import linked_group_names
from telegg.browser import existing_media_index
from telegg.content_types import DOWNLOADABLE
from telegg.group_menu import load_groups


def main():
    paths = load_paths()
    failures = []
    expected_peers = {group.identity for group in load_groups()}
    seen_peers = set()
    baseline = {}
    for journal in (paths['output'].parent / '~local' / '~archive').glob('rebuild-empty-logic-*/moves.jsonl'):
        for line in journal.read_text(encoding='utf-8').splitlines():
            source = Path(json.loads(line)['from'])
            peer = next((match.group(1) for part in source.parts
                         if (match := re.search(r'\[(-\d+)\]$', part))), None)
            media = re.match(r'^\d{4}-\d{2}-\d{2}-m(\d+)_\d+_\d+', source.name)
            if peer and media:
                baseline.setdefault(peer, set()).add(media.group(1))
    for root in paths['output'].iterdir():
        if not root.is_dir():
            continue
        peer = root.name.rsplit('[', 1)[-1].rstrip(']')
        if peer in seen_peers:
            failures.append((peer, 'duplicate_output_group'))
        seen_peers.add(peer)
        record = ScanRecord(paths['scan_records'] / f'{peer}.jsonl')
        index = existing_media_index(root, known_text_media=set(record.media))
        missing_baseline = baseline.get(peer, set()) - index.keys()
        if missing_baseline:
            failures.append((peer, 'missing_pre_rebuild_media', len(missing_baseline)))
        links = linked_group_names(record)
        expected = {group_folder(anchor): {part[0] for item in batch for part in item.parts}
                    for anchor, batch in grouped(record.ordered_messages())}
        seen = set()
        segments = 0
        for manifest in root.rglob('段内清单.json'):
            segments += 1
            ids = {mid for row in json.loads(manifest.read_text(encoding='utf-8'))['items']
                   for mid in row['message_ids']}
            if seen & ids or expected.get(manifest.parent.name) != ids:
                failures.append((peer, 'segment_membership', manifest.parent.name))
            seen.update(ids)
        for mid in seen:
            source = record.source_messages.get(mid, {})
            result = record.results.get(mid, {})
            if result.get('status') not in ('downloaded', 'skipped', 'relocated', 'saved_content', 'no_media'):
                failures.append((peer, 'missing_result', mid))
            if mid not in record.content_files or not Path(record.content_files[mid]).is_file():
                failures.append((peer, 'missing_content', mid))
            if source.get('content_type') in DOWNLOADABLE and len(index.get(mid, [])) != 1:
                failures.append((peer, 'media_count', mid))
        linked_count = 0
        by_mid = {part[0]: links.get(anchor.mid) for anchor, batch in grouped(record.ordered_messages())
                  for item in batch for part in item.parts}
        for parent in root.glob('*关联组'):
            linked_count += 1
            ids = {mid for manifest in parent.rglob('段内清单.json')
                   for row in json.loads(manifest.read_text(encoding='utf-8'))['items']
                   for mid in row['message_ids']}
            required = {(mid, str(source['reply_to'])) for mid, source in record.source_messages.items()
                        if source.get('reply_to') and by_mid.get(mid) == parent.name
                        and by_mid.get(str(source['reply_to'])) == parent.name}
            manifest = parent / '关联清单.json'
            if not manifest.is_file():
                failures.append((peer, 'missing_link_manifest', parent.name))
                continue
            pairs = {(row['source_message_id'], row['target_message_id'])
                     for row in json.loads(manifest.read_text(encoding='utf-8'))['replies']}
            if not pairs or pairs != required or any(a not in ids or b not in ids for a, b in pairs):
                failures.append((peer, 'link_membership', parent.name))
        empty = sum(1 for folder in root.rglob('*') if folder.is_dir() and not any(folder.iterdir()))
        if empty:
            failures.append((peer, 'empty_output_dirs', empty))
        print(json.dumps({'peer': peer, 'messages': len(seen), 'segments': segments,
                          'media': sum(len(v) for v in index.values()), 'linked_groups': linked_count,
                          'pre_rebuild_media': len(baseline.get(peer, set())),
                          'empty_dirs': empty}, ensure_ascii=False))
    failures.extend((peer, 'missing_output_group') for peer in sorted(expected_peers - seen_peers))
    staging_files = sum(1 for path in paths['staging'].rglob('*') if path.is_file())
    print(json.dumps({'staging_files': staging_files, 'failures': failures}, ensure_ascii=False))
    if failures or staging_files:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
