"""Home, file cleanup and quarantine pages in the same terminal workflow."""
from pathlib import Path
import time

from rich.text import Text
from textual import on, events
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, ProgressBar, Static

from .scan import LABELS, agent as agent_of
from .terminal import NAMES, fmt, safe
from .tui_common import ActionScreen, DetailScreen, ManagedApp, SHARED_CSS

MINT, SOFT, AMBER = '#7de2bf', '#9ab4ac', '#efc480'


class LoadingApp(ManagedApp):
    CSS = SHARED_CSS
    def __init__(self, title, loader):
        super().__init__()
        self.heading, self.loader = title, loader
    def on_mount(self):
        self.push_screen(ActionScreen(self.heading, Text('正在读取，请稍候…'), self.loader,
                                     self.completed, auto_run=True))
    def completed(self, result):
        self.exit(result)
        return Text('读取完成')


def load_page(title, loader):
    result = LoadingApp(title, loader).run()
    if result is None:
        raise ValueError('已取消读取')
    if isinstance(result, dict) and 'error' in result:
        raise ValueError(result['error'])
    return result


class DashboardApp(ManagedApp):
    TITLE = 'Clean My Agent · 存储总览'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [('q','quit','退出'), ('0',"agent('all')",'全部'), ('1',"agent('codex')",'Codex'),
                ('2',"agent('claude')",'Claude'), ('3',"agent('t3')",'T3'), ('r','reload','刷新'), ('question_mark','help','帮助')]
    CSS = SHARED_CSS + """
        #tabs Button { min-width: 8; padding: 0 1; margin-right: 1; }
        #metrics { height: auto; padding: 0 2; border: solid #364a46; margin-bottom: 1; }
        #home-progress { height: 1; }
        #open { width: auto; height: 1; border: none; background: #7de2bf; color: #10251e; padding: 0 2; }
        #home-message { height: auto; max-height: 2; color: #9ab4ac; margin-bottom: 1; }
        #home-message.attention { color: #efc480; }
    """

    def __init__(self, service, *, snapshot=None, agent='all', notices=()):
        super().__init__()
        self.service, self.snapshot = service, snapshot
        self.agent_key = service.agent if service.agent != 'all' else agent
        self.notices = notices
        self.rows = []
        self.phase = '准备统计主目录占用'
        self.started = None

    def compose(self):
        yield Static('窗口太小，请调整到至少 44 列 × 16 行。q 退出', id='too-small')
        yield Static('CLEAN MY AGENT    /    HOME', id='brand')
        yield Static('Agent 存储总览', id='heading')
        with Horizontal(id='tabs'):
            for key, name in [('all','0 全部'),('codex','1 Codex'),('claude','2 Claude'),('t3','3 T3')]:
                yield Button(name, id=key, compact=True)
        yield Static('统计中…', id='metrics', markup=False)
        yield Static('', id='home-message', markup=False)
        yield ProgressBar(show_eta=False, id='home-progress')
        with Horizontal(id='layout'):
            with Vertical(id='listpane'):
                yield DataTable(cursor_type='row', id='grid', show_row_labels=False)
        yield Button('Enter  打开', id='open', compact=True, disabled=True)
        yield Static('↑↓ 选择 · r 刷新 · ? 帮助 · q 退出', id='footer')

    def on_mount(self):
        self.set_interval(1, self.elapsed)
        if self.snapshot is None:
            self.action_reload()
        else:
            self.fill()
        self.query_one(DataTable).focus()

    def elapsed(self):
        if self.started is not None:
            elapsed = int(time.monotonic() - self.started)
            self.query_one('#home-message', Static).update(f'{elapsed//60:02d}:{elapsed%60:02d} · {self.phase}')

    def on_resize(self, event: events.Resize):
        self.screen_stack[0].set_class(event.size.width < 105, 'narrow')
        self.screen_stack[0].set_class(event.size.width < 44 or event.size.height < 16, 'tiny')
        if self.is_mounted and self.screen is self.screen_stack[0] and self.snapshot:
            self.fill()
            if event.size.width >= 44 and event.size.height >= 16:
                self.call_after_refresh(self.query_one(DataTable).focus)

    def action_reload(self):
        if self.operation_busy or self.screen is not self.screen_stack[0]:
            return
        self.operation_busy = True
        self.started = time.monotonic()
        self.query_one('#open', Button).disabled = True
        self.query_one('#home-progress').display = True
        self.run_worker(self.load, thread=True, exit_on_error=False)

    def load(self):
        def progress(label, done=None, total=None, detail=''):
            self.call_from_thread(self.progress, label, done, total, detail)
        try:
            doc = self.service.snapshot(progress)
        except Exception as exc:
            self.call_from_thread(self.failed, str(exc))
        else:
            self.call_from_thread(self.loaded, doc)

    def progress(self, label, done, total, detail):
        self.phase = safe(label + (' · '+detail if detail else ''))
        self.query_one('#home-progress', ProgressBar).update(total=total, progress=done or 0)
        self.elapsed()

    def failed(self, message):
        self.operation_busy = False
        self.started = None
        self.query_one('#home-progress').display = False
        self.query_one('#home-message').add_class('attention')
        self.query_one('#home-message', Static).update('读取失败：'+safe(message)+' · r 重试，q 退出')

    def loaded(self, doc):
        self.snapshot = doc
        self.operation_busy = False
        self.started = None
        self.fill()

    def action_agent(self, key):
        if self.service.agent != 'all' and key not in ('all', self.service.agent):
            self.notify('本次只扫描了 '+NAMES[self.service.agent])
            return
        if self.service.agent != 'all':
            key = self.service.agent
        self.agent_key = key
        if self.snapshot and not self.operation_busy:
            self.fill()

    def fill(self):
        doc = self.snapshot
        if not doc:
            return
        visible = lambda a: self.agent_key == 'all' or a == self.agent_key
        groups = [r for r in doc['groups'] if visible(r['agent'])]
        total = sum(r['bytes'] for r in groups)
        eligible = sum(r['eligible_bytes'] for r in groups)
        per_agent = '    '.join(f'{NAMES[a]}  {fmt(sum(r["bytes"] for r in groups if r["agent"]==a))}'
                               for a in ('codex','claude','t3') if visible(a) and a in self.service.selected(self.service.agent))
        title = ('文件分配量' if doc.get('read_only') else '主目录占用（含共享）') if doc['full'] else '已统计（部分）'
        self.query_one('#metrics', Static).update(Text(f'{title}  {fmt(total)}    缓存 / 日志可暂存  {fmt(eligible)}\n{per_agent}'))
        for b in self.query('#tabs Button'):
            b.set_class(b.id == self.agent_key, 'chosen')
            b.disabled = self.service.agent != 'all' and b.id != self.service.agent
        self.query_one('#home-progress').display = False
        message = f'采集于 {doc["measured_at"][:19].replace("T"," ")}'
        if doc.get('read_only'):
            message += ' · Windows 只读'
        if not doc['full']:
            message += f' · {doc["deferred_dirs"]} 个目录未统计'
        if doc['errors']:
            message += f' · {len(doc["errors"])} 条提示 [?]'
        if self.notices:
            message = safe(self.notices[-1].splitlines()[0])+' · '+message
        self.query_one('#home-message', Static).update(message)
        self.query_one('#home-message').set_class(bool(doc['errors']) or not doc['full'], 'attention')
        def amount(categories):
            return sum(r['bytes'] for r in groups if r['category'] in categories)
        extras = [r for r in doc['agent_resources']['entries'] if visible(r['agent'])]
        versions = [g for g in doc['extensions']['groups'] if any(visible(a) for a in g['agents'])]
        self.rows = [
            dict(route='sessions', label='会话与归档', size=fmt(amount({'sessions','archives'})),
                 next='只读' if self.agent_key in ('claude', 't3') else '归档 / 删除',
                 detail='按会话 ID、标题和活跃时间复核。Codex 会话确认后调用原生命令归档或删除；Claude 仅供复核。\n\n归档用于整理，不保证释放空间。'),
            dict(route='deps', label='工作树与依赖', size=fmt(amount({'worktrees','codex-projects'})),
                 next='产物 / 删除工作树',
                 detail='按工作树查看完整占用，再区分多语言依赖、构建产物与工具缓存。\n\n选择工作树查看详情或勾选产物。整树删除需核对 Git 改动、进程、引用和忽略文件，确认后通过 Git 执行。'),
            dict(route='clean', label='缓存与旧日志', size=fmt(amount({'cache','logs'})),
                 next='暂存',
                 detail=f'当前符合策略的文件约 {fmt(eligible)}。进入后逐项勾选；确认后移入暂存区。\n\n暂存释放 0 B，可恢复。'),
            dict(route='extensions', label='Skill / MCP / Plugin', size=fmt(amount({'plugins'})),
                 next=f'只读 · {len(versions)} 组多版本',
                 detail='查看来源、版本、引用及原生回收建议。此处大小为主目录中的插件和技能；共享目录及 MCP 包缓存另列。\n\n扩展版本不进入清理候选，不用卸载整个插件代替回收旧版本。'),
            dict(route='resources', label='版本与桌面资源', size=f'{len(extras)} 项待测量',
                 next='核对 / 暂存',
                 detail='核对固定缓存、旧安装版本和更新残留。活动版本、上一版本、使用中及状态未知的资源保留。\n\n确认后暂存；与主目录重叠的明细不重复相加。'),
            dict(route='protected', label='其他受保护数据',
                 size=fmt(sum(r['bytes'] for r in groups if r['category'] not in
                              {'sessions','archives','worktrees','codex-projects','cache','logs','plugins'})),
                 next='保留', detail='数据库、快照、历史工具输出和用途未核实的数据只盘点。\n\n选择后可查看分类，不提供删除入口。'),
            dict(route='trash', label='暂存区', size='按条目',
                 next='恢复 / 永久清除',
                 detail='空格勾选条目，r 恢复，d 删除；输入 DELETE 后按 Enter 确认。同批次未勾选的内容保留。\n\n暂存区属于所有 Agent，永久删除不可恢复。'),
        ]
        if doc.get('read_only'):
            for row in self.rows:
                row['next'] = '只读'
        table = self.query_one(DataTable)
        index = table.cursor_row
        table.clear(columns=True)
        width = self.size.width - 8
        table.add_column('存储类别', width=min(27, max(14, width//3)))
        table.add_column('占用', width=20)
        if width >= 60:
            table.add_column('操作', width=max(16, width-min(27,width//3)-27))
        for i, r in enumerate(self.rows):
            values = [r['label'], r['size']] + ([r['next']] if width >= 60 else [])
            table.add_row(*(Text(v, no_wrap=True, overflow='ellipsis') for v in values), key=str(i))
        table.move_cursor(row=min(index, len(self.rows)-1))
        self.query_one('#open', Button).disabled = False

    @on(DataTable.RowSelected)
    @on(Button.Pressed, '#open')
    def open_selected(self):
        if self.operation_busy:
            return
        index = self.query_one(DataTable).cursor_row
        if 0 <= index < len(self.rows):
            route = self.rows[index]['route']
            if route == 'protected':
                body = Text('数据库与其他受保护数据\n\n')
                for r in self.snapshot['groups']:
                    if (self.agent_key in ('all',r['agent']) and r['category'] not in
                            {'sessions','archives','worktrees','codex-projects','cache','logs','plugins'}):
                        body.append(f'{NAMES[r["agent"]]} · {LABELS[r["category"]]} · {fmt(r["bytes"])}\n')
                self.push_screen(DetailScreen('受保护数据 · 只读', body))
                return
            if route=='sessions' and self.agent_key=='t3':
                self.notify('T3 会话状态保留；工作树产物可在下一项查看。')
                return
            self.exit((route, self.agent_key))

    @on(Button.Pressed, '#tabs Button')
    def tab(self, event):
        self.action_agent(event.button.id)

    def action_help(self):
        body = Text()
        index = self.query_one(DataTable).cursor_row
        if 0 <= index < len(self.rows):
            body.append(self.rows[index]['label']+'\n', style='bold')
            body.append(self.rows[index]['detail']+'\n\n')
        body.append('总览只读取占用。选择类别后查看清单，确认后才会操作。\n\n'
                    '主目录含共享块；外部资源单独计量，不与主目录重复相加。\n'
                    '依赖的共享空间在进入依赖页后测量；目录大小不等于可释放空间。\n'
                    '暂存释放 0 B，永久清除在暂存区完成。会话归档 / 删除交给 Codex。\n'
                    '子页面按 q 返回总览；操作后首页重新统计，未操作时复用结果。\n\n')
        if self.snapshot:
            body.append(self.snapshot.get('bytes_scope', '')+'\n\n')
            rules = self.snapshot.get('policy', {}).get('retention_days', {})
            if rules:
                body.append('当前保留天数：'+ ' · '.join(f'{LABELS[k]} {v} 天' for k,v in rules.items())+'\n\n')
            for path in self.snapshot['roots'].values():
                body.append(safe(path)+'\n')
            for path in self.snapshot['missing_roots']:
                body.append('\n未找到：'+safe(path), style=SOFT)
            for error in self.snapshot['errors']:
                body.append('\n'+safe(error), style=AMBER)
        self.push_screen(DetailScreen('使用与扫描范围', body))


class ReviewList(ManagedApp):
    CSS = SHARED_CSS
    BINDINGS = [('q','quit','返回总览'), ('enter','detail','详情')]
    def __init__(self, service, data):
        super().__init__()
        self.service, self.data = service, data
        self.rows = data['items']
        self.operation_results = []
    def compose(self):
        yield Static('CLEAN MY AGENT    /    REVIEW', id='brand')
        yield Static(self.TITLE, id='heading')
        yield Static(self.hint, id='context')
        yield Static('', id='warning', markup=False)
        yield DataTable(cursor_type='row', id='grid', show_row_labels=False)
        yield Static(self.keys, id='footer')
    def on_mount(self):
        self.fill()
        self.query_one(DataTable).focus()
    @on(DataTable.RowSelected)
    def action_detail(self):
        index = self.query_one(DataTable).cursor_row
        if 0 <= index < len(self.rows):
            self.push_screen(DetailScreen('完整清单', self.detail(self.rows[index])))
    def current(self):
        index = self.query_one(DataTable).cursor_row
        return self.rows[index] if 0 <= index < len(self.rows) else None
    def refreshed(self, data):
        self.data, self.rows = data, data['items']
        self.fill()


class FilesApp(ReviewList):
    TITLE = '缓存与旧日志'
    hint = '暂存可恢复 · 释放 0 B'
    keys = 'Space 勾选 · Ctrl+A 全选 · Enter 详情 · t 暂存 · q 返回'
    BINDINGS = ReviewList.BINDINGS + [('space','toggle','勾选'),('ctrl+a','all','全选'),('t','review','暂存'),('x','review','暂存')]
    def __init__(self, service, agent, data):
        super().__init__(service, data)
        self.agent_key, self.checked = agent, set()
    @staticmethod
    def identity(r):
        return r['source'], r['path']
    def fill(self):
        table = self.query_one(DataTable)
        index = table.cursor_row
        table.clear(columns=True)
        for name in ('选择','Agent','类别','目录计块','文件路径'):
            table.add_column(name)
        for r in self.rows:
            table.add_row('☑' if self.identity(r) in self.checked else '☐', agent_of(r['source']),
                          LABELS[r['category']],fmt(r['size']), Text(safe(str(Path(r['root'])/r['path']))))
        table.move_cursor(row=min(index,max(0,len(self.rows)-1)))
        self.query_one('#warning', Static).update('；'.join(map(safe,self.data['errors'])) or f'{len(self.rows)} 项 · 已选 {len(self.checked)}')
    def detail(self, r):
        return Text(safe(str(Path(r['root'])/r['path']))+'\n\n'+fmt(r['size'])+' · '+LABELS[r['category']])
    def action_toggle(self):
        r = self.current()
        if r:
            self.checked.symmetric_difference_update({self.identity(r)})
            self.fill()
    def action_all(self):
        self.checked = set() if len(self.checked)==len(self.rows) else {self.identity(r) for r in self.rows}
        self.fill()
    def action_review(self):
        if self.data['errors']:
            self.notify('扫描不完整，未开放暂存。')
            return
        chosen = [r for r in self.rows if self.identity(r) in self.checked]
        if not chosen:
            self.notify('请先用空格勾选文件。')
            return
        body = Text(f'暂存 {len(chosen)} 个文件 · 释放 0 B\n\n')
        for r in chosen:
            body.append(safe(str(Path(r['root'])/r['path']))+'  '+fmt(r['size'])+'\n')
        body.append('\n可从暂存区恢复。')
        def complete(result):
            if 'error' in result:
                return Text('未完成：'+safe(result['error']))
            self.checked.clear()
            if result['count']:
                self.operation_results.append(f'已暂存 {result["count"]} 个文件 · 批次 {result["batch"]}')
                self.rows = []  # Never keep actionable stale rows after a partial write.
                self.data = dict(items=[], errors=['本次操作结束；返回总览后可重新预览。'])
            return Text(f'已暂存 {result["count"]} 项，释放 0 B。\n批次：{result["batch"] or "无"}\n'
                        '返回总览 → 暂存区，可恢复或永久清除。\n'+'\n'.join(map(safe,result['errors'])))
        self.push_screen(ActionScreen('确认暂存缓存 / 日志', body,
            lambda p: self.service.stage_files(self.agent_key, chosen, p), complete, refresh=self.fill))


class BatchesApp(ManagedApp):
    TITLE = '暂存区'
    CSS = SHARED_CSS

    def __init__(self, service, data=None):
        super().__init__()
        self.service = service
        self.operation_results = []

    def on_mount(self):
        from .quarantine_actions import QuarantineActions
        from .vault_tui import VaultScreen
        self.push_screen(VaultScreen(QuarantineActions(self.service.roots, self.service.state)),
                         lambda _: self.exit())
