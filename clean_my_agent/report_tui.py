"""Keyboard-driven, read-only view of one inventory snapshot using ncurses."""
import os
import sys

from .inventory import SECTIONS, plan
from .pie import STYLES, pie_lines
from .policy import SESSION_DAYS
from .session_titles import UNTITLED
from .terminal import NAMES, cells, fmt, pad, safe


def wrap(text, width):
    """Wrap by terminal cells, including CJK text and long paths/IDs."""
    line, used = '', 0
    for char in text:
        size = cells(char)
        if used + size > width:
            yield line
            line, used = '', 0
        line += char
        used += size
    yield line


class Report:
    def __init__(self, doc, rows, details=False):
        self.doc, self.rows = doc, rows
        self.keys = list(doc['agents'])
        self.agent = self.keys[0]
        self.rules = {key: data['policy'].copy() for key, data in doc['agents'].items()}
        self.action = 'archive'
        self.details, self.help, self.offset = details, False, 0
        self.recalculate()

    def recalculate(self):
        self.results = plan(self.rows[self.agent], **self.rules[self.agent], limit=self.doc['limit'])
        self.offset = 0

    def input(self, key, page_size, max_offset):
        if key in ('q', 'Q') or (key == 'ESC' and not self.help):
            return False
        if key == '?' or key == 'ESC':
            self.help, self.offset = not self.help, 0
        elif key in ('UP', 'k', 'DOWN', 'j', 'PAGE_UP', 'PAGE_DOWN', 'g', 'G', 'HOME', 'END'):
            delta = {'UP': -1, 'k': -1, 'DOWN': 1, 'j': 1, 'PAGE_UP': -page_size, 'PAGE_DOWN': page_size}
            self.offset = (0 if key in ('g', 'HOME') else max_offset if key in ('G', 'END')
                           else max(0, min(max_offset, self.offset + delta[key])))
        elif not self.help:
            if key in ('TAB', '1', '2'):
                target = self.keys[(self.keys.index(self.agent) + 1) % len(self.keys)] if key == 'TAB' else (
                    'codex' if key == '1' else 'claude')
                if target in self.keys:
                    self.agent = target
                    self.recalculate()
            elif key in ('a', 'A', 'd', 'D'):
                self.action = 'archive' if key.lower() == 'a' else 'delete'
                rule = self.action + '_days'
                idx = SESSION_DAYS.index(self.rules[self.agent][rule])
                self.rules[self.agent][rule] = SESSION_DAYS[(idx + (1 if key.islower() else -1)) % len(SESSION_DAYS)]
                self.details = False
                self.recalculate()
            elif key in ('v', 'LEFT', 'RIGHT'):
                self.action = 'delete' if self.action == 'archive' else 'archive'
                self.details, self.offset = False, 0
            elif key == 'i':
                self.details, self.offset = not self.details, 0
        return True

    def body(self, width):
        # Each line is a list of styled spans. The viewport is shared by mouse-free
        # small terminals, the detail list, help and resized windows.
        lines = []
        def line(text='', style='normal'):
            for part in wrap(text, width):
                lines.append([(part, style)])
        def section(label):
            lines.append([(label + '  ', 'bold'), ('─' * max(0, width - cells(label) - 2), 'dim')])

        data = self.doc['agents'][self.agent]
        if self.help:
            section('按键')
            for k, description in [('Tab / 1 / 2', '轮换工具 / Codex / Claude Code'),
                                   ('a / A', '增加 / 减少归档复核阈值'),
                                   ('d / D', '增加 / 减少删除复核阈值'),
                                   ('v 或 ← →', '切换归档与删除候选清单'),
                                   ('i', '查看文件与工作树明细 / 返回图表'),
                                   ('↑ ↓ / j k', '滚动一行'), ('PgUp / PgDn', '翻页'),
                                   ('g / G', '滚动至开头 / 结尾'), ('? / Esc', '关闭帮助'), ('q', '退出报告')]:
                line(pad(k, 15) + description)
            line()
            section('如何解读')
            line('阈值为 5、10、15、20 天，循环选择；调整只对当前视图有效。')
            line('按最近可见活动估算，不代表会话进程已经停止。报告不执行归档或删除。')
            for value in data['guidance'].values():
                line(safe(value))
            return lines

        if data['errors']:
            line('盘点不完整，已暂停此工具的复核建议。', 'warn')
            for error in data['errors'][:3]:
                line(safe(error))
        if self.details:
            for group, info in data['groups'].items():
                size = '未完整测量' if info['bytes'] is None else fmt(info['bytes'])
                section(f"{SECTIONS[group]} · {info['count']} 项 · {size}")
                for item in info['entries']:
                    line(safe(item['path']))
                line(f"显示 {len(info['entries'])} 项 / {info['count']} 项", 'dim')
                line()
            return lines

        rules = self.rules[self.agent]
        metrics = [('已识别会话', data['session_count'], 'bold'),
                   (f"归档复核 ≥{rules['archive_days']} 天", self.results['archive']['count'], 'accent'),
                   (f"删除复核 ≥{rules['delete_days']} 天", self.results['delete']['count'], 'warn')]
        if width >= 51:
            slot = width // 3
            lines.append([(pad(k, slot), 'dim') for k, _, _ in metrics])
            lines.append([(pad(f'{n:,}', slot), color) for _, n, color in metrics])
        else:
            for k, n, color in metrics:
                lines.append([(pad(k, 19), 'normal'), (f'{n:,}', color)])
        line()
        section('未活跃天数分布 · 会话数占比')
        lines.extend(pie_lines(data['histogram'], width))
        line(f"主会话文件 {fmt(data['session_bytes'])}  ·  已排除 {data['held']} 个", 'dim')
        line()
        group = self.results[self.action]
        kind = '归档' if self.action == 'archive' else '删除'
        section(f"{kind}复核清单 · {len(group['entries'])} / {group['count']}")
        if self.agent == 'claude':
            line('归档状态未核实，仅供人工复核。', 'dim')
        if not group['entries']:
            line('当前阈值没有候选。')
        else:
            for row in group['entries']:
                line(row['title'] or UNTITLED, 'bold' if row['title'] else 'dim')
                if width >= 62:
                    line(f"ID {row['session_id']}  ·  {row['idle_days']} 天  ·  {fmt(row['bytes'])}", 'dim')
                else:
                    line(f"{row['idle_days']} 天 · {fmt(row['bytes'])}", 'dim')
                    line('ID ' + row['session_id'])
        return lines


