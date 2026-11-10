"""Optional resource browser; there are deliberately no mutation bindings."""
import sys


def describe(row, *, compact=False):
    from rich.text import Text
    from .terminal import safe, fmt
    from .resources import LABELS
    t = Text(overflow='fold')
    if compact:
        t.append(safe(row['name'])+'\n', style='bold')
        t.append(safe(row.get('version') or '版本未知')+'\n\n', style='#9ab4ac')
        if row.get('parent_path'):
            t.append('占用计入父插件\n\n')
        elif row.get('directory_bytes') is not None:
            t.append(fmt(row['directory_bytes'])+'\n\n')
        t.append(safe(row['reason']))
        if row.get('referenced_by'):
            t.append(f'\n\n{len(row["referenced_by"])} 处引用', style='#efc480')
        return t
    values = [('类型', LABELS[row['category']]), ('名称', row['name']), ('Agent', row['agent']),
              ('来源', row.get('source') or '本机已识别路径'), ('版本', row.get('version') or '未声明 / 未验证'),
              ('完整路径', row['path']), ('状态', row['reason'])]
    if row.get('declared_version'):
        values.append(('配置版本或范围', row['declared_version']))
    if row.get('parent_path'):
        values.append(('占用归属', '包含在父插件中：' + row['parent_path']))
    elif row.get('directory_bytes') is not None:
        values.append(('目录计块', fmt(row['directory_bytes'])))
    else:
        values.append(('目录计块', '未测量 / 仅配置'))
    if row.get('included_in_primary'):
        values.append(('统计范围', '在完整 scan 原有主目录统计范围内；不可重复相加'))
    if row.get('referenced_by'):
        values.append(('已知引用', '\n'.join(row['referenced_by'])))
    if row.get('duplicate_group'):
        values.append(('同组版本', row['duplicate_group']))
    if row.get('guidance'):
        values.append(('复核建议', row['guidance']))
    for label, value in values:
        t.append(label + '\n', style='#9ab4ac')
        t.append(safe(value) + '\n\n')
    t.append('只读盘点，不进入 clean / deps 暂存候选。', style='#efc480')
    return t


