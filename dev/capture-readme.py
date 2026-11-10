"""Record legacy Python GIFs and SVG stills with fictional data and no native actions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import html
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.pop('NO_COLOR', None)
os.environ['COLORTERM'] = 'truecolor'

from textual.widgets import Button, DataTable
from rich.cells import cell_len
from clean_my_agent import __version__, activity
from clean_my_agent.dashboard_tui import DashboardApp
from clean_my_agent.inventory import BUCKETS
from clean_my_agent.loading_tui import ScanLoading
from clean_my_agent.policy import DAY_NS
from clean_my_agent.session_tui import SessionApp
from clean_my_agent.storage_tui import StorageApp

OUT = ROOT / 'docs'
FRAMES = ROOT / 'reports' / 'readme-gif'
EXAMPLE_TIME = datetime(2026, 11, 10, 12, tzinfo=timezone(timedelta(hours=8)))
NOW_NS = int(EXAMPLE_TIME.timestamp()) * 10**9
GIB, MIB = 1024**3, 1024**2


class NoActions:
    agent = 'all'
    writes_enabled = True

    def selected(self, agent):
        return {'codex', 'claude', 't3'} if agent == 'all' else {agent}

    def forbidden(self, *args, **kwargs):
        raise AssertionError('Screenshot fixtures must not scan or change user data')

    apply = stage = stage_many = snapshot = quarantine = forbidden


class DemoScan(NoActions):
    """A bounded, synthetic scan; never inspect the host filesystem."""
    def __init__(self):
        self.ready = threading.Event()

    def snapshot(self, progress):
        progress('示例扫描', 0, 48)
        if not self.ready.wait(30):
            raise AssertionError('Demo scan was not released')
        return home_fixture()


def recency(days, workspace=None):
    value = activity.classify(NOW_NS - days * DAY_NS, now_ns=NOW_NS,
                              basis='会话索引 / 工作目录文件修改')
    return dict(value, workspace=workspace) if workspace else value


def readable_svg(source):
    # Rich positions runs by terminal cells, but exports textLength by code points.
    # Match that width to the real cells so double-width Chinese glyphs do not overlap.
    def fix_width(match):
        start, width, end, content = match.groups()
        text = html.unescape(content)
        cells = cell_len(text)
        if text and cells != len(text):
            width = f'{float(width) * cells / len(text):.3f}'.rstrip('0').rstrip('.')
        return f'{start}{width}{end}{content}</text>'

    source = re.sub(r'(<text\b[^>]*\btextLength=")([\d.]+)("[^>]*>)([^<]*)</text>',
                    fix_width, source)
    # Rasterizers otherwise trim leading spaces and break terminal alignment / cat art.
    source = source.replace('<svg ', '<svg xml:space="preserve" ', 1)
    return '\n'.join(line.rstrip() for line in source.splitlines()) + '\n'


def home_fixture():
    values = [
        ('codex', 'worktrees', 68, 0), ('codex', 'sessions', 3, 0),
        ('codex', 'plugins', 1, 0), ('codex', 'logs', .5, .25),
        ('codex', 'database', .5, 0), ('claude', 'sessions', 1.25, 0),
        ('claude', 'cache', .75, .125), ('t3', 'sessions', .25, 0),
        ('t3', 'cache', .125, .03125),
    ]
    groups = [dict(agent=agent, category=category, bytes=int(amount * GIB),
                   eligible_bytes=int(eligible * GIB), eligible_files=int(bool(eligible)))
              for agent, category, amount, eligible in values]
    return dict(groups=groups, bytes=sum(r['bytes'] for r in groups),
                eligible_bytes=sum(r['eligible_bytes'] for r in groups), eligible_files=3,
                full=True, deferred_dirs=0, errors=[], missing_roots=[],
                roots={a: f'/Users/demo/.{a}' for a in ('codex', 'claude', 't3')},
                measured_at=EXAMPLE_TIME.isoformat(),
                agent_resources={'entries': [{'agent': 'codex'}] * 4},
                extensions={'groups': [{'agents': ['codex']}, {'agents': ['claude']}]})


def dependency_fixture():
    entries, projects = [], []
    for name, artifact, ecosystem, amount, freed, days in [
        ('rust-index', 'target', 'Rust', 2.4 * GIB, 2.4 * GIB, 28),
        ('offline-docs', '.venv', 'Python', 780 * MIB, 778 * MIB, 24),
        ('billing-ui', '.next', 'JavaScript / TypeScript', 420 * MIB, 420 * MIB, 18),
        ('api-timeout', 'node_modules', 'JavaScript / TypeScript', 4.1 * GIB, 8 * MIB, 12),
        ('search-ui', 'node_modules', 'JavaScript / TypeScript', 3.6 * GIB, 5 * MIB, 16),
    ]:
        workspace = f'/Users/demo/.codex/worktrees/{name}/app'
        amount, freed = int(amount), int(freed)
        entries.append(dict(path=f'{workspace}/{artifact}', label=f'{name} / app / {artifact}',
            kind='directory', status='candidate', agent='codex', ecosystem=ecosystem,
            artifact=artifact, artifact_category='项目依赖' if artifact in ('.venv', 'node_modules') else '构建产物',
            rebuild='使用项目包管理器或构建命令重新生成', target=None, reason=None,
            referenced_by=[], directory_bytes=amount, shared_bytes=amount-freed,
            known_shared_bytes=amount-freed, unknown_bytes=0, reclaimable_bytes=freed,
            reclaimable_min_bytes=freed, reclaimable_max_bytes=freed,
            activity=recency(days, workspace),
            installation={'reason': '项目清单和可重建条件已核对（示例）'},
            runtime={'reason': '未发现运行引用（示例）'}))
        projects.append(dict(path=workspace, agent='codex', artifacts=1,
                             directory_bytes=amount+48*MIB, activity=recency(days)))
    held = dict(entries[-1], path='/Users/demo/.codex/worktrees/shared-ui/app/node_modules',
                label='shared-ui / app / node_modules', status='retained',
                reason='其他工作树仍在引用',
                referenced_by=['/Users/demo/.codex/worktrees/live-ui/app/node_modules'])
    entries.append(held)
    entries.append(dict(path=held['referenced_by'][0], kind='symlink', status='retained',
                        target=held['path'], referenced_by=[]))
    return dict(mode='measured', entries=entries, projects=projects, errors=[],
                external_targets={}, measured_at=EXAMPLE_TIME.isoformat(), title='工作树 / 多语言产物')


def session_fixture():
    doc, all_rows = dict(limit=20, agents={}), {}
    examples = [
        ('整理离线安装指南', 32, 'open', 'archive'),
        ('追踪登录超时的原因', 24, 'open', 'archive'),
        ('优化会话列表筛选', 18, 'open', 'archive'),
        ('构建依赖占用报告', 12, 'open', 'archive'),
        ('更新缓存保护规则', 7, 'open', 'archive'),
        ('调整项目首页布局', 2, 'open', 'archive'),
        ('归档旧版接口实验', 35, 'archived', 'delete'),
        ('复核迁移脚本结果', 25, 'archived', 'delete'),
    ]
    for agent in ('codex', 'claude'):
        rows = [dict(session_id=f'019a5290-7f49-7c91-8d5e-{i+1:012x}', title=title,
                     state=state, route='review' if agent == 'claude' else route,
                     idle_days=days, bytes=(i+1)*8192, latest_ns=NOW_NS-days*DAY_NS,
                     hold_reason=None, activity=recency(days))
                for i, (title, days, state, route) in enumerate(examples)]
        doc['agents'][agent] = dict(policy={'archive_days':10, 'delete_days':20}, errors=[],
            session_count=len(rows), groups={}, histogram=dict(zip(BUCKETS, [1, 1, 1, 1, 4])))
        all_rows[agent] = rows
    return doc, all_rows


class Clip:
    def __init__(self, name, size):
        self.name, self.size, self.frames = name, size, []
        self.directory = FRAMES / name
        self.directory.mkdir(parents=True, exist_ok=True)

    async def capture(self, pilot, duration):
        assert duration > 0 and duration % 10 == 0
        await pilot.pause()
        name = f'{len(self.frames):03}.svg'
        pilot.app.save_screenshot(name, path=str(self.directory))
        svg = self.directory / name
        svg.write_text(readable_svg(svg.read_text()), encoding='utf-8')
        self.frames.append((svg, duration))

    def encode(self):
        def render(frame):
            svg, _ = frame
            subprocess.run(['rsvg-convert', '--width', '1600', '--keep-aspect-ratio',
                            '--background-color', '#111b1d', '--output', str(svg.with_suffix('.png')),
                            str(svg)], check=True, capture_output=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(render, self.frames))
        command = ['magick']
        for svg, duration in self.frames:
            command += ['-delay', str(duration // 10), str(svg.with_suffix('.png'))]
        output = OUT / (self.name + '.gif')
        command += ['-loop', '0', '+dither', '-colors', '128', '-layers', 'Optimize', str(output)]
        subprocess.run(command, check=True, capture_output=True)
        return output


async def main():
    for command in ('rsvg-convert', 'magick'):
        if not shutil.which(command):
            raise SystemExit(f'Missing {command}: see docs/screenshots.md for recording dependencies.')
    OUT.mkdir(exist_ok=True)
    captures, clips = [], []
    def save(app, name, size):
        app.save_screenshot(name, path=str(OUT))
        svg = OUT / name
        svg.write_text(readable_svg(svg.read_text()))
        captures.append((name, size, hashlib.sha256((OUT/name).read_bytes()).hexdigest()))

    service = DemoScan()
    app = DashboardApp(service, snapshot=home_fixture())
    clip = Clip('dashboard', (120, 34))
    clips.append(clip)
    try:
        async with app.run_test(size=clip.size) as pilot:
            await pilot.press('down')
            await clip.capture(pilot, 1800)
            assert app.query_one('#heading').region.bottom < app.query_one('#tabs').region.y
            assert app.query_one(DataTable).row_count == 7
            save(app, 'dashboard.svg', clip.size)
            await pilot.press('r')
            loading = app.query_one(ScanLoading)
            loading.timer.pause()
            assert app.operation_busy and not loading.has_class('compact')
            for frame in range(48):
                loading.frame = frame
                loading.started = time.monotonic() - frame * .08
                loading.set_progress('示例扫描', frame + 1, 48)
                await clip.capture(pilot, 80)
            service.ready.set()
            for _ in range(100):
                await pilot.pause(.02)
                if not app.operation_busy:
                    break
            assert not app.operation_busy
            await clip.capture(pilot, 1800)
            for key in ('1', '2', '3', '0'):
                await pilot.press(key)
                await clip.capture(pilot, 1600)
            await pilot.press('down')
            await clip.capture(pilot, 2200)
    finally:
        service.ready.set()

    report = dependency_fixture()
    app = StorageApp(report, actions=NoActions(), loader=NoActions().forbidden, load_on_mount=False)
    app.return_to_dashboard = True
    clip = Clip('deps-table', (152, 34))
    clips.append(clip)
    async with app.run_test(size=clip.size) as pilot:
        await clip.capture(pilot, 1800)
        await pilot.press('space')
        await clip.capture(pilot, 1100)
        await pilot.press('down', 'space')
        await clip.capture(pilot, 1100)
        await pilot.click('#select-all')
        assert len(app.checked) == 5
        assert not app.query_one('#select-all', Button).disabled
        assert app.query_one('#select-all').region.bottom <= app.query_one(DataTable).region.y
        await clip.capture(pilot, 2000)
        index = next(i for i, row in enumerate(app.rows) if '/api-timeout/' in row['path'])
        app.query_one(DataTable).move_cursor(row=index)
        await clip.capture(pilot, 2200)
        save(app, 'deps-table.svg', clip.size)
        await pilot.press('t')
        assert app.screen.heading == '确认暂存'
        await clip.capture(pilot, 3500)
        await pilot.press('escape')
        await clip.capture(pilot, 1400)
        await pilot.press('2')
        await clip.capture(pilot, 2000)
        await pilot.press('1')
        await clip.capture(pilot, 1200)

    doc, rows = session_fixture()
    app = SessionApp(doc, rows, actions=NoActions())
    app.return_to_dashboard = True
    clip = Clip('reporter-table', (156, 34))
    clips.append(clip)
    async with app.run_test(size=clip.size) as pilot:
        await clip.capture(pilot, 1800)
        await pilot.press('space')
        await clip.capture(pilot, 1100)
        await pilot.press('down', 'space')
        await clip.capture(pilot, 2000)
        assert len(app.checked) == 2
        assert any(str(column.label) == '完整会话 ID' for column in app.query_one(DataTable).columns.values())
        assert not app.query_one('#review', Button).disabled
        save(app, 'reporter-table.svg', clip.size)
        await pilot.press('x')
        assert app.screen.heading == '确认归档 · 操作清单'
        await clip.capture(pilot, 4000)
        await pilot.press('escape', 'v')
        await clip.capture(pilot, 2200)
        await pilot.press('s')
        await clip.capture(pilot, 2200)
        await pilot.press('2')
        await clip.capture(pilot, 1800)
        await pilot.press('1', 'a')
        await clip.capture(pilot, 1400)

    for clip in clips:
        output = clip.encode()
        print(f'{output.name}: {len(clip.frames)} frames, {sum(ms for _, ms in clip.frames)/1000:.2f}s, '
              f'{output.stat().st_size/1024:.0f} KiB', flush=True)

    captured = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
    notes = f'''# 历史 Python 版动态演示 / Legacy Python demos

以下为迁移前 Python 版演示；当前 Rust 版见 [Rust 截图](rust-screenshots.md)。These are legacy demonstrations.

- 生成时间 / Captured: `{captured}`
- 录制版本 / Captured version: `{__version__}`
- 来自实际 Textual 界面，数据为固定示例，不含真实用户路径或会话。
- Rendered by the actual Textual apps with fictional fixtures. No user storage was scanned and no native actions were executed.
- 保留三段历史循环 GIF：小猫扫描与 Agent 切换、依赖勾选与暂存复核、会话勾选与归档清单。
- Three looping GIFs cover the cat loading animation and agent filters, dependency selection and staging review, and session selection and archive review.
- 扫描进度为模拟数据；确认页只展示并取消。Loading progress is simulated; confirmation dialogs are only previewed and cancelled.

## 重新生成 / Regenerate

录制依赖 / Recording dependencies: Python UI extras (`pip install -e '.[ui]'`), `rsvg-convert` (librsvg), ImageMagick 7 (`magick`), and a CJK font.
这些仅用于制作文档，终端工具和发布安装包不需要它们。These are documentation tools, not runtime dependencies.

macOS: `brew install librsvg imagemagick`。Linux 可安装对应工具与中文字体，确保上述命令位于 PATH；Windows 可在 WSL 中录制。

在仓库根目录运行 / Run from the repository root:

```sh
.venv/bin/python dev/capture-readme.py
```

脚本调用历史 Python 版 `DashboardApp`、`StorageApp` 和 `SessionApp`，通过实际按键 / 点击录制。
执行接口均为禁止操作的替身；修改产品界面后重新运行即可更新 GIF 和 SVG 静态备份。
The script records real key presses and clicks against the current apps with blocked mutation handlers, then renders SVG frames offline and encodes GIFs.
中间帧保存在忽略的 `reports/readme-gif/`；GIF 宽度为 1600 像素，停留 1–4 秒方便阅读，小猫动画为 80 毫秒 / 帧。
Intermediate frames stay in ignored `reports/readme-gif/`. GIFs are 1600 px wide, with 1–4 second reading pauses and 80 ms cat-animation frames.
画面中的采集时间 `{EXAMPLE_TIME.isoformat()}` 属于示例数据，不是真实扫描时间。

| 动态演示 / GIF | 时长 / Duration | 大小 / Size | SHA-256 |
| --- | --- | --- | --- |
'''
    for clip in clips:
        output = OUT / (clip.name + '.gif')
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        notes += (f'| [{output.name}]({output.name}) | {sum(ms for _, ms in clip.frames)/1000:.2f}s '
                  f'| {output.stat().st_size/1024:.0f} KiB | `{digest}` |\n')
    notes += '\n## 静态备份 / Static alternatives\n\n| 文件 / File | 终端尺寸 / Columns × rows | SHA-256 |\n| --- | --- | --- |\n'
    for name, size, digest in captures:
        notes += f'| [{name}]({name}) | {size[0]} × {size[1]} | `{digest}` |\n'
    (OUT/'screenshots.md').write_text(notes, encoding='utf-8')
    print(f'Regenerated {len(clips)} GIFs and {len(captures)} stills from v{__version__}; native actions disabled.')


if __name__ == '__main__':
    asyncio.run(main())
