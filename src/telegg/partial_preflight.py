"""编组迁移前检查选中附件的断点；只读失败不改变完好关联成员的位置。"""
import re

from .content_types import DOWNLOADABLE
from .errors import LocalDataError, RemoteFileError
from .resumable import PartialFile
from .media_index import media_version


async def validate_pending_media(page, peer, items, record, index, partials, source_for, fetch,
                                 *, trust_completed=False):
    for item in items:
        for mid, kind in item.content_types:
            if kind not in DOWNLOADABLE:
                continue
            if trust_completed and record.results.get(mid, {}).get('status') in {'downloaded','skipped','relocated'}:
                continue
            existing = index.get(mid, [])
            if (record.results.get(mid, {}).get('status') in {'downloaded', 'skipped', 'relocated'}
                    and not existing):
                raise LocalDataError(f'已完成媒体缺失，未移动关联成员：m{mid}')
            keys = {path.name.rsplit('.', 1)[0] for suffix in ('part', 'jsonl')
                    for path in partials.glob(f'{mid}*.{suffix}')
                    if re.fullmatch(re.escape(mid)+r'(?:\.v[1-9]\d*)?\.'+suffix, path.name)}
            for key in sorted(keys):
                payload = partials/f'{key}.part'
                version = int(key.split('.v')[1]) if '.v' in key else 0
                finalized = any(media_version(path) == version or
                                (version == 0 and media_version(path) == 1) for path in existing)
                if not payload.exists() and finalized:
                    # 完整入库后保留的提交日志，不是孤立断点。
                    continue
                try:
                    source = await source_for(page, peer, mid)
                    identity = {field: source[field] for field in ('peer', 'mid', 'kind', 'id', 'dc', 'thumb')}
                    partial = PartialFile(partials, key, identity, source['size'])
                    partial.open(readonly=True)
                    await partial.reconcile_tail(lambda offset, limit: fetch(page, source, offset, limit),
                                                 readonly=True)
                except RemoteFileError:
                    # 远端文件故障仍交给原下载路径，生成说明并继续其他附件。
                    continue
                except RuntimeError as exc:
                    raise LocalDataError(f'断点只读预检失败，未移动关联成员：m{mid}；{exc}') from exc
