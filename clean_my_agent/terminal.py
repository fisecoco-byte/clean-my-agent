"""Local terminal rendering. Nothing here scans, selects or changes files."""
from contextlib import contextmanager
import os
import shutil
import sys
import threading
import time
import unicodedata

from .policy import REASONS
from .scan import LABELS


def fmt(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f'{n} B' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def safe(value):
    return ''.join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in str(value))


def cells(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'FW' else 1
               for c in text)


def pad(text, width, right=False):
    gap = ' ' * max(0, width - cells(text))
    return gap + text if right else text + gap


def clipped(text, width):
    if cells(text) <= width:
        return text
    end = ''
    used = 0
    for ch in text:
        used += cells(ch)
        if used > width - 1:
            break
        end += ch
    return end + '…' if width > 0 else ''


def progress_line(width, elapsed, tick, label, done, total, detail):
    """Counts are local to the named phase; elapsed time covers the whole scan."""
    clock = f'{elapsed//60:02d}:{elapsed%60:02d}'
    if total:
        slots = min(16, (width - 26) // 2) if width >= 36 else 0
        filled = done * slots // total
        bar = ('█' * filled + '░' * (slots - filled) + '  ') if slots else ''
        meter = f'  {bar}{done}/{total}'
        if width >= 45:
            meter += f' {100 * done // total:3}%'
        prefix = clock + '  '
    else:
        meter = ''
        frame = '·' if tick is None else '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[tick % 10]
        prefix = f'{clock}  {frame}  '
    line = prefix + clipped(label, width - cells(prefix + meter)) + meter
    room = width - cells(line) - 3
    if detail and room >= 4:
        line += ' · ' + clipped(detail, room)
    return clipped(line, width)


NAMES = {'codex': 'Codex', 'claude': 'Claude Code', 't3': 'T3 Code'}
PROFILES = {'standard': '日常', 'conservative': '保守', 'deep': '深度'}
INK = {'title': '1', 'accent': '36', 'value': '1;32', 'caution': '33', 'muted': '2'}
INK.update({f'slice{i}': color for i, color in enumerate(('36', '32', '33', '35', '31'))})


class Terminal:
    def __init__(self, plain=False):
        self.width = min(96, shutil.get_terminal_size((80, 24)).columns)
        self.active = (not plain and sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'
                       and self.width >= 30)
        self.color = self.active and 'NO_COLOR' not in os.environ

    def ink(self, text, style):
        return f'\x1b[{INK[style]}m{text}\x1b[0m' if self.color else text

    def header(self, page, label):
        if not self.active:
            return
        print()
        if self.width >= 56:
            brand = 'CLEAN MY AGENT'
            print('  ' + self.ink('.--------.', 'accent'))
            print('  ' + self.ink('| :::  o |', 'accent') + '   ' + self.ink(brand, 'title')
                  + ' ' * (self.width - 16 - len(brand) - cells(label) - 2)
                  + self.ink(f'[{label}]', 'accent'))
            print('  ' + self.ink("'--------'", 'accent') + '   ' + page)
        else:
            print('  ' + self.ink('CLEAN MY AGENT', 'title'))
            if cells(page) + cells(label) + 6 <= self.width:
                print('  ' + page + '  ' + self.ink(f'[{label}]', 'accent'))
            else:
                print('  ' + page)
                print('  ' + self.ink(f'[{label}]', 'accent'))

    def section(self, title):
        if not self.active:
            print(title)
            return
        mark = '  ' + title + '  '
        print('\n' + self.ink(mark, 'title') + self.ink('─' * max(0, self.width - cells(mark)), 'muted'))

    def metrics(self, pairs):
        """One band of metrics, stacked when the values would not fit."""
        inner = self.width - 4
        slots = [inner // len(pairs)] * len(pairs)
        slots[-1] += inner - sum(slots)
        def row(text):
            print('  ' + self.ink('│', 'muted') + text + self.ink('│', 'muted'))
        print()
        print('  ' + self.ink('┌' + '─' * inner + '┐', 'muted'))
        if all(slot >= max(cells(k), cells(v)) + 3 for (k, v), slot in zip(pairs, slots)) and self.width >= 54:
            for index in (0, 1):
                row(''.join('  ' + self.ink(pad(pair[index], slot - 2), 'muted' if index == 0 else
                                             'value' if col == 1 else 'title')
                            for col, (pair, slot) in enumerate(zip(pairs, slots))))
        else:
            for k, v in pairs:
                row(' ' + pad(k, inner - cells(v) - 2) + self.ink(v, 'title') + ' ')
        print('  ' + self.ink('└' + '─' * inner + '┘', 'muted'))

    def meter(self, label, amount, total):
        fraction = amount / total if total else 0
        percent = f'{fraction:.1%}'
        if self.width < 52:
            print('  ' + pad(label, 13) + fmt(amount))
            return
        slots = min(40, self.width - 36)
        count = round(slots * fraction)
        bar = self.ink('━' * count, 'accent') + self.ink('·' * (slots - count), 'muted')
        print('  ' + pad(label, 13) + bar + '  ' + pad(percent, 6, True) + '  ' + pad(fmt(amount), 9, True))

    def entry(self, label, value):
        if self.width >= 56:
            print('  ' + pad(label, 12) + value)
        else:
            print('  ' + label + '\n    ' + value)

    def note(self, text):
        # Break prose at cell boundaries; keep paths and commands copied via entry() intact.
        line = ''
        for char in text:
            if cells(line) + cells(char) > self.width - 4:
                print('  ' + line)
                line = ''
            line += char
        if line:
            print('  ' + line)

    @contextmanager
    def working(self, text):
        if not self.active or not sys.stderr.isatty():
            yield
            return
        stop = threading.Event()
        state = (safe(text), None, None, '')
        started = time.monotonic()
        def update(message, done=None, total=None, detail=''):
            nonlocal state
            state = (safe(message), done, total, safe(detail))
        def spin():
            tick = 0
            previous, length = None, 0
            while not stop.wait(0.15 if tick else 0.25):
                current = state
                width = min(self.width, shutil.get_terminal_size((80, 24)).columns) - 1
                line = progress_line(width, int(time.monotonic() - started),
                                     tick if self.color else None, *current)
                if previous != line:
                    print('\r' + pad(line, min(width, length)), end='', file=sys.stderr, flush=True)
                    previous, length = line, cells(line)
                tick += 1
            if tick:
                print('\r' + ' ' * length + '\r', end='', file=sys.stderr, flush=True)
        thread = threading.Thread(target=spin, daemon=True)
        thread.start()
        try:
            yield update
        finally:
            stop.set()
            thread.join()

    def overview(self, args, doc):
        clean = args.cmd == 'clean'
        scope = '完整扫描' if doc['full'] else '快速扫描'
        p = doc['policy']
        profile = p['profile']
        self.header(('清理预览' if clean and not args.apply else '清理清单' if clean else '存储概览')
                    + ' / ' + scope, '暂存' if clean and args.apply else '只读')
        print('  ' + self.ink(f'策略  {PROFILES[profile]} / {profile}', 'muted'))
        self.metrics([('目录计块' if doc['full'] else '已统计', fmt(doc['bytes'])), ('可清理', fmt(doc['eligible_bytes'])),
                      ('候选文件', f"{doc['eligible_files']} 个")])
        if doc['errors']:
            self.note('扫描不完整，以上仅为已统计的数据。')
        if doc['deferred_dirs']:
            self.note(f"快速扫描未展开 {doc['deferred_dirs']} 个目录，它们不计入上面的占用。")
        rows = doc['groups']
        if rows and not clean:
            self.section('占用分布')
            totals = {a: sum(r['bytes'] for r in rows if r['agent'] == a) for a in NAMES}
            for a in sorted({r['agent'] for r in rows}, key=lambda key: (-totals[key], key)):
                self.meter(NAMES[a], totals[a], doc['bytes'])
        self.section('可清理项目')
        candidates = sorted((r for r in rows if r['eligible_files']), key=lambda r: -r['eligible_bytes'])
        if not candidates:
            self.note('没有文件符合当前策略。')
        for r in candidates:
            label = f"{NAMES[r['agent']]} / {LABELS[r['category']]}"
            info = f"{fmt(r['eligible_bytes'])}  ·  {r['eligible_files']} 个  ·  保留 {p['retention_days'][r['category']]} 天"
            if self.width >= 72:
                print('  ' + pad(label, 30) + info)
            else:
                print('  ' + label)
                self.note(f"  {fmt(r['eligible_bytes'])} · {r['eligible_files']} 个 · {p['retention_days'][r['category']]} 天")
        if args.agent in ('all', 'codex'):
            self.note('会话与工作树仅盘点；用 inventory 查看 ID 和 Codex 管理方式。')
        if args.details:
            if not clean:
                self.section('分类占用')
                for r in rows:
                    self.entry(fmt(r['bytes']), NAMES[r['agent']] + ' / ' + LABELS[r['category']])
            if doc['files']:
                self.section('候选文件路径')
                for f in doc['files']:
                    self.entry(fmt(f['bytes']), safe(f['path']))
        if doc.get('largest_groups'):
            self.section('占用最大的目录或文件组')
            for f in doc['largest_groups']:
                self.entry(fmt(f['bytes']), safe(f['path']))
        if doc['kept']:
            self.section('保留原因')
            self.note('  /  '.join(f"{REASONS[k]} {v['files']} 个" for k, v in sorted(doc['kept'].items())))
        for path in doc['missing_roots']:
            self.note('未找到：' + safe(path))

    def policy(self, policy):
        self.header('清理策略', PROFILES[policy.profile] + ' / ' + policy.profile)
        self.section('分类保留期')
        for cat, days in policy.retention.items():
            gate = '需单独选择依赖目录' if cat == 'dependencies' else '常规清理'
            if self.width >= 54:
                print('  ' + pad(LABELS[cat], 18) + pad(f'{days} 天', 9) + gate)
            else:
                self.note(f'{LABELS[cat]} · {days} 天')
                self.note('  ' + gate)
        self.section('保护范围')
        self.note('会话（含归档）、快照、历史工具输出、工作树本体均保留。用 inventory 查看 Codex 管理方式。')
        for rule in policy.protect:
            print('  ' + safe(rule))
        if not policy.protect:
            self.note('未设置个人保护路径。')
        self.section('配置文件')
        print('  ' + safe(policy.path))
