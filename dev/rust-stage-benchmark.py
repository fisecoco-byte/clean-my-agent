#!/usr/bin/env python3
"""Compare batch staging in a real PTY using only an isolated temporary HOME."""
import argparse
import codecs
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import time

import pyte

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('parity', REPO / 'dev/rust-parity.py')
parity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parity)


def stage(binary, fixture, targets):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 160, 0, 0))
    process = subprocess.Popen([str(binary), 'deps', '--tui', '--agent', 'codex'],
                               stdin=slave, stdout=slave, stderr=slave, cwd=REPO,
                               env=dict(fixture.env, TERM='xterm-256color'),
                               start_new_session=True)
    os.close(slave)
    screen = pyte.Screen(160, 40)
    stream = pyte.Stream(screen)
    decoder = codecs.getincrementaldecoder('utf8')('replace')
    frames = []

    def display():
        return '\n'.join(''.join(screen.buffer[y][x].data for x in range(screen.columns))
                         for y in range(screen.lines))

    def wait(predicate, timeout=30):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            if select.select([master], [], [], 0.05)[0]:
                stream.feed(decoder.decode(os.read(master, 1024 * 1024)))
                frame = display()
                frames.append(frame)
                if predicate(frame):
                    return frame
            if process.poll() is not None:
                raise AssertionError(f'Premature exit: {process.returncode}')
        raise AssertionError('Timed out:\n' + display())

    def send(keys):
        os.write(master, keys.encode())

    try:
        wait(lambda s: 'batch' in s and '后台扫描中' not in s and '工作目录 · Enter' in s)
        send('p')
        wait(lambda s: '项目 / 资源' in s)
        send('/selected-\r')
        wait(lambda s: '搜索：selected-' in s)
        send('a')
        wait(lambda s: '☑' in s)
        send('t')
        prompt = wait(lambda s: '输入 STAGE' in s)
        assert f'将暂存 {len(targets)} 项' in prompt, prompt
        started = time.perf_counter()
        send('STAGE\r')
        result = wait(lambda s: '操作结果' in s and '暂存批次' in s, timeout=180)
        elapsed = time.perf_counter() - started
        assert f'已处理 {len(targets)} 项' in result, result
        assert all(not p.exists() for p in targets)
        manifests = list((fixture.home / 'state/clean-my-agent').glob('*/manifest.json'))
        assert len(manifests) == 1, manifests
        progress_visible = any('暂存 1/' in f for f in frames)
        # The operation has completed; stop only this isolated fixture process
        # before its automatic post-action rescan, which is outside the timing.
        process.terminate()
        process.wait(timeout=10)
        restored = subprocess.run([str(binary), 'restore', manifests[0].parent.name, '--json'],
                                  env=fixture.env, cwd=REPO, capture_output=True, text=True, timeout=60)
        assert restored.returncode == 0, restored.stdout + restored.stderr
        assert all((p / 'generated').read_text() == 'fixture' for p in targets)
        return elapsed, progress_visible
    finally:
        if sys.exc_info()[0] is not None:
            print(display(), file=sys.stderr)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        os.close(master)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--items', type=int, default=8)
    parser.add_argument('--files', type=int, default=20000)
    parser.add_argument('--runs', type=int, default=3)
    args = parser.parse_args()
    if min(args.items, args.files, args.runs) < 1:
        parser.error('items, files and runs must be positive')
    binaries = {'before': args.before.resolve(), 'after': args.after.resolve()}
    with tempfile.TemporaryDirectory(prefix='cma-stage-benchmark-') as temp:
        fixture = parity.Fixtures(Path(temp).resolve())
        bin_dir = fixture.home / 'bin'
        bin_dir.mkdir()
        for name, script in {
            'lsof': "#!/bin/sh\nprintf 'p99999999\\0fcwd\\0n/nonexistent-fixture\\0\\n'\n",
            'ps': "#!/bin/sh\nprintf '99999999 fixture-process\\n'\n",
        }.items():
            path = bin_dir / name
            path.write_text(script)
            path.chmod(0o700)
        fixture.env['PATH'] = str(bin_dir) + os.pathsep + fixture.env.get('PATH', '')
        repo = fixture.home / '.codex/worktrees/batch'
        fixture.file(repo / '.gitignore', 'target/\n')
        targets = []
        for n in range(args.items):
            project = repo / f'selected-{n}'
            fixture.file(project / 'Cargo.toml', '[package]\nname="fixture"\nversion="0.1.0"\n')
            fixture.file(project / 'target/generated')
            fixture.old(project / 'target')
            targets.append(project / 'target')
        unrelated = repo / 'unrelated'
        fixture.file(unrelated / 'Cargo.toml', '[package]\nname="unrelated"\nversion="0.1.0"\n')
        for n in range(args.files):
            fixture.file(unrelated / f'target/group-{n // 100}/file-{n}')
        fixture.old(unrelated / 'target')
        fixture.git(repo, 'init', '-q')
        fixture.git(repo, 'add', '.')
        fixture.git(repo, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
        timings = {name: [] for name in binaries}
        for trial in range(args.runs + 1):
            for name in list(binaries)[::1 if trial % 2 == 0 else -1]:
                elapsed, progress_visible = stage(binaries[name], fixture, targets)
                if name == 'after':
                    assert progress_visible, 'Missing batch action progress'
                if trial:
                    timings[name].append(round(elapsed, 4))
        print(json.dumps({
            'selected_items': args.items, 'unselected_artifact_files': args.files,
            'runs': args.runs, 'warm_cache': True,
            'timing_scope': 'confirmation to completed staging result in a real PTY',
            'process_checks': 'identical isolated stubs', 'stage_and_restore_verified': True,
            'results': {name: {'median_seconds': round(statistics.median(values), 4),
                               'runs_seconds': values} for name, values in timings.items()},
        }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
