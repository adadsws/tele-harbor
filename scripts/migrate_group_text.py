"""迁移已有编组、消息子目录名，并补齐完整文字.txt。"""

from __future__ import annotations

import argparse
from pathlib import Path

from telegg.content_archive import prepare_group_folder, prepare_item_folders
from telegg.layout import (group_folder, group_root, grouped, item_folder,
                           legacy_group_folder, old_group_folder, old_item_folder,
                           previous_group_folder)
from telegg.local_config import load_paths
from telegg.scan_record import ScanRecord


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='执行可恢复的目录重命名和 TXT 创建')
    args = parser.parse_args()
    paths = load_paths()
    counts = {'groups': 0, 'rename': 0, 'items': 0, 'txt': 0, 'conflicts': 0, 'mismatch': 0}
    for record_path in sorted(paths['scan_records'].glob('*.jsonl')):
        record = ScanRecord(record_path)
        identity = record_path.stem
        output = paths['output']
        roots = [group_root(output, '', identity)]
        roots.append(paths['staging'] / roots[0].name)
        for anchor, batch in grouped(record.ordered_messages()):
            if anchor is None:
                continue
            for root in roots:
                new = root / group_folder(anchor)
                olds = [root / name for name in dict.fromkeys((previous_group_folder(anchor),
                                                                old_group_folder(anchor), legacy_group_folder(anchor)))
                        if name != new.name]
                present = [old for old in olds if old.exists()]
                if not present and not new.is_dir():
                    continue
                counts['groups'] += 1
                if len(present) > 1 or (present and new.exists()):
                    counts['conflicts'] += 1
                    print(f'冲突：{present} -> {new}')
                    continue
                current = present[0] if present else new
                if not current.is_dir():
                    counts['conflicts'] += 1
                    print(f'不是目录：{current}')
                    continue
                rename = bool(present)
                item_moves = sum((current / old_item_folder(item)).is_dir() for item in batch) if len(batch) > 1 else 0
                if any((current / old_item_folder(item)).exists() and
                       (current / item_folder(item)).exists() for item in batch):
                    counts['conflicts'] += 1
                    print(f'消息子目录冲突：{current}')
                    continue
                text_path = current / '完整文字.txt'
                txt = len(anchor.text) > 20 and not text_path.is_file()
                if text_path.exists() and (not text_path.is_file() or
                                           text_path.read_bytes() != anchor.text.encode('utf-8')):
                    counts['mismatch'] += 1
                    print(f'TXT 内容不符：{text_path}')
                    continue
                if args.apply:
                    prepare_group_folder(root, anchor, create=True)
                    prepare_item_folders(new, batch)
                counts['rename'] += int(rename)
                counts['items'] += item_moves
                counts['txt'] += int(txt)
    print(f"编组 {counts['groups']}，需改名 {counts['rename']}，子目录改名 {counts['items']}，需写 TXT {counts['txt']}，冲突 {counts['conflicts']}，TXT 不符 {counts['mismatch']}")
    if counts['conflicts'] or counts['mismatch']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
