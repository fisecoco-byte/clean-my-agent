"""Session table, review list and native Codex actions in the shared Textual shell."""
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Static

from .inventory import SECTIONS, plan
from . import activity
from .pie import pie_lines
from .policy import SESSION_DAYS
from .session_titles import UNTITLED
from .terminal import NAMES, safe
from .storage_view import size
from .tui_common import ActionScreen, DetailScreen, ManagedApp, SHARED_CSS

MINT, AMBER, SOFT = '#7de2bf', '#efc480', '#9ab4ac'
STATE = {'open':'未归档', 'archived':'已归档', 'unverified':'状态未核实', 'unknown':'人工复核'}


def session_detail(row, agent, *, compact=False):
    t = Text()
    if compact:
        t.append(safe(row['title'] or UNTITLED)+'\n\n', style='bold')
        t.append(row['session_id']+'\n\n', style=SOFT)
        t.append(f'{STATE[row["state"]]} · {row["idle_days"]} 天未活跃\n{size(row["bytes"])}\n\n活跃度 {activity.badge(row)}')
        if row.get('hold_reason'):
            t.append('\n\n'+safe(row['hold_reason']), style=AMBER)
        return t
    for label, value in [('标题 / 摘要', row['title'] or UNTITLED), ('完整会话 ID', row['session_id']),
                         ('Agent', NAMES[agent]), ('状态', STATE[row['state']]),
                         ('未活跃', str(row['idle_days']) + ' 天'), ('会话占用', size(row['bytes']))]:
        t.append(label + '\n', style=SOFT)
        t.append(safe(value) + '\n\n')
    t.append('活跃度\n' + safe(activity.describe(row)) + '\n\n')
    if row.get('hold_reason'):
        t.append('保留原因\n' + safe(row['hold_reason']), style=AMBER)
    elif agent == 'claude':
        t.append('Claude 会话仅供人工复核，本面板不执行归档或删除。', style=AMBER)
    return t


