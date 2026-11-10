"""Item-level quarantine selection shared by the overview and dependency pages."""
from rich.text import Text
from textual import on, events
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, ProgressBar, Static

from .terminal import fmt, safe
from .tui_common import ActionScreen, DetailScreen


class VaultScreen(ModalScreen):
    BINDINGS = [('escape','back','返回'), ('q','back','返回'), ('v','back','返回'),
                ('space','toggle','勾选'), ('ctrl+a','all','全选'),
                ('d',"review('purge')",'删除'), ('r',"review('restore')",'恢复'),
                ('ctrl+r','reload','刷新')]
    DEFAULT_CSS = '''
        VaultScreen { background: #111b1d; padding: 1 2; }
        #vault-title { height: 2; color: #83d9c0; text-style: bold; }
        #vault-table { height: 1fr; }
        #vault-status, #vault-path { height: auto; max-height: 3; color: #9ab4ac; }
        #vault-progress { height: 1; }
        #vault-keys { height: auto; max-height: 2; margin-top: 1; }
        #vault-buttons { height: 1; }
        #vault-buttons Button { height: 1; width: auto; min-width: 0; border: none; margin-right: 1; padding: 0 1; }
        #vault-buttons Button:focus { text-style: bold reverse; }
    '''
    def __init__(self, actions):
        super().__init__()
        self.actions = actions
        self.rows, self.errors, self.checked = [], [], set()

    def compose(self):
        yield Static('暂存区 · 按条目选择', id='vault-title')
        yield Static('', id='vault-status', markup=False)
        yield ProgressBar(id='vault-progress', show_eta=False)
        yield DataTable(id='vault-table', cursor_type='row', show_row_labels=False)
        yield Static('', id='vault-path', markup=False)
        with Horizontal(id='vault-buttons'):
            yield Button('r 恢复', id='vault-restore', compact=True, disabled=True)
            yield Button('d 删除', id='vault-delete', compact=True, disabled=True)
            yield Button('返回', id='vault-back', compact=True)
        yield Static('Space 勾选 · Enter 详情 · Ctrl+A 全选 · Ctrl+R 刷新 · Esc 返回', id='vault-keys')

    def on_mount(self):
        self.query_one(DataTable).focus()
        self.action_reload()

    @staticmethod
    def identity(row):
        return row['batch'], row['index']

    def selected(self):
        i = self.query_one(DataTable).cursor_row
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def fill(self):
        table = self.query_one(DataTable)
        index = table.cursor_row
        table.clear(columns=True)
        wide = self.size.width >= 90
        for label, width in [('选择',4), ('原路径',max(12,self.size.width-(44 if wide else 24))), ('清单计块',11)]:
            table.add_column(label,width=width)
        if wide:
            table.add_column('批次',width=16)
        for row in self.rows:
            table.add_row('☑' if self.identity(row) in self.checked else '☐',
                Text(safe(row['path']),no_wrap=True,overflow='ellipsis'),fmt(row['bytes']),
                *([row['batch']] if wide else []))
        table.move_cursor(row=min(index,max(0,len(self.rows)-1)))
        self.query_one('#vault-status',Static).update(
            '；'.join(map(safe,self.errors)) if self.errors else
            f'{len(self.rows)} 项 · 已选 {len(self.checked)} · 清单计块 ≠ 实际释放')
        for button in self.query('#vault-delete, #vault-restore'):
            button.disabled = not self.checked or bool(self.errors) or self.app.operation_busy
        self.highlight()

    def on_resize(self, event: events.Resize):
        if self.is_mounted:
            self.call_after_refresh(self.fill)

    @on(DataTable.RowHighlighted, '#vault-table')
    def highlight(self, event=None):
        if event:
            event.stop()
        row = self.selected()
        self.query_one('#vault-path',Static).update(safe(row['path']) if row else '暂存区为空')

    @on(DataTable.RowSelected, '#vault-table')
    def detail(self, event):
        event.stop()
        row = self.selected()
        if row and not self.app.operation_busy:
            self.app.push_screen(DetailScreen('暂存条目',Text(safe(row['path'])+'\n\n批次 '+row['batch']+
                '\n创建于 '+safe(row['created'])+'\n清单计块 '+fmt(row['bytes']))))

    def action_toggle(self):
        row = self.selected()
        if row and not self.app.operation_busy and not self.errors:
            self.checked.symmetric_difference_update({self.identity(row)})
            self.fill()

    def action_all(self):
        if not self.app.operation_busy and not self.errors:
            paths = {self.identity(r) for r in self.rows}
            self.checked = set() if self.checked==paths else paths
            self.fill()

    @on(Button.Pressed, '#vault-back')
    def action_back(self):
        if not self.app.operation_busy:
            self.dismiss()

    def progress(self,label,done=None,total=None,detail=''):
        self.app.call_from_thread(self.query_one('#vault-status',Static).update,safe(label))
        self.app.call_from_thread(self.query_one('#vault-progress',ProgressBar).update,total=total,progress=done or 0)

    def start(self, fn, complete):
        if self.app.operation_busy:
            return
        self.app.operation_busy = True
        self.query_one('#vault-progress').display = True
        def run():
            try:
                result = fn(self.progress)
            except Exception as exc:
                result = {'error':str(exc)}
            self.app.call_from_thread(self.done,result,complete)
        self.run_worker(run,thread=True,exit_on_error=False)

    def done(self,result,complete):
        self.app.operation_busy = False
        self.query_one('#vault-progress').display = False
        complete(result)

    def action_reload(self):
        def loaded(result):
            self.checked.clear()
            self.rows = result.get('items',[])
            self.errors = [result['error']] if result.get('error') else result.get('errors',[])
            self.fill()
        self.start(self.actions.list,loaded)

    @on(Button.Pressed, '#vault-delete')
    def delete(self):
        self.action_review('purge')

    @on(Button.Pressed, '#vault-restore')
    def restore(self):
        self.action_review('restore')

    def action_review(self, operation):
        if self.app.operation_busy or self.errors:
            return
        chosen = [dict(r) for r in self.rows if self.identity(r) in self.checked]
        if not chosen:
            self.notify('请先用空格勾选暂存条目。')
            return
        def prepared(result):
            if result.get('error'):
                self.app.push_screen(DetailScreen('暂存条目保留',Text(safe(result['error']))))
                return
            label = '永久删除' if operation=='purge' else '恢复'
            body = Text(f'{label} {len(chosen)} 项'+(' · 不可恢复' if operation=='purge' else ' · 不覆盖已有文件')+'\n\n',style='#efc480')
            for row in chosen:
                body.append(safe(row['path'])+'\n批次 '+row['batch']+' · '+fmt(row['bytes'])+'\n\n',style='')
            attempted = False
            def completed(done):
                nonlocal attempted
                attempted = True
                self.checked.clear()
                if done.get('error'):
                    return Text('未完成：'+safe(done['error']))
                message = f'已{label} {done["count"]} 项'
                self.app.operation_results.append(message)
                return Text(message+'\n'+'\n'.join(map(safe,done['errors'])))
            self.app.push_screen(ActionScreen('确认'+label,body,
                lambda p:self.actions.apply(result,p),completed,
                phrase='DELETE' if operation=='purge' else None,
                refresh=lambda: self.action_reload() if attempted else None))
        self.start(lambda p:self.actions.preview(operation,chosen,p),prepared)
