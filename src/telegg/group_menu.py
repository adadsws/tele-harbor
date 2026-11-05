"""CMD 数字菜单与群组号清单维护；群名只在打开 Telegram 后读取。"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .diagnostics import detail
from .layout import group_id

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GROUPS_FILE = PROJECT_ROOT / 'config' / 'config.local.json'
EXIT_INPUTS = {'q', 'quit', 'exit'}


@dataclass(frozen=True, slots=True)
class ConfiguredGroup:
    identity: str

    @property
    def url(self) -> str:
        return f'https://web.telegram.org/k/#{self.identity}'


def checked_url(value: str) -> str:
    url = value.strip()
    if urlsplit(url).scheme != 'https':
        raise ValueError('群组链接必须使用 https')
    group_id(url)
    return url


def checked_identity(value: str) -> str:
    """菜单接受群组号或完整链接，配置始终只存群组号。"""
    text = value.strip()
    if re.fullmatch(r'-\d+', text):
        return text
    return group_id(checked_url(text))


def parse_groups(value: object) -> list[ConfiguredGroup]:
    """严格校验配置，避免坏配置使菜单下载错误群组。"""
    if not isinstance(value, list):
        raise ValueError('群组配置必须是列表')
    groups: list[ConfiguredGroup] = []
    seen: set[str] = set()
    for position, item in enumerate(value, 1):
        if isinstance(item, str):
            identity = item.strip()
            if not re.fullmatch(r'-\d+', identity):
                raise ValueError(f'群组配置第 {position} 项须为群组号')
        elif isinstance(item, dict) and isinstance(item.get('url'), str):
            # 只读兼容旧配置；下一次保存会写成仅含群组号的格式。
            identity = group_id(checked_url(item['url']))
        else:
            raise ValueError(f'群组配置第 {position} 项须为群组号')
        if identity in seen:
            raise ValueError(f'群组配置重复：{identity}')
        seen.add(identity)
        groups.append(ConfiguredGroup(identity))
    return groups


def load_groups(path: Path = GROUPS_FILE) -> list[ConfiguredGroup]:
    if not path.is_file():
        raise FileNotFoundError(f'找不到群组配置：{path}')
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('本地配置必须是 JSON 对象')
    return parse_groups(config.get('groups'))


def save_groups(path: Path, groups: list[ConfiguredGroup]) -> None:
    """每次修改前归档原配置；移除群组只改清单，不触碰下载文件。"""
    parse_groups([item.identity for item in groups])
    project = PROJECT_ROOT.resolve()
    actual = path.resolve()
    if not actual.is_relative_to(project) or not actual.is_file():
        raise ValueError(f'仅允许修改项目内已有群组配置：{path}')
    batch = project / '~archive' / f'{datetime.now():%Y-%m-%d-%H%M%S}-groups-config-{uuid.uuid4().hex[:8]}'
    archived = batch / actual.relative_to(project)
    archived.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(actual, archived)
    payload = json.loads(actual.read_text(encoding='utf-8-sig'))
    if not isinstance(payload, dict):
        raise ValueError('本地配置必须是 JSON 对象')
    payload['groups'] = [item.identity for item in groups]
    actual.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    detail(f'群组清单修改前配置已归档：{archived}')
    print('群组清单已保存。', flush=True)


def choose_groups(groups: list[ConfiguredGroup], save: Callable[[list[ConfiguredGroup]], None],
                  read: Callable[[str], str] = input,
                  names: dict[str, str] | None = None) -> list[str] | None:
    """参考 leak 项目的数字循环输入：单组、全部、增加、移除和退出。"""
    def answer(prompt: str) -> str:
        try:
            return read(prompt).strip()
        except EOFError as exc:
            raise ValueError('没有可用的 CMD 输入；请交互运行 BAT，或直接传入群组链接') from exc

    names = names or {}

    def label(identity: str) -> str:
        name = ' '.join(names.get(identity, '').split())
        return name if name and name != identity else f'群组 {identity}'

    while True:
        print('\n已配置的 Telegram 群组：', flush=True)
        for number, item in enumerate(groups, 1):
            print(f'  {number}. 下载 {label(item.identity)}', flush=True)
        if groups:
            all_number, add_number, remove_number = len(groups) + 1, len(groups) + 2, len(groups) + 3
            print(f'  {all_number}. 下载全部群组', flush=True)
        else:
            add_number, remove_number = 1, None
            print('  暂无群组', flush=True)
        print(f'  {add_number}. 新增群组', flush=True)
        if remove_number is not None:
            print(f'  {remove_number}. 移除群组', flush=True)
        print('  0. 退出', flush=True)
        choice = answer('请输入数字选择：')
        if choice == '0' or choice.casefold() in EXIT_INPUTS:
            return None
        if choice.isdecimal() and 1 <= int(choice) <= len(groups):
            selected = groups[int(choice) - 1]
            print(f'已选择：{label(selected.identity)}', flush=True)
            return [selected.url]
        if groups and choice == str(all_number):
            print(f'已选择全部 {len(groups)} 个群组', flush=True)
            return [item.url for item in groups]
        if choice == str(add_number):
            while True:
                value = answer('输入群组号或 Telegram Web K 链接（0 返回，q 退出）：')
                if value == '0':
                    break
                if value.casefold() in EXIT_INPUTS:
                    return None
                try:
                    identity = checked_identity(value)
                except ValueError as exc:
                    print(f'群组号或链接无效：{exc}', flush=True)
                    continue
                if any(item.identity == identity for item in groups):
                    print('该群组已在清单中，请输入其他链接。', flush=True)
                    continue
                updated = [*groups, ConfiguredGroup(identity)]
                save(updated)
                groups[:] = updated
                print('已新增群组；打开群组后才读取实际群名。', flush=True)
                break
            continue
        if remove_number is not None and choice == str(remove_number):
            while True:
                for number, item in enumerate(groups, 1):
                    print(f'  {number}. {label(item.identity)}', flush=True)
                target = answer('输入要移除的群组编号（0 返回）：')
                if target == '0':
                    break
                if target.isdecimal() and 1 <= int(target) <= len(groups):
                    removed = groups[int(target) - 1]
                    updated = [item for item in groups if item != removed]
                    save(updated)
                    groups[:] = updated
                    print('已从清单移除群组；已有下载文件保留。', flush=True)
                    break
                print(f'请输入 1–{len(groups)}，或输入 0 返回。', flush=True)
            continue
        print('请输入菜单中的数字，或输入 0 退出。', flush=True)