def _screen(win, c, model):
    try:
        c.curs_set(0)
    except c.error:
        pass
    win.keypad(True)
    styles = dict(normal=0, bold=c.A_BOLD, dim=c.A_DIM, accent=c.A_BOLD, warn=c.A_BOLD, tab=c.A_REVERSE)
    styles.update(dict.fromkeys(STYLES, c.A_BOLD))
    if c.has_colors():
        try:
            c.use_default_colors()
            if 'NO_COLOR' not in os.environ:
                colors = [('accent', c.COLOR_CYAN), ('warn', c.COLOR_YELLOW), *zip(STYLES,
                          (c.COLOR_CYAN, c.COLOR_GREEN, c.COLOR_YELLOW, c.COLOR_MAGENTA, c.COLOR_RED))]
                for i, (key, color) in enumerate(colors, 1):
                    c.init_pair(i, color, -1)
                    styles[key] |= c.color_pair(i)
        except c.error:
            pass
    keys = {c.KEY_UP:'UP', c.KEY_DOWN:'DOWN', c.KEY_LEFT:'LEFT', c.KEY_RIGHT:'RIGHT', c.KEY_NPAGE:'PAGE_DOWN',
            c.KEY_PPAGE:'PAGE_UP', c.KEY_HOME:'HOME', c.KEY_END:'END', '\t':'TAB', '\x1b':'ESC'}

    while True:
        h, w = win.getmaxyx()
        win.erase()
        def draw(y, x, text, style='normal'):
            if 0 <= y < h and 0 <= x < w - 1:
                part = next(wrap(text, w - x - 1))
                try:
                    win.addstr(y, x, part, styles[style])
                except c.error:  # A resize can invalidate a position mid-frame.
                    pass

        viewport, maximum = 1, 0
        if w < 30 or h < 9:
            draw(0, 0, '窗口太小 · q 退出', 'bold')
            draw(1, 0, '请放大到至少 30 列 × 9 行')
        else:
            left = 2 if w >= 50 else 1
            width = w - left * 2 - 1
            draw(0, left, 'CLEAN MY AGENT  /  会话报告' if w >= 44 else '会话报告', 'bold')
            draw(0, w - left - 8, '[只读]', 'accent')
            x = left
            for agent in model.keys:
                tab = ' ' + ('1' if agent == 'codex' else '2') + ' ' + NAMES[agent] + ' '
                draw(2, x, tab, 'tab' if agent == model.agent else 'dim')
                x += cells(tab) + 1
            footer = 2 if w < 80 else 1
            viewport = max(1, h - 4 - footer - 1)
            body = model.body(width)
            maximum = max(0, len(body) - viewport)
            model.offset = min(model.offset, maximum)
            for y, spans in enumerate(body[model.offset:model.offset + viewport], 4):
                x = left
                for value, style in spans:
                    draw(y, x, value, style)
                    x += cells(value)
            status = (f'{model.offset + 1}–{min(len(body), model.offset + viewport)} / {len(body)} 行'
                      if maximum else '单页')
            draw(h - footer - 1, left, status + ' · ↑↓ 滚动', 'dim')
            if model.help:
                draw(h - 1, left, '? 或 Esc 返回', 'bold')
            elif footer == 1:
                draw(h - 1, left, 'Tab 工具   a 归档天数   d 删除天数   v 清单   i 明细   ? 帮助   q 退出', 'bold')
            else:
                draw(h - 2, left, 'Tab 工具  a 归档  d 删除', 'bold')
                draw(h - 1, left, 'v 清单  ? 帮助  q 退出', 'bold')
        win.refresh()
        event = win.get_wch()
        if event == '\x03':
            return
        if not model.input(keys.get(event, event), viewport, maximum):
            return


def show(doc, rows, details=False, *, actions=None):
    """Return False if this terminal cannot host a full-screen report."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get('TERM', '') in ('', 'dumb'):
        return False
    try:
        from .session_tui import SessionApp
    except ImportError:
        pass  # The dependency-free legacy view remains read-only.
    else:
        if actions is not None:
            app = SessionApp(doc, rows, actions=actions, details=details)
            app.run()
            for result in app.operation_results:
                print(result)
            return True
    try:
        import curses
    except ImportError:
        return False
    try:
        curses.setupterm(fd=sys.stdout.fileno())
        if not curses.tigetstr('cup'):
            return False
        curses.wrapper(_screen, curses, Report(doc, rows, details))
    except curses.error:
        return False
    return True
