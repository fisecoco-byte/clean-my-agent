"""Selectable worktree list, scoped artifact navigation and checked native removal."""
from pathlib import Path
from rich.text import Text
from textual import on, events
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, ProgressBar, Static

from .storage_view import project, size
from .terminal import safe
from .tui_common import ActionScreen, DetailScreen


def details(row):
    body = Text(safe(row['path'])+'\n\n')
    body.append(f"{row['agent']} · 目录占用 {size(row.get('directory_bytes'))}\n")
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
    for reason in row.get('reasons',[]):
        body.append('\n保留：'+safe(reason),style='#efc480')
    return body


class WorktreeScreen(ModalScreen):
    BINDINGS = [('escape','back','返回'),('q','back','返回'),('p','artifacts','产物'),('d','delete','删除')]
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

    def compose(self):
        yield Static('工作树 · Enter 详情',id='wt-title')
        yield DataTable(id='wt-table',cursor_type='row',show_row_labels=False)
        yield Static('',id='wt-path',markup=False)
        yield Static('',id='wt-status',markup=False)
        yield ProgressBar(id='wt-progress',show_eta=False)
        with Horizontal(id='wt-buttons'):
            yield Button('详情',id='wt-detail',compact=True)
            yield Button('p 产物',id='wt-artifacts',compact=True)
            yield Button('d 删除',id='wt-delete',compact=True,disabled=self.actions is None)
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
        table.add_column('工作树',width=max(12,self.size.width-(41 if wide else 20)))
        if wide:
            table.add_column('Agent',width=7)
        table.add_column('目录占用',width=11)
        if wide:
            table.add_column('产物',width=5)
        for i,row in enumerate(self.rows):
            table.add_row(Text(project(row['path']),no_wrap=True,overflow='ellipsis'),
                          *([row['agent']] if wide else []),size(row.get('directory_bytes')),
                          *([str(row.get('artifacts',0))] if wide else []),key=str(i))
        table.move_cursor(row=max(0,min(index,len(self.rows)-1)))
        self.query_one('#wt-status',Static).update('历史快照 · 只读' if self.actions is None else '')
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
        if self.actions is None:
            if operation=='detail':
                self.app.push_screen(DetailScreen('工作树详情 · 快照',details(row),row['path']))
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
        self.query_one('#wt-status',Static).update('可核对删除' if result.get('can_delete') else '工作树保留 · Enter 查看原因')
        body = details(result)
        if operation != 'delete' or not result.get('can_delete'):
            self.app.push_screen(DetailScreen('工作树详情',body,result['path']))
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
