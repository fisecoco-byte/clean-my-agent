"""Selectable worktree list, scoped artifact navigation and checked native removal."""
from pathlib import Path
from rich.text import Text
from textual import on, events
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, ProgressBar, Static

from .storage_view import project, size
from . import activity
from .terminal import safe
from .tui_common import ActionScreen, DetailScreen


def details(row):
    body = Text(safe(row['path'])+'\n\n')
    reasons = row.get('reasons', [])
    if reasons:
        body.append('当前不能删除\n', style='bold #efc480')
        for reason in reasons:
            body.append('• '+safe(reason)+'\n', style='#efc480')
        body.append('\n')
    body.append(f"{row['agent']} · 目录占用 {size(row.get('directory_bytes'))}\n")
    if row.get('repository_kind') == 'standalone':
        body.append('独立仓库 · 清理方式：移入系统废纸篓\n')
    body.append('\n活跃度 ' + safe(activity.describe(row)) + '\n\n')
    body.append('目录大小包含共享块，不等于可释放量。\n\n')
    if row.get('head'):
        body.append(safe(row.get('branch') or '分离 HEAD')+'\n'+safe(row['head'])+'\n\n')
    for label, result in row.get('checks',{}).items():
        body.append(label+'  '+safe(result)+'\n')
    for label,key in (('改动 / 未跟踪','changes'),('被引用','referenced_by'),('随工作树移除的忽略产物','ignored')):
        if row.get(key):
            body.append('\n'+label+'\n',style='bold')
            for item in row[key]:
                body.append(safe(item)+'\n')
    for ref in row.get('agent_references',[]):
        body.append('\n'+safe(ref.get('title') or ref.get('path',''))+
                    (' · 保留' if ref['blocking'] else ' · 已归档会话')+'\n')
        if ref.get('id'):
            body.append(safe(ref['id'])+'\n')
    return body


