"""Textual dependency browser with explicitly confirmed quarantine actions."""
from pathlib import Path
from rich.text import Text
from textual import on, events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Static

from .scan import LABELS
from .storage_view import GROUPS, LABEL, exact, groups, ordered, project, release, size, row_label
from .terminal import pad, safe
from .tui_common import ActionScreen, DetailScreen, ManagedApp, SHARED_CSS

MINT, AMBER, SOFT = '#7de2bf', '#efc480', '#9ab4ac'


def details(row, *, compact=False):
    t = Text(overflow='fold')
    def field(label, value, color=''):
        t.append(label + '\n', style=SOFT)
        t.append(value + '\n\n', style=color)
    link = row['kind'] == 'link_group'
    field('链接目标' if link else '项目 / 资源', row_label(row), 'bold')
    if not compact:
        field('完整路径', safe(row['path']))
        if row.get('ecosystem'):
            field('类型', safe(row['ecosystem']+' · '+row.get('artifact_category', row.get('category', ''))))
        if row.get('rebuild'):
            field('重建方式', safe(row['rebuild']))
    if not link:
        field('预计释放 · 单独永久删除', release(row) if compact else (
            exact(row,'reclaimable_bytes') if row['reclaimable_bytes'] is not None else
            (f"{row['reclaimable_min_bytes']:,} 至 {row['reclaimable_max_bytes']:,} B"
             if None not in (row['reclaimable_min_bytes'], row['reclaimable_max_bytes']) else '未知')), MINT)
        if not compact:
            field('目录大小', exact(row, 'directory_bytes'))
            field('共享占用', exact(row,'shared_bytes'))
            if row['unknown_bytes']:
                field('未确定是否共享', exact(row, 'unknown_bytes'))
                field('已确认共享', exact(row, 'known_shared_bytes'))
        elif row['directory_bytes'] and row['shared_bytes'] is not None:
            fraction = row['shared_bytes'] / row['directory_bytes']
            blocks = min(22, max(0, round(22*fraction)))
            t.append('共享占目录计块\n', style=SOFT)
            t.append('━'*blocks,style='#67a9a3')
            t.append('┄'*(22-blocks),style='#586969')
            t.append(f'\n{size(row["shared_bytes"])} / {size(row["directory_bytes"])} · {fraction:.1%}\n\n')
        status = {'candidate':'可暂存', 'retained':'保留', 'unmeasured':'未测量'}[row['status']]
        why = '' if row['referenced_by'] else safe(row.get('reason') or '')
        field('策略', status + (' · ' + why if why else ''), AMBER if row['status']=='retained' else '')
        if not compact:
            for label, key in [('安装检查', 'installation'), ('运行检查', 'runtime')]:
                if row.get(key):
                    field(label, safe(row[key]['reason']))
    else:
        field('受管区域的软链接', str(len(row['sources'])) + ' 个 · 不重复计算目标大小')
    refs = row['referenced_by']
    if refs:
        t.append(f'引用来源  ·  {len(refs)}\n', style=AMBER)
        for p in (refs[:3] if compact else refs):
            t.append(('  ' + project(p) if compact else safe(p)) + '\n')
        if compact and len(refs)>3:
            t.append(f'  … 另有 {len(refs)-3} 项\n', style=SOFT)
    return t


def scan_details(doc):
    t = Text(overflow='fold')
    t.append(f"主目录计块  {size(doc['bytes'])}     常规可清理  {size(doc['eligible_bytes'])}\n\n",style=MINT)
    if doc.get('agent_resources'):
        t.append('额外资源与扩展版本另列；返回后按 4 查看。\n\n')
    for row in doc['groups']:
        t.append(f"{row['agent']:6}  {size(row['bytes']):>11}   {LABELS.get(row['category'],row['category'])}\n")
    if doc.get('largest_groups'):
        t.append('\n占用最大的目录或文件组\n',style='bold')
        for item in doc['largest_groups']:
            t.append(f"{size(item['bytes']):>11}   {safe(item['path'])}\n")
    if doc['missing_roots']:
        t.append('\n未找到：' + '\n'.join(safe(x) for x in doc['missing_roots']))
    return t




