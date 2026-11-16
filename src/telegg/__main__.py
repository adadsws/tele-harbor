"""BAT/CMD 入口：正常可见运行，自动调试可明确选择无窗口。"""

from __future__ import annotations

import argparse
import json
import asyncio
import os
import sys
import time
from contextlib import suppress
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout

from .browser import (audit_messages, download_group, open_group, wait_for_login,
                       DownloadTransportUnavailable)
from .diagnostics import detail, failure, log_location, start_log
from .errors import LocalDataError
from .group_menu import GROUPS_FILE, MODE_LABELS, choose_groups, load_groups, save_groups
from .extra_files import check_extra_files, loose_storage_files, storage_groups
from .layout_migration import migrate_group_layout
from .layout import group_id
from .local_config import load_paths
from .storage_review import inspect_group, finish_review


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    paths = load_paths()
    load_groups()
    parser = argparse.ArgumentParser(description='使用可见 Chrome 从 Telegram Web 下载群组媒体；无需 API 凭据')
    parser.add_argument('groups', nargs='*', help='Telegram Web K 群组链接；留空进入群组选择菜单')
    parser.add_argument('--output', type=Path, default=paths['output'], help='输出根目录')
    parser.add_argument('--limit', type=int, help='每组仅处理最新 N 条消息，用于调试')
    parser.add_argument('--dry-run', action='store_true', help='只扫描并显示目录计划')
    parser.add_argument('--check-extra-files', action='store_true',
                        help='检查已下载缺失和额外文件，不下载新内容；结束后统一询问是否补齐/归档')
    parser.add_argument('--migrate-standalone-layout', action='store_true',
                        help='给已有无 group 消息增加前一相册定位层；可配 --dry-run 先预览')
    parser.add_argument('--layout-plan', type=Path, help='按此前预览清单执行单群布局迁移')
    parser.add_argument('--audit-messages', type=int, metavar='N', help='每群核对最新至少 N 条原消息并写扫描记录，不下载')
    parser.add_argument('--full-scan', action='store_true', help='忽略扫描断点，从最新端重新扫描完整历史')
    parser.add_argument('--rebuild-local', action='store_true',
                        help=argparse.SUPPRESS)
    parser.add_argument('--verify', choices=('fast', 'exact'), default='fast',
                        help='已有文件校验：fast 按消息编组标记快速跳过（默认），exact 逐文件重新计算 SHA-256')
    parser.add_argument('--debug-window', choices=('hidden', 'visible'), default='visible',
                        help='调试窗口：hidden 无可见 Chrome 窗口，visible 有可见窗口（正常 BAT 默认）')
    parser.add_argument('--login-only', action='store_true', help='只打开 Chrome 供首次登录')
    parser.add_argument('--pause-at-oldest', action='store_true', help=argparse.SUPPRESS)
    return parser.parse_args(argv)


