"""BAT/CMD 入口：可见 Chrome 的 Telegram Web 批量下载。"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from contextlib import suppress
from pathlib import Path

from playwright.async_api import async_playwright

from .browser import audit_messages, download_group, open_group, wait_for_login
from .diagnostics import detail, failure, log_location, start_log
from .errors import LocalDataError
from .group_menu import GROUPS_FILE, choose_groups, load_groups, save_groups
from .layout import group_id
from .local_config import load_paths


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    paths = load_paths()
    load_groups()
    parser = argparse.ArgumentParser(description='使用可见 Chrome 从 Telegram Web 下载群组媒体；无需 API 凭据')
    parser.add_argument('groups', nargs='*', help='Telegram Web K 群组链接；留空进入群组选择菜单')
    parser.add_argument('--output', type=Path, default=paths['output'], help='输出根目录')
    parser.add_argument('--limit', type=int, help='每组仅处理最新 N 条消息，用于调试')
    parser.add_argument('--dry-run', action='store_true', help='只扫描并显示目录计划')
    parser.add_argument('--audit-messages', type=int, metavar='N', help='每群核对最新至少 N 条原消息并写扫描记录，不下载')
    parser.add_argument('--full-scan', action='store_true', help='忽略扫描断点，从最新端重新扫描完整历史')
    parser.add_argument('--rebuild-local', action='store_true',
                        help=argparse.SUPPRESS)
    parser.add_argument('--verify', choices=('fast', 'exact'), default='fast',
                        help='已有文件校验：fast 按消息编组标记快速跳过（默认），exact 逐文件重新计算 SHA-256')
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
    for url in args.groups:
        group_id(url)
    profile = load_paths()['chrome_profile']
    profile.mkdir(parents=True, exist_ok=True)
    print('[Chrome] 启动可见窗口，打开 Telegram Web。', flush=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), channel='chrome', headless=False,
            accept_downloads=True, viewport=None,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        failures = total_messages = total_downloaded = total_existing = 0
        task_started = time.monotonic()
        current_verify = getattr(args, 'verify', 'fast')

        def change_verify(mode: str) -> None:
            nonlocal current_verify
            current_verify = mode

        try:
            await page.goto('https://web.telegram.org/k/', wait_until='domcontentloaded')
            await wait_for_login(page)
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
                print(f'[任务] 已选群组 {len(groups)} 个 · '
                      f'{"快速模式" if current_verify == "fast" else "逐文件检查模式"}', flush=True)
                print(f'[保存到] {args.output.resolve()}', flush=True)
                for group_number, url in enumerate(groups, 1):
                    group_started = time.monotonic()
                    print(f'\n[群组 {group_number}/{len(groups)}] 正在打开', flush=True)
                    try:
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
                        count, done, skipped, failed = await download_group(
                            page, url, args.output, args.limit, args.dry_run,
                            on_group_open, args.full_scan, getattr(args, 'pause_at_oldest', False),
                            current_verify == 'exact',
                            current_verify == 'fast',
                            getattr(args, 'rebuild_local', False),
                        )
                        failures += failed
                        total_messages += count
                        total_downloaded += done
                        total_existing += skipped
                        print(f'[群组 {group_number}/{len(groups)} 完成] 已处理原消息 {count}，'
                              f'新下载 {done}，已有 {skipped}，失败 {failed}，'
                              f'耗时 {time.monotonic() - group_started:.1f} 秒', flush=True)
                    except Exception as exc:
                        failures += 1
                        report_group_error(exc, group_number, len(groups), url)
                if args.audit_messages is not None:
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


def report_group_error(exc: Exception, number: int, total: int, url: str) -> None:
    failure(f'群组 {number}/{total} {group_id(url)} 失败')
    if isinstance(exc, LocalDataError):
        print(f'[本地数据错误] {exc}；已停止本群，未自动修复。', file=sys.stderr, flush=True)
    print(f'[群组 {number}/{total} 失败] 处理未完成，详情见 {log_location()}',
          file=sys.stderr, flush=True)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    start_log()
    try:
        raise SystemExit(asyncio.run(run()))
    except (KeyboardInterrupt, EOFError):
        print('已中断', file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        failure('启动失败')
        print(f'启动失败，请查看 {log_location()}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