def make_screen(data):
    from rich.text import Text
    from textual import on, events
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import Screen
    from textual.widgets import Button, DataTable, Input, Static
    from .tui_common import DetailScreen
    from .resources import LABELS
    from .terminal import safe, fmt

    class ResourceScreen(Screen):
        BINDINGS = [('q', 'back', '返回'), ('escape', 'back', '返回'), ('slash', 'search', '搜索'),
                    ('1', 'filter("all")', '全部'), ('2', 'filter("multiple")', '多版本'),
                    ('3', 'filter("plugin")', 'Plugin'), ('4', 'filter("skill")', 'Skill'),
                    ('5', 'filter("mcp")', 'MCP'), ('question_mark', 'help', '范围和提示')]

        def __init__(self):
            super().__init__()
            self.kind = 'all'
            self.rows = []

        def compose(self):
            yield Static('窗口太小，请调整到至少 44 列 × 16 行。q 返回', id='too-small')
            yield Static('CLEAN MY AGENT    /    RESOURCES', id='brand')
            yield Static('资源与版本 · 只读', id='heading')
            with Horizontal(id='tabs'):
                for i, (key, name) in enumerate((('all','全部'),('multiple','多版本'),('plugin','Plugin'),('skill','Skill'),('mcp','MCP')), 1):
                    yield Button(f'{i} {name}', id=key, compact=True)
            yield Input(placeholder='搜索 agent、来源、名称、版本或路径', id='search')
            with Horizontal(id='layout'):
                with Vertical(id='listpane'):
                    yield Static(id='sortline')
                    yield DataTable(cursor_type='row', id='grid', show_row_labels=False)
                with VerticalScroll(id='side'):
                    yield Static('当前选项', id='sidetitle')
                    yield Static(id='preview', markup=False)
            yield Static('Enter 详情 · / 搜索 · ? 帮助 · q 返回', id='footer')

        def on_mount(self):
            self.fill()
            self.query_one(DataTable).focus()

        def on_resize(self, event: events.Resize):
            self.set_class(event.size.width < 110, 'narrow')
            self.set_class(event.size.width < 60, 'small')
            self.set_class(event.size.width < 44 or event.size.height < 16, 'tiny')
            if self.is_mounted:
                self.fill()

        def fill(self):
            for button in self.query('#tabs Button'):
                button.set_class(button.id == self.kind, 'chosen')
            query = self.query_one(Input).value.casefold()
            self.rows = [r for r in data['entries'] if
                         (self.kind == 'all' or self.kind == r['category'] or
                          self.kind == 'multiple' and r.get('duplicate_group')) and
                         query in ' '.join(str(r.get(k) or '') for k in ('name','version','path','agent','source')).casefold()]
            table = self.query_one(DataTable)
            table.clear(columns=True)
            available = self.size.width - (43 if self.size.width >= 110 else 8)
            table.add_column('Agent', width=7)
            table.add_column('类型', width=8)
            table.add_column('名称 / 来源', width=max(8, available - 45))
            table.add_column('版本', width=15)
            if available >= 55:
                table.add_column('目录计块', width=10)
            for i, r in enumerate(self.rows):
                fields = [r['agent'], LABELS[r['category']], r['name'] + (' @' + r['source'] if r.get('source') else ''),
                          r.get('version') or '未知']
                if available >= 55:
                    fields.append(fmt(r['directory_bytes']) if r['directory_bytes'] is not None else '—')
                table.add_row(*(Text(safe(s), no_wrap=True, overflow='ellipsis') for s in fields), key=str(i))
            errors = len(data.get('errors', []))
            self.query_one('#sortline', Static).update(f'{len(self.rows)} 项'+(f' · {errors} 条提示 [?]' if errors else ''))
            self.preview()

        def preview(self):
            table = self.query_one(DataTable)
            self.query_one('#preview', Static).update(describe(self.rows[table.cursor_row], compact=True)
                if 0 <= table.cursor_row < len(self.rows) else '无匹配资源')

        @on(DataTable.RowHighlighted)
        def selected(self):
            self.preview()

        @on(DataTable.RowSelected)
        def detail(self):
            if 0 <= self.query_one(DataTable).cursor_row < len(self.rows):
                r = self.rows[self.query_one(DataTable).cursor_row]
                self.app.push_screen(DetailScreen('资源详情 · 只读', describe(r)))

        @on(Button.Pressed)
        def tab(self, event):
            self.action_filter(event.button.id)

        def action_filter(self, kind):
            self.kind = kind
            self.fill()

        def action_search(self):
            self.query_one(Input).add_class('open')
            self.query_one(Input).focus()

        def action_help(self):
            self.app.push_screen(DetailScreen('盘点范围与提示', Text(
                data.get('scope', '本机已识别资源，只读盘点。') + '\n\n' +
                data['totals_note'] + '\n\n' +
                '\n'.join(safe(e) for e in data.get('errors', [])))))

        @on(Input.Changed)
        def changed(self):
            self.fill()

        @on(Input.Submitted)
        def submitted(self):
            self.query_one(DataTable).focus()

        def action_back(self):
            search = self.query_one(Input)
            if search.value or search.has_class('open'):
                search.value = ''
                search.remove_class('open')
                self.query_one(DataTable).focus()
            elif len(self.app.screen_stack) > 2:
                self.app.pop_screen()
            elif getattr(self.app, 'resource_only', False):
                self.app.exit()
            else:
                self.app.pop_screen()

    return ResourceScreen()


def browse(ui, data):
    if not (ui.active and sys.stdin.isatty()):
        return False
    try:
        from .tui_common import ManagedApp, SHARED_CSS
    except ImportError:
        return False

    class ResourceApp(ManagedApp):
        CSS = SHARED_CSS
        TITLE = 'Clean My Agent · 资源版本'
        resource_only = True
        def on_mount(self):
            self.push_screen(make_screen(data))
    ResourceApp().run()
    return True