async def run(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    if args.limit is not None and args.limit < 1:
        raise ValueError('--limit 必须大于 0')
    if args.audit_messages is not None and args.audit_messages < 1:
        raise ValueError('--audit-messages 必须大于 0')
    if args.audit_messages is not None and (args.limit is not None or args.dry_run or args.full_scan):
        raise ValueError('--audit-messages 不可与 --limit、--dry-run、--full-scan 同用')
    if args.full_scan and args.limit is not None:
        raise ValueError('--full-scan 不可与 --limit 同时使用')
    if getattr(args, 'rebuild_local', False) and (args.full_scan or args.dry_run or args.audit_messages is not None):
        raise ValueError('--rebuild-local 不可与 --full-scan、--dry-run、--audit-messages 同用')
    if getattr(args, 'rebuild_local', False):
        raise ValueError('--rebuild-local 的旧目录迁移已停用；当前运行自动从 ~local 逐个复用媒体文件')
    if getattr(args, 'pause_at_oldest', False):
        raise ValueError('--pause-at-oldest 的旧网页暂停流程已停用；下载扫描使用服务器原始消息')
    if getattr(args, 'check_extra_files', False):
        validate_extra_options(args)
    migrating = getattr(args, 'migrate_standalone_layout', False)
    layout_plan = getattr(args, 'layout_plan', None)
    if layout_plan is not None and not migrating:
        raise ValueError('--layout-plan 只能与 --migrate-standalone-layout 同用')
    if migrating:
        if (args.limit is not None or args.audit_messages is not None or args.full_scan or
                args.login_only or getattr(args, 'check_extra_files', False)):
            raise ValueError('--migrate-standalone-layout 不可与限额、消息核验、全扫描、仅登录或额外文件检查同用')
        if layout_plan is not None:
            peer = json.loads(layout_plan.read_text(encoding='utf-8'))['peer']
            args.groups = [f'https://web.telegram.org/k/#{peer}']
        elif not args.groups:
            args.groups = [item.url for item in load_groups()]
    for url in args.groups:
        group_id(url)
    hidden = getattr(args, 'debug_window', 'visible') == 'hidden'
    if args.login_only and hidden:
        raise ValueError('首次登录需要 --debug-window visible；无可见窗口模式不能交互登录')
    profile = load_paths()['chrome_profile']
    profile.mkdir(parents=True, exist_ok=True)
    print(f'[Chrome] {"无可见窗口调试" if hidden else "启动可见窗口"}，打开 Telegram Web。', flush=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), channel='chrome', headless=hidden,
            accept_downloads=True, viewport=None,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        failures = total_messages = total_downloaded = total_existing = total_extra = 0
        task_started = time.monotonic()
        current_verify = 'extras' if getattr(args, 'check_extra_files', False) else getattr(args, 'verify', 'fast')

        def change_verify(mode: str) -> None:
            nonlocal current_verify
            current_verify = mode

        try:
            await page.goto('https://web.telegram.org/k/', wait_until='domcontentloaded')
            try:
                await wait_for_login(page, timeout=15_000 if hidden else 300_000)
            except BrowserTimeout:
                if hidden:
                    print('[登录未就绪] 无可见窗口模式不自动开窗；'
                          '请使用 --debug-window visible 检查登录后重试。', flush=True)
                raise
            print('[准备] 环境就绪：Chrome 已打开，Telegram 已登录。', flush=True)
            if args.login_only:
                print('按 Enter 关闭 Chrome。', flush=True)
                await asyncio.to_thread(input)
                return 0
            if not args.groups:
                configured = load_groups()
                names: dict[str, str] = {}
                for number, item in enumerate(configured, 1):
                    print(f'[准备] 正在读取群组名称 {number}/{len(configured)}', flush=True)
                    try:
                        names[item.identity] = await open_group(page, item.url, require_message=False)
                    except Exception as exc:
                        detail(f'群名读取失败 {item.identity}: {type(exc).__name__}: {exc}')
                        print('  一个群组的名称暂不可用，菜单将显示编号。', flush=True)
            while True:
                if args.groups:
                    groups = args.groups
                else:
                    groups = choose_groups(configured, lambda updated: save_groups(GROUPS_FILE, updated),
                                           names=names, verify_mode=current_verify,
                                           on_verify_change=change_verify)
                if groups is None:
                    print('已退出，正在关闭 Chrome。', flush=True)
                    return 1 if failures else 0
                reviewing = current_verify in ('exact','extras') and not migrating and args.audit_messages is None and not args.dry_run
                existing_reviews = {}
                download_failures = {}
                if current_verify == 'extras':
                    validate_extra_options(args)
                if reviewing:
                    staging, partials = extra_storage_paths(args.output)
                    configured_ids = {item.identity for item in load_groups()}
                    selected_ids = {group_id(url) for url in groups}
                    if selected_ids == configured_ids:
                        extra_ids = storage_groups(args.output, staging, partials) - selected_ids
                        groups = [*groups, *(f'https://web.telegram.org/k/#{identity}' for identity in sorted(extra_ids))]
                print(f'[任务] 已选群组 {len(groups)} 个 · '
                      f'{"无 group 目录迁移预览" if migrating and args.dry_run else "无 group 目录迁移" if migrating else MODE_LABELS[current_verify]}', flush=True)
                loose = []
                if current_verify != 'extras':
                    print(f'[保存到] {args.output.resolve()}', flush=True)
                if reviewing:
                    try:
                        loose = await asyncio.to_thread(loose_storage_files, args.output, staging, partials)
                        total_extra += len(loose)
                    except Exception as exc:
                        failures += 1
                        failure('额外文件检查：下载根目录盘点失败')
                        print(f'[下载根目录检查失败] {exc}', file=sys.stderr, flush=True)
                for group_number, url in enumerate(groups, 1):
                    group_started = time.monotonic()
                    print(f'\n[群组 {group_number}/{len(groups)}] 正在打开', flush=True)
                    try:
                        if migrating:
                            paths = load_paths()
                            if args.output.resolve() != paths['output']:
                                raise ValueError('布局迁移仅操作本机配置指向的数据目录')
                            options = {'apply': not args.dry_run}
                            if layout_plan is not None:
                                options['preview'] = layout_plan
                            count, report = await migrate_group_layout(page, url, paths, **options)
                            print(f'[{"迁移预览" if args.dry_run else "迁移完成"}] {count} 个文件 · 清单：{report}', flush=True)
                            continue
                        if current_verify == 'extras':
                            # 与逐文件下载共用检查；这里不扫描/保存新增消息。
                            existing_reviews[url] = await inspect_group(page, url, args.output, staging, partials)
                            continue
                        if args.audit_messages is not None:
                            bubbles, sources = await audit_messages(page, url, args.audit_messages)
                            total_messages += sources
                            detail(f'群组 {group_id(url)} 核验页面消息块 {bubbles}，原消息 {sources}')
                            print(f'[群组 {group_number}/{len(groups)} 完成] '
                                  f'已核对 {sources} 条消息，未下载', flush=True)
                            continue
                        on_group_open = None
                        if not args.groups:
                            on_group_open = lambda title, identity=group_id(url): names.__setitem__(identity, title)
                        if reviewing:
                            existing_reviews[url] = await inspect_group(page, url, args.output, staging, partials)
                        count, done, skipped, failed = await download_group(
                            page, url, args.output, args.limit, args.dry_run,
                            on_group_open, args.full_scan, getattr(args, 'pause_at_oldest', False),
                            current_verify == 'exact',
                            current_verify in ('fast','exact'),
                            getattr(args, 'rebuild_local', False),
                            reviewed=reviewing,
                        )
                        failures += failed
                        download_failures[url] = failed
                        total_messages += count
                        total_downloaded += done
                        total_existing += skipped
                        detail(f'GROUP_RESULT index={group_number} messages={count} '
                               f'downloaded={done} existing={skipped} failed={failed}')
                        print(f'[群组 {group_number}/{len(groups)} 完成] 已处理原消息 {count}，'
                              f'新下载 {done}，已有 {skipped}，失败 {failed}，'
                              f'耗时 {time.monotonic() - group_started:.1f} 秒', flush=True)
                    except Exception as exc:
                        failures += 1
                        download_failures[url] = download_failures.get(url,0)+1
                        report_group_error(exc, group_number, len(groups), url)
                if reviewing:
                    # 新下载可能改变记录/目录，流程结束后重新生成待确认清单。
                    print('[获取与检查流程完成] 正在汇总最终缺失和额外文件清单',flush=True)
                    reviews = []
                    complete_review = True
                    for url in groups:
                        if url not in existing_reviews and download_failures.get(url):
                            # 初次核验已失败，未开始下载；不再完整重读同一批媒体。
                            complete_review=False
                            continue
                        try:
                            prior = existing_reviews.get(url)
                            reviews.append(prior if current_verify == 'extras' and prior is not None else
                                           await inspect_group(page, url, args.output, staging, partials,previous=prior))
                        except Exception as exc:
                            complete_review = False
                            failures += 1
                            report_group_error(exc, groups.index(url)+1, len(groups), url)
                    if complete_review:
                        try:
                            loose = await asyncio.to_thread(loose_storage_files,args.output,staging,partials)
                            repair_failures, changed = await finish_review(page,reviews,loose)
                            failures += repair_failures
                            if changed and not repair_failures and current_verify == 'exact':
                                for review in reviews:
                                    retired_known = any(path in review.expected for path in review.extras)
                                    if not retired_known and not download_failures.get(review.url):
                                        continue
                                    print('[继续下载] 修正后继续本群未完成的新内容',flush=True)
                                    await inspect_group(page,review.url,args.output,staging,partials)
                                    count,done,skipped,failed = await download_group(
                                        page,review.url,args.output,args.limit,False,
                                        exact_verify=True,coarse_skip=True,reviewed=True)
                                    failures += failed-download_failures.get(review.url,0)
                                    total_messages += count
                                    total_downloaded += done
                                    total_existing += skipped
                        except Exception as exc:
                            failures += 1
                            print(f'[修正失败] {exc}；现场及已完成操作保留',file=sys.stderr,flush=True)
                    else:
                        print('[检查未完成] 不按不完整清单执行修正',flush=True)
                if migrating:
                    print(f'\n[布局迁移] 失败 {failures}；未下载附件，未覆盖现有文件', flush=True)
                elif current_verify == 'extras':
                    print(f'\n[本次检查] 检查/修正失败 {failures}；未下载新增消息或附件', flush=True)
                elif args.audit_messages is not None:
                    print(f'\n[本次总计] 已核对原消息 {total_messages}，失败 {failures}，未下载', flush=True)
                else:
                    print(f'\n[本次总计] 已处理原消息 {total_messages}，新下载 {total_downloaded}，'
                          f'已有 {total_existing}，失败 {failures}，'
                          f'耗时 {time.monotonic() - task_started:.1f} 秒', flush=True)
                if args.groups:
                    return 1 if failures else 0
                print('[菜单] 本次群组获取已结束，返回主菜单。', flush=True)
        finally:
            # Ctrl+C 可能先关闭 Playwright 驱动；清理报错不能掩盖原本的中断状态。
            with suppress(Exception):
                await context.close()


def extra_storage_paths(output: Path) -> tuple[Path, Path]:
    """成品、完整 downloading 与配置分片根使用同一套路径约定。"""
    paths = load_paths()
    if output.resolve() == paths['output']:
        return paths['staging'], paths['partials']
    base = output.resolve()
    return (base.parent / f'.{base.name}.telegg-staging',
            base.parent / f'.{base.name}.telegg-partials')


def validate_extra_options(args: argparse.Namespace) -> None:
    """检查必须覆盖本地涉及范围，拒绝会使其混入下载或截断范围的参数。"""
    if (args.limit is not None or args.audit_messages is not None or args.dry_run or
            args.full_scan or args.login_only):
        raise ValueError('--check-extra-files / 检查额外文件模式不可与 '
                         '--limit、--audit-messages、--dry-run、--full-scan、--login-only 同用')


def report_group_error(exc: Exception, number: int, total: int, url: str) -> None:
    failure(f'群组 {number}/{total} {group_id(url)} 失败')
    if isinstance(exc, LocalDataError):
        print(f'[本地数据错误] {exc}；已停止本群，未自动修复。', file=sys.stderr, flush=True)
    elif isinstance(exc, DownloadTransportUnavailable):
        print(f'[下载连接中断] {exc}', file=sys.stderr, flush=True)
    elif isinstance(exc, BrowserTimeout):
        print('[页面超时] Telegram 页面或目标群组未能及时就绪；本群已停止，已有文件和断点保留。',
              file=sys.stderr, flush=True)
    print(f'[群组 {number}/{total} 失败] 处理未完成，详情见 {log_location()}',
          file=sys.stderr, flush=True)


def run_exit_status() -> tuple[int, str]:
    """记录所有可处理的退出；窗口被强制结束时不伪造正常结束记录。"""
    try:
        code = asyncio.run(run())
        return code, 'incomplete' if code else 'completed'
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else int(bool(exc.code))
        return code, 'cli'
    except (KeyboardInterrupt, EOFError):
        print('已中断', file=sys.stderr)
        return 130, 'interrupted'
    except Exception:
        failure('启动失败')
        print(f'启动失败，请查看 {log_location()}', file=sys.stderr)
        return 1, 'error'


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    start_log()
    detail(f'RUN_START pid={os.getpid()}')
    code, reason = run_exit_status()
    detail(f'RUN_EXIT pid={os.getpid()} code={code} reason={reason}')
    raise SystemExit(code)


if __name__ == '__main__':
    main()
