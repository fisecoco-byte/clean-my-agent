#!/usr/bin/env python3
"""Observe real workspace discovery in a read-only PTY; record counts, never paths."""
import argparse
import codecs
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time
import pyte

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('binary', type=Path)
parser.add_argument('--seconds', type=float, default=120)
args = parser.parse_args()
master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 140, 0, 0))
process = subprocess.Popen(
    [str(args.binary.resolve()), 'deps', '--agent', 'codex', '--tui', '--dry-run'],
    stdin=slave, stdout=slave, stderr=slave,
    env=dict(os.environ, TERM='xterm-256color'), start_new_session=True,
)
os.close(slave)
screen = pyte.Screen(140, 40)
stream = pyte.Stream(screen)
decoder = codecs.getincrementaldecoder('utf8')('replace')
started = time.monotonic()
first_loading = None
row_count = 0
visited = 0
try:
    while time.monotonic() - started < args.seconds and process.poll() is None:
        if not select.select([master], [], [], .1)[0]:
            continue
        try:
            stream.feed(decoder.decode(os.read(master, 1024 * 1024)))
        except OSError as error:
            if error.errno == errno.EIO:
                break
            raise
        display = '\n'.join(screen.display)
        if first_loading is None and '正在扫描' in display:
            first_loading = round(time.monotonic() - started, 3)
        for count in re.findall(r'(\d+) 项', display):
            visited = max(visited, int(count))
        match = re.search(r'(\d+) 项 · 已选', display)
        if match and '目录占用' in display:
            row_count = int(match.group(1))
        if row_count:
            break
    print(json.dumps({
        'scope': 'actual Codex workspace discovery, --dry-run',
        'loading_seconds': first_loading, 'workspace_count': row_count,
        'preview_observed': row_count > 0,
        'elapsed_seconds': round(time.monotonic() - started, 3),
        'maximum_visible_entry_counter': visited,
    }, ensure_ascii=False, indent=2), flush=True)
    assert first_loading is not None, 'Loading not observed'
finally:
    if process.poll() is None:
        os.write(master, b'qq')
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
    os.close(master)