class SessionApp(ManagedApp):
    TITLE = 'Clean My Agent · 会话管理'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [('q','quit','退出'), ('1',"agent('codex')",'Codex'), ('2',"agent('claude')",'Claude'),
                ('a',"mode('archive')",'归档'), ('d',"mode('delete')",'删除'), ('v',"mode('all')",'全部'),
                ('space','toggle','勾选'), ('ctrl+a','select_visible','勾选筛选结果'), ('x','review','操作清单'),
                ('slash','search','搜索'), ('escape','reset','清除'), ('r','reload','刷新'),
                ('i','overview','分布'), ('s','sort','排序'), ('question_mark','help','帮助')]
    CSS = SHARED_CSS + """
    #modes, #thresholds, #actions { height: auto; }
    #modes Button, #thresholds Button, #actions Button {
        width: auto; min-width: 0; height: 1; border: none; margin: 0 1 1 0;
        padding: 0 1; background: #233234; color: #b3c5bf;
    }
    #modes Button.chosen { color: #111b1d; background: #83d9c0; text-style: bold; }
    #review { background: #7de2bf; color: #10251e; }
    #review:disabled { background: #233234; color: #738c83; }
    .short #heading, .short #context, .short #thresholds { display: none; }
    """

    def __init__(self, doc, rows, *, actions, details=False):
        super().__init__()
        self.doc, self.all_rows, self.actions = doc, rows, actions
        self.agent_key = next(iter(doc['agents']))
        self.rules = {key: data['policy'].copy() for key, data in doc['agents'].items()}
        self.mode, self.filter_text = 'archive', ''
        self.checked = set()
        self.rows = []
        self.operation_results = []
        self.show_details = details
        self.sort_mode = 'cold'

    def compose(self) -> ComposeResult:
        yield Static('窗口太小，请调整到至少 44 列 × 16 行。q 退出', id='too-small')
        yield Static('CLEAN MY AGENT    /    SESSIONS', id='brand')
        yield Static('会话管理', id='heading')
        with Horizontal(id='tabs'):
            for key in self.doc['agents']:
                yield Button(('1 ' if key == 'codex' else '2 ') + NAMES[key], id=key, compact=True)
        with Horizontal(id='modes'):
            yield Button('a 待归档', id='archive', compact=True)
            yield Button('d 待删除', id='delete', compact=True)
            yield Button('v 全部', id='all', compact=True)
        with Horizontal(id='thresholds'):
            yield Button('', id='archive_days', compact=True)
            yield Button('', id='delete_days', compact=True)
        yield Input(placeholder='搜索标题或 ID', id='search')
        yield Static('', id='warning', markup=False)
        with Horizontal(id='layout'):
            with Vertical(id='listpane'):
                yield Static(id='sortline', markup=False)
                yield Static(id='empty')
                yield DataTable(cursor_type='row', id='grid', show_row_labels=False)
            with VerticalScroll(id='side'):
                yield Static('当前会话', id='sidetitle')
                yield Static(id='preview', markup=False)
        with Horizontal(id='actions'):
            yield Button('x 操作清单', id='review', compact=True, disabled=True)
            yield Button('r 刷新', id='reload', compact=True)
            yield Button('i 活跃分布', id='distribution', compact=True)
            yield Button('s 排序', id='sort', compact=True)
        yield Static('Space 勾选 · Enter 详情 · / 搜索 · ? 帮助 · '+
                     ('q 返回' if self.return_to_dashboard else 'q 退出'), id='footer')

    def on_mount(self):
        self.refresh_list()
        self.query_one(DataTable).focus()
        if self.show_details:
            self.action_overview()

    def on_resize(self, event: events.Resize):
        base = self.screen_stack[0]
        base.set_class(event.size.width < 44 or event.size.height < 16, 'tiny')
        base.set_class(event.size.width < 110, 'narrow')
        base.set_class(event.size.width < 60, 'small')
        base.set_class(event.size.height < 24, 'short')
        if self.is_mounted and self.screen is base and self.query(DataTable):
            self.call_after_refresh(self.refresh_list, True)

    def plans(self):
        rows = self.all_rows[self.agent_key]
        return plan(rows, **self.rules[self.agent_key], limit=len(rows))

    def selectable(self, row):
        return (getattr(self.actions, 'writes_enabled', True) and self.agent_key == 'codex'
                and self.mode in ('archive','delete') and row['route'] == self.mode)

    def refresh_list(self, preserve=False):
        if self.screen is not self.screen_stack[0]:
            return
        table = self.query_one(DataTable)
        index = table.cursor_row if preserve else 0
        selected = self.rows[index]['session_id'] if preserve and 0 <= index < len(self.rows) else None
        plans = self.plans()
        rows = self.all_rows[self.agent_key] if self.mode == 'all' else plans[self.mode]['entries']
        words = self.filter_text.casefold().split()
        self.rows = [r for r in rows if all(w in ((r['title'] or '') + ' ' + r['session_id']).casefold() for w in words)]
        self.rows.sort(key=(lambda r: (-r['bytes'], r['session_id'])) if self.sort_mode == 'size'
                       else lambda r: activity.sort_key(r, self.sort_mode))
        if selected:
            index = next((i for i,r in enumerate(self.rows) if r['session_id'] == selected), 0)
        for key in self.doc['agents']:
            self.query_one('#'+key, Button).set_class(key == self.agent_key, 'chosen')
        for mode in ('archive','delete','all'):
            button = self.query_one('#'+mode, Button)
            button.set_class(mode == self.mode, 'chosen')
            label = {'archive':'a 待归档', 'delete':'d 待删除', 'all':'v 全部'}[mode]
            count = len(self.all_rows[self.agent_key]) if mode == 'all' else plans[mode]['count']
            button.label = f'{label} {count}'
        for action in ('archive','delete'):
            self.query_one('#'+action+'_days', Button).label = ('归档' if action == 'archive' else '删除') + f" ≥{self.rules[self.agent_key][action+'_days']} 天"
        errors = self.doc['agents'][self.agent_key]['errors']
        warning = self.query_one('#warning', Static)
        readonly = not getattr(self.actions, 'writes_enabled', True)
        warning.display = bool(errors) or self.agent_key == 'claude' or readonly
        warning.update('盘点不完整 · 操作暂停 [?]' if errors else 'Windows · 只读' if readonly else 'Claude Code · 只读')
        table.clear(columns=True)
        width = max(20, self.size.width - (41 if self.size.width >= 110 else 6))
        with_id = width >= 95
        columns = [('选', 3), ('标题 / 摘要', 0)]
        if with_id:
            columns.append(('完整会话 ID', 36))
        columns.append(('活跃度', 12))
        if width >= 45:
            columns.append(('状态', 10))
        columns[1] = (columns[1][0], max(9, width-sum(w for _,w in columns)-len(columns)*2-1))
        for label, column_width in columns:
            table.add_column(label, width=column_width)
        for n, row in enumerate(self.rows):
            mark = '☑' if row['session_id'] in self.checked else '☐' if self.selectable(row) else '—'
            values = [Text(mark, style=MINT), Text(safe(row['title'] or UNTITLED), no_wrap=True, overflow='ellipsis')]
            if with_id:
                values.append(Text(row['session_id']))
            values.append(Text(activity.badge(row), style=activity.COLORS[activity.value(row).get('level','unknown')]))
            if width >= 45:
                values.append(Text(STATE[row['state']], style=SOFT))
            table.add_row(*values, key=str(n))
        index = max(0, min(index, len(self.rows)-1))
        table.move_cursor(row=index)
        self.preview(index)
        self.query_one('#empty').display = not self.rows
        self.query_one('#empty', Static).update('无匹配会话')
        self.query_one('#sortline', Static).update(f'{len(self.rows)} 项 · 已选 {len(self.checked)} · {activity.SORT_LABELS[self.sort_mode]}')
        button = self.query_one('#review', Button)
        button.disabled = not self.checked or self.agent_key != 'codex'
        label = '归档' if self.mode == 'archive' else '永久删除'
        button.label = f'x {label} ({len(self.checked)})' if self.checked else 'x 操作清单'

    def preview(self, index):
        row = self.rows[index] if 0 <= index < len(self.rows) else None
        self.query_one('#preview', Static).update(session_detail(row, self.agent_key, compact=True) if row else '无选中会话')
        self.query_one('#side', VerticalScroll).scroll_home(animate=False)

    @on(DataTable.RowHighlighted)
    def highlighted(self, event):
        self.preview(event.cursor_row)

    @on(DataTable.RowSelected)
    def selected(self, event):
        if 0 <= event.cursor_row < len(self.rows):
            self.push_screen(DetailScreen('会话详情', session_detail(self.rows[event.cursor_row], self.agent_key)))

    @on(Button.Pressed)
    def buttons(self, event):
        key = event.button.id
        if key in ('codex','claude'): self.action_agent(key)
        elif key in ('archive','delete','all'): self.action_mode(key)
        elif key in ('archive_days','delete_days'):
            values = self.rules[self.agent_key]
            values[key] = SESSION_DAYS[(SESSION_DAYS.index(values[key])+1) % len(SESSION_DAYS)]
            self.checked.clear()
            self.refresh_list()
        elif key == 'review': self.action_review()
        elif key == 'reload': self.action_reload()
        elif key == 'distribution': self.action_overview()
        elif key == 'sort': self.action_sort()

    def action_sort(self):
        if self.screen is not self.screen_stack[0] or self.operation_busy:
            return
        modes = ('cold', 'hot', 'size')
        self.sort_mode = modes[(modes.index(self.sort_mode)+1) % len(modes)]
        self.refresh_list(True)

    def action_agent(self, key):
        if key in self.doc['agents']:
            self.agent_key = key
            self.checked.clear()
            self.refresh_list()
            self.query_one(DataTable).focus()

    def action_mode(self, mode):
        self.mode = mode
        self.checked.clear()
        self.refresh_list()
        self.query_one(DataTable).focus()

    def action_toggle(self):
        index = self.query_one(DataTable).cursor_row
        if not 0 <= index < len(self.rows) or not self.selectable(self.rows[index]):
            self.notify('请在 Codex 的待归档或待删除页选择候选。')
            return
        sid = self.rows[index]['session_id']
        self.checked.symmetric_difference_update({sid})
        self.refresh_list(True)

    def action_select_visible(self):
        selected = {r['session_id'] for r in self.rows if self.selectable(r)}
        self.checked = set() if self.checked == selected else selected
        self.refresh_list(True)

    def action_search(self):
        search = self.query_one('#search', Input)
        search.add_class('open')
        search.focus()

    @on(Input.Changed, '#search')
    def filter_changed(self, event):
        self.filter_text = event.value
        self.checked.clear()
        self.refresh_list()

    @on(Input.Submitted, '#search')
    def finish_filter(self):
        self.query_one(DataTable).focus()

    def action_reset(self):
        search = self.query_one('#search', Input)
        search.value = ''
        search.remove_class('open')
        self.checked.clear()
        self.refresh_list(True)
        self.query_one(DataTable).focus()

    def action_review(self):
        if self.screen is not self.screen_stack[0] or self.operation_busy:
            return
        selected = [r.copy() for r in self.rows if r['session_id'] in self.checked and self.selectable(r)]
        if not selected or self.agent_key != 'codex':
            self.notify('请先用空格勾选 Codex 候选会话。')
            return
        operation = self.mode
        label = '归档' if operation == 'archive' else '永久删除'
        body = Text(f'即将{label} {len(selected)} 个 Codex 会话\n\n', style='bold')
        for row in selected:
            body.append(safe(row['title'] or UNTITLED) + '\n')
            body.append(row['session_id'] + f"  ·  {row['idle_days']} 天未活跃\n", style=SOFT)
            body.append(f"codex {operation} {row['session_id']}" + (' --force' if operation == 'delete' else '') + '\n\n')
        body.append('通过 Codex 执行，操作前复核状态。\n')
        if operation == 'delete':
            body.append('永久删除，不可恢复。', style=AMBER)
        rules = {k: v.copy() for k, v in self.rules.items()}
        def execute(progress):
            result = self.actions.apply(operation, selected, rules, progress)
            try:
                result['snapshot'] = self.actions.refresh(rules, progress)
            except Exception as exc:
                result['refresh_error'] = str(exc)
            return result
        self.push_screen(ActionScreen(f'确认{label} · 操作清单', body, execute, self.operation_done,
                                      phrase='DELETE' if operation == 'delete' else None,
                                      refresh=self.refresh_list))

    def operation_done(self, result):
        self.checked.clear()
        text = Text()
        if 'error' in result:
            return Text('未完成：' + safe(result['error']), style=AMBER)
        if 'snapshot' in result:
            self.doc, self.all_rows = result['snapshot']
        for row in result.get('results', []):
            label = {'succeeded':'已完成', 'skipped':'已跳过', 'failed':'失败', 'uncertain':'待核对'}[row['status']]
            entry = f"{label} · {safe(row['title'] or UNTITLED)}\n{row['session_id']}\n{safe(row['message'])}\n"
            self.operation_results.append(entry)
            text.append(entry+'\n', style=MINT if row['status']=='succeeded' else AMBER)
        if result.get('refresh_error'):
            text.append('刷新失败：' + safe(result['refresh_error']) + '\n列表仍为旧数据，请按 r 重新盘点。', style=AMBER)
        return text if text.plain else Text('已刷新会话列表。')

    def action_reload(self):
        if self.screen is not self.screen_stack[0] or self.operation_busy:
            return
        rules = {k:v.copy() for k,v in self.rules.items()}
        self.push_screen(ActionScreen('刷新会话', Text('正在重新盘点…'),
            lambda p: dict(snapshot=self.actions.refresh(rules, p)), self.operation_done,
            refresh=self.refresh_list, auto_run=True))

    def action_overview(self):
        data = self.doc['agents'][self.agent_key]
        distribution = (' · '.join(f'{activity.LABELS[k]} {v}' for k,v in data['activity_levels'].items())
                        if data.get('activity_levels') else '未活跃天数分布')
        text = Text(f"{NAMES[self.agent_key]} · {data['session_count']} 个会话\n{distribution}\n\n", style='bold')
        colors = {'slice0':'cyan','slice1':'green','slice2':'yellow','slice3':'magenta','slice4':'red', 'normal':''}
        for line in pie_lines(data['histogram'], min(78, max(20, self.size.width-12))):
            for content, style in line:
                text.append(content, style=colors.get(style, style))
            text.append('\n')
        for name, group in data['groups'].items():
            text.append(f"\n{SECTIONS[name]} · {group['count']} 项 · {size(group['bytes'])}\n")
            for item in group['entries']:
                text.append(safe(item['path'])+'\n', style=SOFT)
        self.push_screen(DetailScreen('会话分布与明细', text))

    def action_help(self):
        text = Text('1 / 2   切换 Codex / Claude Code\na / d / v   待归档 / 待删除 / 全部\n'
                    '点击天数按钮   调整 5 / 10 / 15 / 20 天阈值\n'
                    '空格   勾选当前会话\nCtrl+A   勾选或取消当前筛选结果中的全部候选\n'
                    's   活跃度升序 / 降序 / 占用排序；勾选保持不变\n' + activity.LEGEND + '\n'
                    'x   查看清单并确认操作\nEnter   查看完整 ID 和标题\n/   搜索标题或 ID\n'
                    'i   活跃分布与文件明细\nr   刷新\nEsc   清除搜索和勾选\nq   返回 / 退出\n\n'
                    '弹窗中 Esc / q 关闭帮助或返回列表。\n'
                    '切换 agent、分类、天数或搜索时会清空勾选。\n'
                    '待删除仅包含已归档且达到删除阈值的会话。\n'
                    'Claude 仅人工复核；Codex 通过原生命令管理会话。\n'
                    '选择与浏览不会修改文件，确认后才执行。')
        for error in self.doc['agents'][self.agent_key]['errors']:
            text.append('\n'+safe(error), style=AMBER)
        self.push_screen(DetailScreen('帮助', text))
