"""顺序运行隔离副本中的实际 BAT；保留私有完整日志，只输出状态。"""
import os
import re
import subprocess
import time


def run_bat(runtime, log, peer, mode, allowed, timeout=240, *, debug_window='hidden', limit=1):
    before = set((runtime / '~temp/logs').glob('telegg-*.log'))
    started = time.monotonic()
    stopped = None
    first_transfer = first_network = None
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    with log.open('wb') as stream:
        process = subprocess.Popen(['cmd', '/d', '/c', 'download.bat',
            f'https://web.telegram.org/k/#{peer}', '--limit', str(limit), '--verify', mode,
            '--debug-window', debug_window],
            cwd=runtime, env=env, stdin=subprocess.PIPE, stdout=stream, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if debug_window == 'hidden' else 0)
        process.stdin.write(b'\n')
        process.stdin.close()
        while process.poll() is None:
            time.sleep(.25)
            text = log.read_text(encoding='utf-8', errors='replace')
            elapsed = time.monotonic() - started
            if first_transfer is None and '[传输]' in text:
                first_transfer = round(elapsed, 2)
            if first_network is None and ('[下载] 新资源已开始' in text or '[下载] 续传已开始' in text):
                first_network = round(elapsed, 2)
            ids = {m for line in text.splitlines() if '[传输]' in line
                   for m in re.findall(r'\bm(\d+)', line)}
            if ids - allowed:
                stopped = 'source_scope_changed'
            if time.monotonic() - started > timeout:
                stopped = 'timeout'
            if stopped:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
        code = process.wait()
    fresh = set((runtime / '~temp/logs').glob('telegg-*.log')) - before
    markers = [line for file in fresh for line in file.read_text(encoding='utf-8').splitlines()
               if 'RUN_EXIT' in line]
    return {'exit': code, 'seconds': round(time.monotonic() - started, 2), 'stop': stopped,
            'first_transfer_seconds': first_transfer, 'first_network_seconds': first_network,
            'debug_window': debug_window,
            'normal_exit_logged': bool(markers), '_console': log.read_text(encoding='utf-8', errors='replace')}
