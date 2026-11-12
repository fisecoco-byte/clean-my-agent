#!/usr/bin/env python3
"""Exercise the built Rust terminal application in an isolated real PTY."""
import codecs
import errno
import fcntl
import importlib.util
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import pyte
from terminal_svg import save_svg

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('parity',REPO/'dev/rust-parity.py')
parity=importlib.util.module_from_spec(spec);spec.loader.exec_module(parity)


def main():
    binary=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else REPO/'target/debug/clean-my-agent'
    evidence=REPO/'build/rust-migration/ui';evidence.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cma-rust-ui-') as temp:
        fixture=parity.Fixtures(Path(temp).resolve());fixture.sessions();fixture.artifacts();old_log=fixture.file(".codex/log/ui-clean.log")
        second=fixture.home/'.codex/worktrees/second-workspace'
        fixture.file(second/'Cargo.toml','[package]\nname="fixture"\nversion="0.1.0"\n')
        fixture.file(second/'.gitignore','target/\n')
        fixture.file(second/'target/generated')
        fixture.old(second/'target')
        fixture.git(second,'init','-q');fixture.git(second,'add','Cargo.toml','.gitignore')
        fixture.git(second,'-c','user.name=fixture','-c','user.email=test@example.invalid','-c','commit.gpgsign=false','commit','-qm','fixture')
        master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',40,140,0,0))
        terminal_env = dict(fixture.env, TERM='xterm-256color', COLORTERM='truecolor')
        terminal_env.pop('NO_COLOR', None)
        process=subprocess.Popen([str(binary),'--agent','codex'],stdin=slave,stdout=slave,stderr=slave,cwd=REPO,
                                 env=terminal_env,start_new_session=True)
        os.close(slave);screen=pyte.Screen(140,40);stream=pyte.Stream(screen);decoder=codecs.getincrementaldecoder('utf8')('replace')
        history=[];raw=bytearray()
        def display():
            return '\n'.join(''.join(screen.buffer[y][x].data for x in range(screen.columns)) for y in range(screen.lines))
        def pump(timeout=0.1):
            if select.select([master],[],[],timeout)[0]:
                try: chunk=os.read(master,1024*1024)
                except OSError as exc:
                    if exc.errno==errno.EIO:return
                    raise
                raw.extend(chunk);stream.feed(decoder.decode(chunk));history.append(display())
        def wait(predicate,timeout=25):
            started=time.monotonic()
            while time.monotonic()-started<timeout:
                pump()
                if history and predicate(history[-1]):return history[-1]
                if process.poll() is not None:raise AssertionError(f'program exited {process.returncode}: '+raw.decode(errors='replace')[-2000:])
            raise AssertionError('timed out:\n'+history[-1])
        def send(value):os.write(master,value.encode())
        def capture(name):
            (evidence/f'{name}.txt').write_text(display())
            save_svg(screen, evidence/f'{name}.svg')
        try:
            wait(lambda s:'工作树与依赖' in s);capture('home')
            assert any('正在扫描' in frame for frame in history),'home did not draw loading'
            start=len(history);send('\x1b[B\r')
            wait(lambda s:'tree é with space' in s and '后台扫描中' not in s);capture('workspaces')
            assert any('正在扫描' in frame for frame in history[start:]),'workspaces did not draw loading'
            assert 't 清理产物' in display() and 'd 清理整个目录' in display(),'missing workspace cleanup actions'
            send(' ');wait(lambda s:'已选 1' in s and '☑' in s);capture('workspace-selected')
            send('a');wait(lambda s:'已选 2' in s);send('d')
            wait(lambda s:'整目录清理每次核对一个' in s);capture('workspace-batch-directory-guard')
            send('\x1b');wait(lambda s:'整目录清理每次核对一个' not in s)
            send('t');wait(lambda s:'项目 / 资源' in s and '范围：2 个工作目录' in s);capture('workspace-artifact-cleanup')
            assert '已选 0' in display(),'workspace selection leaked into artifact selection'
            send('w');wait(lambda s:'工作目录 · Enter' in s)
            send('/tree é with space\r ');wait(lambda s:'已勾选 1 个目录' in s and '☑' in s)
            send('d');wait(lambda s:'工作目录核对结果' in s and '保留原因' in s);capture('workspace-retained-reason')
            assert (second/'target/generated').exists(),'workspace review changed files'
            send('\x1b');wait(lambda s:'工作目录核对结果' not in s)
            send('p');wait(lambda s:'target' in s and '项目 / 资源' in s);capture('artifacts')
            send('/target\r');wait(lambda s:'搜索：target' in s);capture('search')
            send('\x1b');wait(lambda s:'工作目录 · Enter' in s)
            send('q');wait(lambda s:'工作树与依赖' in s)
            send('g\r');wait(lambda s:'会话管理' in s and '后台扫描中' not in s and '归档≥10天' in s.replace(' ',''))
            wait(lambda s:'用户名称' in s or '标题' in s or '复核' in s);capture('sessions')
            send('i');wait(lambda s:'会话活跃分布' in s);capture('session-distribution')
            send('\x1b');wait(lambda s:'会话活跃分布' not in s)
            send('q');wait(lambda s:'工作树与依赖' in s)
            send('\x1b[B\r');wait(lambda s:'工作树 / 多语言产物' in s)
            send('q');wait(lambda s:'工作树与依赖' in s);capture('cancelled-back-home')
            send('gjj\r');wait(lambda s:'ui-clean.log' in s and '后台扫描中' not in s)
            start=len(history);send('q');wait(lambda s:'工作树与依赖' in s)
            home_scans=[frame for frame in history[start:] if 'Agent 存储总览' in frame and '正在扫描' in frame]
            assert not home_scans,'browsing caches rescanned the overview:\n'+'\n'.join(home_scans)
            capture('cached-home-after-browse')
            send('\r');wait(lambda s:'ui-clean.log' in s and '后台扫描中' not in s)
            send(' t');wait(lambda s:'确认暂存' in s and '输入 STAGE' in s)
            send('wrong\r');pump(0.2);assert old_log.exists(),'wrong confirmation changed file'
            send('\x1b');wait(lambda s:'确认暂存' not in s);assert old_log.exists()
            send('t');wait(lambda s:'确认暂存' in s);send('STAGE\r')
            wait(lambda s:'操作结果' in s and '暂存批次' in s);assert not old_log.exists();capture('stage-result')
            send('\x1b');wait(lambda s:'操作结果 · Esc 返回' not in s);send('v')
            wait(lambda s:'ui-clean.log' in s and '后台扫描中' not in s);capture('vault')
            send(' r');wait(lambda s:'确认恢复' in s and '输入 RESTORE' in s);send('RESTORE\r')
            wait(lambda s:'操作结果' in s and '已处理 1 项' in s);assert old_log.exists();capture('restore-result')
            send('\x1b');wait(lambda s:'操作结果 · Esc 返回' not in s)
            start=len(history);send('q');wait(lambda s:'工作树与依赖' in s)
            assert any('正在扫描' in frame for frame in history[start:]),'file operations did not refresh the overview'
            send('gjj\r');wait(lambda s:'ui-clean.log' in s and '后台扫描中' not in s);send(' t');wait(lambda s:'确认暂存' in s);send('STAGE\r');wait(lambda s:'暂存批次' in s)
            send('\x1b');wait(lambda s:'操作结果 · Esc 返回' not in s);send('v');wait(lambda s:'ui-clean.log' in s and '后台扫描中' not in s)
            send(' d');wait(lambda s:'确认永久清除' in s and '输入 DELETE' in s);send('DELETE\r');wait(lambda s:'操作结果' in s and '已处理 1 项' in s)
            assert not old_log.exists();assert not list((fixture.home/'state/clean-my-agent').glob('*/manifest.json'));capture('purge-result')
            send('\x1b');wait(lambda s:'操作结果 · Esc 返回' not in s);send('q');wait(lambda s:'工作树与依赖' in s)
            send('q')
            deadline=time.monotonic()+5
            while process.poll() is None and time.monotonic()<deadline:pump()
            process.wait(timeout=1)
            assert process.returncode==0,process.returncode
            print('PASS real PTY: loading, workspace checkboxes and cleanup actions, multi-workspace artifact scope, directory cleanup guard and retained reasons, cached overview on return with cursor preserved, refresh after file operations, artifact filter, sessions, cancel, confirmations, stage, restore, purge, exit')
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=5)
            os.close(master);(evidence/'terminal.ansi').write_bytes(raw)


if __name__=='__main__':main()
