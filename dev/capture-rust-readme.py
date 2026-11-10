#!/usr/bin/env python3
"""Capture the native Rust UI using the isolated PTY regression fixtures."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
binary = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT/'target/release/clean-my-agent'
subprocess.run([sys.executable, str(ROOT/'dev/rust-ui-smoke.py'), str(binary)],
               env=dict(os.environ, TMPDIR='/tmp'), check=True)
version = json.loads(subprocess.check_output([str(binary), '--build-info']))['version']
lines = ['# Rust 界面截图 / Rust terminal screenshots', '',
         f'录制版本 / Version: `{version}` · {datetime.now(timezone.utc).isoformat(timespec="seconds")}', '',
         '从原生 Rust 程序的真实终端输出导出，保留终端网格与颜色。全部数据来自临时 HOME 内的虚构项目和会话，不扫描用户数据。',
         'Captured from the native Rust executable in a real PTY, preserving its grid and colors. All fixtures live in a temporary HOME.', '',
         '录制过程同时验证勾选、清理入口、暂存、恢复和永久清除；文件操作只针对临时测试数据。',
         'The recording also tests selection, cleanup actions, staging, restore and purge, exclusively on temporary fixtures.', '',
         '重新生成 / Regenerate (macOS / Linux):', '', '```sh',
         'cargo build --release --locked',
         'python3 -m venv .venv', '.venv/bin/python -m pip install pyte',
         '.venv/bin/python dev/capture-rust-readme.py', '```', '',
         'Python 和 pyte 仅用于开发录制，发布程序不需要它们。Python and pyte are capture tools, not runtime dependencies.', '',
         '| Screenshot | SHA-256 |', '| --- | --- |']
for source, name in [('home','dashboard'), ('workspace-selected','workspaces'),
                     ('workspace-artifact-cleanup','deps'), ('sessions','sessions')]:
    target = ROOT/f'docs/rust-{name}.svg'
    shutil.copy2(ROOT/f'build/rust-migration/ui/{source}.svg', target)
    lines.append(f'| [{name}]({target.name}) | `{hashlib.sha256(target.read_bytes()).hexdigest()}` |')
(ROOT/'docs/rust-screenshots.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