class WorktreeScreen(ModalScreen):
    BINDINGS = [('escape','back','返回'),('q','back','返回'),('p','artifacts','产物'),('d','delete','清理'),('s','sort','排序'),('r','reload','刷新')]
    DEFAULT_CSS = '''
        WorktreeScreen { background: #111b1d; padding: 1 2; }
        #wt-title { height: 2; color: #83d9c0; text-style: bold; }
        #wt-table { height: 1fr; }
        #wt-path { height: auto; max-height: 3; color: #9ab4ac; margin: 1 0; }
        #wt-progress { height: 1; }
        #wt-status { height: auto; max-height: 2; color: #efc480; }
        #wt-buttons { height: 1; }
        #wt-buttons Button { width: auto; min-width: 0; height: 1; padding: 0 1; margin-right: 1; border: none; background: #233234; }
    '''
    def __init__(self, report, actions=None):
        super().__init__()
        self.report, self.actions = report, actions
        self.rows = list(report.get('projects',[]))
        self.check_results = {}
        self.sort_mode = 'cold'
        self.rows.sort(key=activity.sort_key)

    def compose(self):
        yield Static('工作树 / 工作目录 · Enter 详情 · d 核对清理',id='wt-title')
        yield DataTable(id='wt-table',cursor_type='row',show_row_labels=False)
        yield Static('',id='wt-path',markup=False)
        yield Static('',id='wt-status',markup=False)
        yield ProgressBar(id='wt-progress',show_eta=False)
        with Horizontal(id='wt-buttons'):
            yield Button('详情',id='wt-detail',compact=True)
            yield Button('p 产物',id='wt-artifacts',compact=True)
            yield Button('d 清理',id='wt-delete',compact=True,disabled=self.actions is None)
            yield Button('s 排序',id='wt-sort',compact=True)
            yield Button('返回',id='wt-back',compact=True)

    def on_mount(self):
        table = self.query_one('#wt-table',DataTable)
        self.fill()
        self.query_one('#wt-progress').display = False
        table.focus()

    def fill(self):
        table = self.query_one('#wt-table',DataTable)
        index = table.cursor_row
        table.clear(columns=True)
        wide = self.size.width >= 75
        table.add_column('工作树 / 工作目录',width=max(9,self.size.width-(55 if wide else 34)))
        if wide:
            table.add_column('Agent',width=7)
        table.add_column('活跃度',width=12)
        table.add_column('目录占用',width=11)
        if wide:
            table.add_column('产物',width=5)
        for i,row in enumerate(self.rows):
            table.add_row(Text(project(row['path']),no_wrap=True,overflow='ellipsis'),
                          *([row['agent']] if wide else []),
                          Text(activity.badge(row), style=activity.COLORS[activity.value(row).get('level','unknown')]),
                          size(row.get('directory_bytes')),
                          *([str(row.get('artifacts',0))] if wide else []),key=str(i))
        table.move_cursor(row=max(0,min(index,len(self.rows)-1)))
        self.show_status()
        self.highlight()

    def show_status(self):
        message = getattr(self.app, 'scan_message', '') or activity.SORT_LABELS[self.sort_mode]
        row = self.selected()
        checked = self.check_results.get(row['path']) if row else None
        ready = getattr(self.app, 'inspection_ready', True)
        reasons = checked.get('reasons', []) if checked else []
        if checked and ready:
            message = ('上次检查未通过：'+safe('；'.join(reasons)) if reasons else
                       '上次检查通过 · d 重新核对并确认移入废纸篓' if checked.get('can_trash') else
                       '上次检查通过 · d 重新核对并确认删除' if checked.get('can_delete') else '删除条件未确认 · d 重新核对')
        elif not getattr(self.app, 'scan_pending', False) and self.report.get('measured_at'):
            message += ' · 采集于 '+self.report['measured_at'].replace('T', ' ')+' · r 刷新'
        self.query_one('#wt-status', Static).update(message + (' · 历史快照 · 只读' if self.actions is None else ''))
        button = self.query_one('#wt-delete', Button)
        button.disabled = self.actions is None or not ready
        button.label = ('d 原因' if reasons and ready else
                        'd 废纸篓' if checked and checked.get('can_trash') and ready else
                        'd 删除' if checked and checked.get('can_delete') and ready else 'd 清理')

    def replace_report(self):
        selected = self.selected()
        self.check_results.clear()
        self.rows = list(self.report.get('projects', []))
        self.rows.sort(key=(lambda r: (r.get('directory_bytes') is None, -(r.get('directory_bytes') or 0), r['path']))
                       if self.sort_mode == 'size' else lambda r: activity.sort_key(r, self.sort_mode))
        self.fill()
        if selected:
            index = next((i for i,r in enumerate(self.rows) if r['path'] == selected['path']), 0)
            self.query_one('#wt-table', DataTable).move_cursor(row=index)
            self.highlight()

    def action_reload(self):
        if not self.app.operation_busy and getattr(self.app, 'loader', None):
            self.app.action_reload(from_worktrees=True)

    @on(Button.Pressed,'#wt-sort')
    def action_sort(self):
        if self.app.operation_busy:
            return
        selected = self.selected()
        modes = ('cold', 'hot', 'size')
        self.sort_mode = modes[(modes.index(self.sort_mode)+1) % len(modes)]
        self.rows.sort(key=(lambda r: (r.get('directory_bytes') is None, -(r.get('directory_bytes') or 0), r['path']))
                       if self.sort_mode == 'size' else lambda r: activity.sort_key(r, self.sort_mode))
        self.fill()
        if selected:
            self.query_one('#wt-table',DataTable).move_cursor(row=self.rows.index(selected))
            self.highlight()

    def on_resize(self,event: events.Resize):
        if self.is_mounted and self.query('#wt-table'):
            self.call_after_refresh(self.fill)

    def selected(self):
        index = self.query_one('#wt-table',DataTable).cursor_row
        return self.rows[index] if 0 <= index < len(self.rows) else None

    @on(DataTable.RowHighlighted,'#wt-table')
    def highlight(self,event=None):
        if event is not None:
            event.stop()
        row = self.selected()
        self.query_one('#wt-path',Static).update(safe(row['path']) if row else '暂无工作树')
        self.show_status()

    @on(Button.Pressed,'#wt-back')
    def action_back(self):
        if not self.app.operation_busy:
            self.dismiss(None)

    @on(Button.Pressed,'#wt-artifacts')
    def action_artifacts(self):
        row = self.selected()
        if row and not self.app.operation_busy:
            self.dismiss(row)

    @on(DataTable.RowSelected,'#wt-table')
    @on(Button.Pressed,'#wt-detail')
    def action_detail(self,event=None):
        if event is not None:
            event.stop()
        self.check('detail')

    @on(Button.Pressed,'#wt-delete')
    def action_delete(self):
        self.check('delete')

    def check(self, operation):
        row = self.selected()
        if self.app.operation_busy or not row:
            return
        if self.actions is None or not getattr(self.app, 'inspection_ready', True):
            if operation=='detail':
                self.app.push_screen(DetailScreen('工作树详情 · 当前记录',details(row),row['path']))
            else:
                reason = ('当前为只读查看，不能执行删除。' if self.actions is None else
                          '后台扫描尚未完成，请等待扫描结束后再核对删除。' if getattr(self.app, 'scan_pending', False) else
                          '扫描未完成，请按 r 重新扫描后再核对删除。')
                self.app.push_screen(DetailScreen('当前不能删除', details(dict(row, reasons=[reason])), row['path']))
            return
        self.app.operation_busy = True
        self.query_one('#wt-progress').display = True
        self.run_worker(lambda:self.run_check(dict(row),operation),thread=True,exit_on_error=False)

    def run_check(self,row,operation):
        def progress(label,done=None,total=None,detail=''):
            self.app.call_from_thread(self.progress,label,done,total,detail)
        try:
            result = self.actions.worktree_details(row,progress)
        except Exception as exc:
            result = dict(row,can_delete=False,reasons=[str(exc)])
        self.app.call_from_thread(self.show_check,result,operation)

    def progress(self,label,done,total,detail):
        self.query_one('#wt-status',Static).update(safe(label))
        self.query_one('#wt-progress',ProgressBar).update(total=total,progress=done or 0)

    def show_check(self,result,operation):
        self.app.operation_busy = False
        self.query_one('#wt-progress').display = False
        self.check_results[result['path']] = result
        self.show_status()
        body = details(result)
        if operation != 'delete' or not (result.get('can_delete') or result.get('can_trash')):
            title = '当前不能删除' if operation == 'delete' else '工作树详情'
            self.app.push_screen(DetailScreen(title,body,result['path']))
            return
        if result.get('can_trash'):
            warning = Text('将独立仓库移入系统废纸篓\n文件、本地分支和 Git 历史一起保留在废纸篓中。\n'
                           '清空废纸篓前，可将整个目录移回原路径；移入时不释放空间。\n\n',style='#efc480')
            self.app.push_screen(ActionScreen('确认移入废纸篓',warning+body,
                lambda p:self.actions.trash_repository(result,p),self.deleted,
                phrase='TRASH',confirmation_action='移入废纸篓'))
            return
        warning = Text('永久删除工作树及产物 · 不进入暂存区\nGit 原生执行；分支、其他工作树和会话记录保留。\n\n',style='#efc480')
        body = warning + body
        self.app.push_screen(ActionScreen('确认删除整个工作树',body,
            lambda p:self.actions.delete_worktree(result,p),self.deleted,phrase='DELETE'))

    def deleted(self,result):
        if result.get('error'):
            return Text('未删除：'+safe(result['error']))
        self.app.operation_results.append(safe(result['message']))
        if result['status']=='succeeded':
            path = Path(result['path'])
            self.rows = [r for r in self.rows if Path(r['path'])!=path]
            self.report['projects'] = self.rows
            self.report['entries'] = [r for r in self.report['entries'] if not (path==Path(r['path']) or path in Path(r['path']).parents)]
            self.app.update_worktree_data()
            self.fill()
        return Text(safe(result['message']))