class StorageApp(ManagedApp):
    TITLE = 'Clean My Agent · 存储盘点'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [('q','quit','退出'), ('1','group(0)','候选'), ('2','group(1)','保留'),
                ('3','group(2)','链接'), ('slash','search','筛选'), ('escape','reset','返回'),
                ('s','sort','排序'), ('space','toggle','勾选'), ('ctrl+a','select_all','全选'), ('t','stage','暂存清单'), ('v','vault','暂存区'), ('w','worktrees','工作树'), ('0','overview','概览'), ('4','resources','资源'), ('question_mark','help','帮助')]

    CSS = SHARED_CSS + '''
        #storage-actions { height: 1; }
        #vault { height: 1; width: auto; min-width: 0; border: none; padding: 0 1; margin-left: 2; }
    '''
    def __init__(self, report, *, scan=None, source=None, actions=None, start_worktrees=False):
        super().__init__()
        self.report, self.scan_doc, self.source = report, scan, source
        self.buckets = groups(report)
        self.category = next((k for k in GROUPS if self.buckets[k]), 'candidate')
        self.by_size, self.filter_text = False, ''
        self.rows = []
        self.actions = actions if not source else None
        self.operation_results = []
        self.checked = set()
        self.worktree_scope = None
        self.start_worktrees = start_worktrees

    def compose(self) -> ComposeResult:
        yield Static('窗口太小，请调整到至少 44 列 × 16 行。q 退出', id='too-small')
        yield Static('CLEAN MY AGENT    /    STORAGE', id='brand')
        yield Static(self.report.get('title', '工作树 / 产物'), id='heading')
        if self.report.get('projects'):
            yield Button(f'w 工作树 {len(self.report["projects"])}', id='worktrees', compact=True)
        yield Static(id='context', markup=False)
        with Horizontal(id='tabs'):
            for i, group in enumerate(GROUPS, 1):
                count = (sum(len(r['sources']) for r in self.buckets[group]) if group=='links'
                         else len(self.buckets[group]))
                yield Button(f'{i} {LABEL[group]} {count}', id=group, compact=True)
            if self.scan_doc and self.scan_doc.get('agent_resources'):
                yield Button('4 资源 / 版本', id='resources', compact=True)
        yield Input(placeholder='搜索项目、产物或语言', id='search')
        yield Static(id='warning', markup=False)
        with Horizontal(id='layout'):
            with Vertical(id='listpane'):
                yield Static(id='sortline')
                yield Static(id='empty')
                yield DataTable(cursor_type='row', id='grid', show_row_labels=False)
            with VerticalScroll(id='side'):
                yield Static('当前选项', id='sidetitle')
                yield Static(id='preview', markup=False)
        yield Static('释放估计不可相加 · 暂存释放 0 B', id='legend')
        with Horizontal(id='storage-actions'):
            yield Button('t 暂存清单', id='stage', compact=True, disabled=True)
            yield Button('v 暂存区', id='vault', compact=True, disabled=self.actions is None)
        yield Static(id='footer')

    def on_mount(self):
        errors = self.report['errors'] if not self.scan_doc else self.scan_doc['errors']
        warning = self.query_one('#warning', Static)
        warning.display = bool(errors)
        warning.update(f'盘点不完整 · {len(errors)} 条提示 [?]')
        ctx = ('历史快照  ·  ' + safe(self.source) if self.source else
               '')
        if self.scan_doc:
            ctx += f"  /  目录计块 {size(self.scan_doc['bytes'])}  ·  常规可清理 {size(self.scan_doc['eligible_bytes'])}"
        self.query_one('#context', Static).update(ctx)
        self.query_one('#context').display = bool(ctx)
        self.refresh_list()
        self.query_one(DataTable).focus()
        if self.start_worktrees and self.report.get("projects"):
            self.call_after_refresh(self.action_worktrees)

    def on_resize(self, event: events.Resize):
        base = self.screen_stack[0]
        base.set_class(event.size.width < 44 or event.size.height < 16, 'tiny')
        base.set_class(event.size.width < 110, 'narrow')
        base.set_class(event.size.width < 60, 'small')
        base.set_class(event.size.height < 24, 'short')
        if self.is_mounted and self.screen is base and self.query(DataTable):
            self.call_after_refresh(self.refresh_list, True)

    def refresh_list(self, preserve=False):
        if self.screen is not self.screen_stack[0]:
            return
        table = self.query_one(DataTable)
        index = table.cursor_row if preserve else 0
        self.rows = ordered(self.buckets[self.category], self.category, self.by_size, self.filter_text)
        for group in GROUPS:
            self.query_one('#'+group, Button).set_class(group==self.category, 'chosen')
            count = (sum(len(r['sources']) for r in self.buckets[group]) if group == 'links' else len(self.buckets[group]))
            self.query_one('#'+group, Button).label = f'{GROUPS.index(group)+1} {LABEL[group]} {count}'
        table.clear(columns=True)
        width = max(16, self.size.width - (41 if self.size.width >= 110 else 6))
        link = self.category=='links'
        cols = ([('目标项目',0),('链接',5),('引用',5)] if link else
                [('项目 / 资源',0),('预计释放',13), *([('目录大小',11)] if width>=53 else []),
                 *([('共享占用',11)] if width>=70 else [])])
        cols[0] = (cols[0][0], max(9, width - sum(w for _,w in cols) - len(cols)*2 - 1))
        for j,(label,w) in enumerate(cols):
            table.add_column(Text(pad(label,w,True) if j else label), width=w)
        for i, row in enumerate(self.rows):
            nums = ([str(len(row['sources'])),str(len(row['referenced_by']))] if link else
                    [release(row),size(row['directory_bytes']),size(row['shared_bytes'])])
            mark = ('☑ ' if row['path'] in self.checked else '☐ ') if self.actions is not None and hasattr(self.actions,'stage_many') and row.get('status')=='candidate' else ''
            table.add_row(Text(mark+row_label(row),no_wrap=True,overflow='ellipsis'),
                          *[Text(pad('范围 · 详情' if j==0 and not link and '–' in n and len(n)>cols[1][1] else n,
                                     cols[j+1][1],True),style=MINT if j==0 and not link else '',
                                 no_wrap=True,overflow='ellipsis') for j,n in enumerate(nums[:len(cols)-1])],
                          key=str(i))
        idx = max(0,min(index,len(self.rows)-1))
        table.move_cursor(row=idx)
        self.preview(idx)
        empty = self.query_one('#empty', Static)
        empty.display = not self.rows
        empty.update('无匹配项 · Esc 清除' if self.filter_text else '暂无记录')
        sort = ('同一目标的链接合并显示' if link else '按目录大小 ↓' if self.by_size else '按预计释放 ↓（范围取下限）')
        self.query_one('#sortline', Static).update(Text(f'{len(self.rows)} 项  ·  已选 {len(self.checked)}  ·  {sort}'))
        self.query_one('#footer', Static).update(
            'Space 勾选 · Enter 详情 · / 搜索 · '+('s 排序 · ' if self.size.width>=74 else '')+
            ('0 概览 · 4 资源 · ' if self.scan_doc else '')+'? 帮助 · '+
            ('q 返回' if self.return_to_dashboard else 'q 退出'))

    def preview(self, idx):
        widget = self.query_one('#preview', Static)
        widget.update(details(self.rows[idx],compact=True) if 0<=idx<len(self.rows) else '无选中项')
        self.query_one('#side',VerticalScroll).scroll_home(animate=False)
        self.query_one('#stage', Button).disabled = not (self.checked or self.can_stage(idx))

    @on(Button.Pressed, '#vault')
    def action_vault(self):
        if self.screen is not self.screen_stack[0] or self.operation_busy or self.actions is None:
            return
        from .vault_tui import VaultScreen
        self.push_screen(VaultScreen(self.actions.quarantine()))

    @on(Button.Pressed, '#worktrees')
    def action_worktrees(self):
        if self.operation_busy or self.screen is not self.screen_stack[0]:
            return
        from .worktree_tui import WorktreeScreen
        actions = self.actions if self.actions is not None and hasattr(self.actions,'worktree_details') else None
        self.push_screen(WorktreeScreen(self.report,actions),self.select_worktree)

    def select_worktree(self,row):
        if row:
            self.worktree_scope = Path(row['path'])
            self.checked.clear()
            self.filter_text = ''
            self.query_one(Input).value = ''
        self.update_worktree_data()
        self.call_after_refresh(self.refresh_list)

    def update_worktree_data(self):
        entries = self.report['entries']
        if self.worktree_scope:
            entries = [r for r in entries if self.worktree_scope in Path(r['path']).parents]
        self.buckets = groups(dict(self.report,entries=entries))
        self.category = next((k for k in GROUPS if self.buckets[k]),'candidate')
        self.checked.intersection_update(r['path'] for r in entries)
        self.scan_doc = None
        if self.query('#worktrees'):
            self.query_one('#worktrees',Button).label = f'w 工作树 {len(self.report.get("projects",[]))}'
        self.query_one('#heading',Static).update('产物 · '+project(str(self.worktree_scope)) if self.worktree_scope else self.report.get('title','工作树 / 产物'))

    def action_toggle(self):
        if self.screen is not self.screen_stack[0] or not self.actions or not hasattr(self.actions,'stage_many'):
            return
        index = self.query_one(DataTable).cursor_row
        if self.can_stage(index):
            path = self.rows[index]['path']
            self.checked.symmetric_difference_update({path})
            self.refresh_list(True)

    def action_select_all(self):
        if self.screen is not self.screen_stack[0] or not self.actions or not hasattr(self.actions,'stage_many'):
            return
        paths = {r['path'] for i,r in enumerate(self.rows) if self.can_stage(i)}
        self.checked = set() if self.checked==paths else paths
        self.refresh_list(True)

    def can_stage(self, idx):
        return (self.actions is not None and not self.report['errors']
                and not (self.scan_doc and self.scan_doc['errors']) and 0 <= idx < len(self.rows)
                and self.rows[idx]['kind'] in ('directory', 'file') and self.rows[idx]['status'] == 'candidate')

    @on(Button.Pressed, '#stage')
    def action_stage(self):
        if self.screen is not self.screen_stack[0] or self.operation_busy:
            return
        idx = self.query_one(DataTable).cursor_row
        if not self.checked and not self.can_stage(idx):
            self.notify('请选择实时报告中的候选。')
            return
        chosen = [r.copy() for r in self.rows if r['path'] in self.checked] if self.checked else [self.rows[idx].copy()]
        body = Text(f'已选 {len(chosen)} 项 · 暂存可恢复 · 释放 0 B\n\n',style=AMBER)
        for row in chosen:
            body.append(row_label(row)+'\n',style='bold')
            body.append(safe(row['path'])+'\n目录大小 '+size(row['directory_bytes'])+'\n\n',style='')
        def execute(progress):
            return self.actions.stage(chosen[0],progress) if len(chosen)==1 else self.actions.stage_many(chosen,progress)
        def complete(result):
            if result.get('error'):
                return Text('未暂存：'+safe(result['error']),style=AMBER)
            text = Text()
            if result['moved_items']:
                key = result['batch']
                moved = result.get('moved_paths',[chosen[0]['path']])
                self.operation_results.append(f'已暂存 {result["moved_items"]} 项\n批次 {key}\n恢复：clean-my-agent restore {key}')
                self.report['entries'] = [r for r in self.report['entries'] if r['path'] not in moved]
                for project_row in self.report.get('projects',[]):
                    project_row['artifacts'] = sum(Path(project_row['path']) in Path(r['path']).parents for r in self.report['entries'])
                    if any(Path(project_row['path']) in Path(p).parents for p in moved):
                        project_row['directory_bytes'] = None
                text.append(f'已暂存 {result["moved_items"]} 项，释放 0 B。\n\n返回后按 v 打开暂存区 · Space 勾选 · d 删除 / r 恢复\n批次：{key}\n',style=MINT)
                self.query_one('#context',Static).update('暂存完成 · 其余容量为扫描时记录')
                self.query_one('#context').display = True
            elif result.get('row'):
                for row in self.report['entries']:
                    if row['path']==result['row']['path']:
                        row.update(result['row'])
            for error in result['errors']:
                text.append('\n'+safe(error),style=AMBER)
            self.checked.clear()
            self.update_worktree_data()
            return text
        self.push_screen(ActionScreen('确认暂存',body,execute,complete,refresh=self.refresh_list))

    @on(DataTable.RowHighlighted)
    def highlighted(self, event):
        self.preview(event.cursor_row)

    @on(DataTable.RowSelected)
    def selected(self, event):
        if 0<=event.cursor_row<len(self.rows):
            row = self.rows[event.cursor_row]
            self.open_detail(DetailScreen('占用详情', details(row), row['path']))

    def open_detail(self, dialog):
        self.push_screen(dialog, lambda _: self.call_after_refresh(self.refresh_list, True))

    @on(Button.Pressed)
    def switch(self, event):
        if event.button.id in GROUPS:
            self.action_group(GROUPS.index(event.button.id))

    def action_group(self, index):
        self.checked.clear()
        self.category=GROUPS[index]
        self.refresh_list()
        self.query_one(DataTable).focus()

    def action_sort(self):
        if self.category!='links':
            self.by_size=not self.by_size
            self.refresh_list()

    def action_search(self):
        search=self.query_one(Input)
        search.add_class('open')
        search.focus()

    @on(Input.Changed, '#search')
    def filter_changed(self,event):
        self.checked.clear()
        self.filter_text=event.value
        self.refresh_list()

    @on(Input.Submitted, '#search')
    def finish_filter(self):
        self.query_one(DataTable).focus()

    def action_reset(self):
        search=self.query_one(Input)
        search.value=''
        search.remove_class('open')
        self.query_one(DataTable).focus()

    def action_overview(self):
        if self.scan_doc:
            self.open_detail(DetailScreen('扫描概览',scan_details(self.scan_doc)))

    @on(Button.Pressed, '#resources')
    def action_resources(self):
        if self.scan_doc and self.scan_doc.get('agent_resources'):
            from .resource_tui import make_screen
            a, b = self.scan_doc['agent_resources'], self.scan_doc['extensions']
            data = dict(entries=a['entries'] + b['entries'], errors=a['errors'] + b['errors'],
                        totals_note='额外资源只读；主目录内明细不可再次加入原统计，内置组件不重复计量。')
            self.push_screen(make_screen(data))

    def action_help(self):
        t=Text('1 / 2 / 3   候选、保留、软链接\n↑ / ↓       选择项目\nEnter       完整路径、精确字节和引用来源\n'
               '/           按工作树、项目或引用路径筛选\nEsc         清除筛选或返回\ns           切换预计释放 / 目录大小排序\n'
               + ('0           查看扫描分类和最大目录\n4           Agent 额外资源与扩展版本（只读）\n' if self.scan_doc else '') + 'w           工作树列表、详情与删除\nSpace       勾选产物；Ctrl+A 选择当前候选\nt           暂存清单，Enter 确认\nv           暂存区：Space 勾选，d 删除 / r 恢复\nq           返回 / 退出\n\n历史快照只读；暂存入口仅对实时报告中的候选开放。\n'
               '预计释放按单个目录永久删除计算，各行不可相加。暂存释放 0 B。\n'
               '安装和运行检查见 Enter 详情；暂存前会重新核实目录与引用。')
        for e in self.scan_doc['errors'] if self.scan_doc else self.report['errors']:
            t.append('\n'+safe(e), style=AMBER)
        self.open_detail(DetailScreen('帮助', t))
