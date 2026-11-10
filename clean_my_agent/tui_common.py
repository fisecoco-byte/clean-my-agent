"""Shared terminal layout and explicit confirmation/operation dialogs."""
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, ProgressBar, Static

from .terminal import safe

SHARED_CSS = """
    Screen { background: #111b1d; color: #e0ebe6; padding: 1 2; }
    #brand { color: #83d9c0; height: 1; text-style: bold; }
    #heading { margin-top: 1; height: 1; text-style: bold; }
    #context { height: auto; max-height: 2; color: #a9c1b8; margin-bottom: 1; }
    #tabs { height: 2; }
    #tabs Button { width: auto; min-width: 11; height: 1; border: none; margin-right: 1;
                    padding: 0 1; background: #233234; color: #b3c5bf; }
    #tabs Button.chosen { background: #83d9c0; color: #10251e; text-style: bold; }
    #search { display: none; height: 3; margin-bottom: 1; }
    #search.open { display: block; }
    #layout { height: 1fr; min-height: 3; }
    #listpane { width: 1fr; padding-right: 1; }
    #sortline { height: 1; color: #9ab4ac; margin-bottom: 1; }
    DataTable { height: 1fr; background: #111b1d; color: #d5e5de; scrollbar-size: 1 1; }
    DataTable > .datatable--header { background: #223235; color: #c3d7cf; text-style: bold; }
    DataTable:focus > .datatable--cursor, DataTable > .datatable--cursor {
        background: #284e47; color: #f1fff9; text-style: bold; }
    #side { width: 34; border-left: solid #364a46; padding-left: 2; scrollbar-size: 1 1; }
    #sidetitle, #dialogtitle { text-style: bold; color: #83d9c0; margin-bottom: 1; }
    #sidehint, #legend { height: auto; color: #9ab4ac; }
    #empty, #warning { height: auto; color: #efc480; }
    #footer { height: auto; color: #d6e7df; border-top: solid #364a46; padding-top: 0; }
    #legend { margin: 1 0; }
    .narrow #side { display: none; }
    .narrow #listpane { padding-right: 0; }
    .small #tabs Button { min-width: 0; margin-right: 1; padding: 0 1; }
    .short #heading, .short #legend { margin: 0; }
    .short #context, .short #sortline { margin-bottom: 0; }
    DetailScreen, ActionScreen { align: center middle; background: #000000 55%; padding: 1; }
    #dialog { width: 86%; max-width: 100; height: 88%; padding: 1 2; border: solid #68b69f; background: #192729; }
    #dialogbody { height: 1fr; min-height: 1; scrollbar-size: 1 1; }
    #dialogkeys { height: 1; }
    #dialogkeys Button { height: 1; min-width: 0; width: auto; margin-right: 1; border: none; background: #29433d; color: #c1eedc; }
    #dialogkeys Button:focus { text-style: bold reverse; }
    #action-hint { height: auto; max-height: 2; color: #9ab4ac; margin-top: 1; }
    """

SHARED_CSS += """
#confirmation { height: 3; margin-top: 1; }
#operation_progress { height: auto; max-height: 3; color: #efc480; }
#jobbar { height: 1; margin-bottom: 1; }
#execute { background: #7de2bf; color: #10251e; }
#execute:disabled { background: #233234; color: #738c83; }
#stage { height: 1; width: auto; border: none; padding: 0 1; background: #7de2bf; color: #10251e; }
#stage:disabled { background: #233234; color: #738c83; }
#too-small { display: none; }
.tiny > * { display: none; }
.tiny > #too-small { display: block; height: auto; color: #efc480; }
"""

class ManagedApp(App):
    operation_busy = False
    return_to_dashboard = False

    def on_ready(self):
        if self.return_to_dashboard and self.query('#brand'):
            self.query_one('#brand', Static).update('CLEAN MY AGENT')

    async def action_quit(self):
        if self.operation_busy:
            self.notify('操作正在执行，请等待结果后退出。')
        else:
            self.exit()


class DetailScreen(ModalScreen):
    BINDINGS = [('escape','dismiss','返回'), ('q','dismiss','返回'), ('enter','dismiss','返回'),
                ('p','path','打印路径')]
    def __init__(self, title, content, path=None):
        super().__init__()
        self.heading, self.body, self.path = title, content, path

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Static(self.heading, id='dialogtitle', markup=False)
            with VerticalScroll(id='dialogbody', can_focus=True):
                yield Static(self.body)
            with Horizontal(id='dialogkeys'):
                yield Button('Esc  返回列表', id='back', compact=True)
                if self.path:
                    yield Button('p  打印路径并退出', id='printpath', compact=True)

    def on_mount(self):
        self.query_one('#dialogbody').focus()

    @on(Button.Pressed, '#back')
    def back(self):
        self.dismiss()

    @on(Button.Pressed, '#printpath')
    def action_path(self):
        if self.path:
            self.app.exit(self.path)


class ActionScreen(ModalScreen):
    """The executor is reachable only after the explicit confirmation button."""
    BINDINGS = [('escape', 'cancel', '取消'), ('q', 'cancel', '取消'),
                Binding('enter', 'submit', '确认', priority=True),
                ('left', 'choose_cancel', '取消'), ('right', 'choose_execute', '确认')]

    def __init__(self, title, body, execute, complete, *, phrase=None, refresh=None, auto_run=False):
        super().__init__()
        self.heading, self.body = title, body
        self.execute, self.complete, self.refresh_callback = execute, complete, refresh
        self.phrase, self.auto_run = phrase, auto_run
        self.busy = self.finished = False

    def compose(self):
        with Vertical(id='dialog'):
            yield Static(self.heading, id='dialogtitle', markup=False)
            with VerticalScroll(id='dialogbody'):
                yield Static(self.body)
            yield Static('', id='operation_progress', markup=False)
            yield ProgressBar(show_eta=False, id='jobbar')
            if self.phrase:
                yield Input(placeholder=f'输入 {self.phrase} 确认永久删除', id='confirmation')
            with Horizontal(id='dialogkeys'):
                yield Button('取消', id='cancel', compact=True)
                yield Button('确认执行', id='execute', compact=True, disabled=bool(self.phrase))
            yield Static('输入 '+self.phrase+' · Enter 确认 · Esc 取消' if self.phrase else
                         'Enter 确认 · ←→ / Tab 切换 · Esc 取消', id='action-hint', markup=False)

    def on_mount(self):
        self.query_one('#jobbar').display = False
        self.query_one('#confirmation' if self.phrase else '#execute').focus()
        if self.auto_run:
            self.start()

    @on(Input.Changed, '#confirmation')
    def changed(self, event):
        event.stop()
        self.query_one('#execute', Button).disabled = event.value != self.phrase

    def action_choose_cancel(self):
        if not self.busy and not self.finished:
            self.query_one('#cancel').focus()

    def action_choose_execute(self):
        if not self.busy and not self.query_one('#execute', Button).disabled:
            self.query_one('#execute').focus()

    def action_submit(self):
        if self.busy:
            return
        if self.focused is self.query_one('#cancel'):
            self.action_cancel()
        else:
            self.confirmed()

    @on(Input.Submitted, '#confirmation')
    def submitted(self, event):
        event.stop()
        self.action_submit()

    def action_cancel(self):
        if self.busy:
            self.app.notify('正在核对并执行，请等待结果。')
            return
        self.dismiss()
        if self.refresh_callback:
            self.app.call_after_refresh(self.refresh_callback)

    @on(Button.Pressed, '#cancel')
    def cancel(self):
        self.action_cancel()

    @on(Button.Pressed, '#execute')
    def confirmed(self):
        if self.finished:
            self.action_cancel()
        elif not self.busy and (not self.phrase or self.query_one('#confirmation', Input).value == self.phrase):
            self.start()

    def start(self):
        self.busy = self.app.operation_busy = True
        self.query_one('#execute', Button).disabled = True
        self.query_one('#cancel', Button).disabled = True
        for widget in self.query('#confirmation'):
            widget.display = False
        self.query_one('#jobbar').display = True
        self.query_one('#action-hint', Static).update('正在执行…')
        self.run_worker(self.run_action, thread=True, exit_on_error=False)

    def run_action(self):
        def progress(label, done=None, total=None, detail=''):
            self.app.call_from_thread(self.progress, label, done, total, detail)
        try:
            result = self.execute(progress)
        except Exception as exc:
            result = {'error': str(exc)}
        self.app.call_from_thread(self.finish, result)

    def progress(self, label, done, total, detail):
        self.query_one('#operation_progress', Static).update(safe(label + (' · ' + detail if detail else '')))
        self.query_one('#jobbar', ProgressBar).update(total=total, progress=done or 0)

    def finish(self, result):
        self.busy = self.app.operation_busy = False
        self.finished = True
        self.query_one('#jobbar').display = False
        self.query_one('#operation_progress', Static).update('')
        self.query_one('#dialogtitle', Static).update('操作结果')
        content = self.complete(result)
        self.query_one('#dialogbody Static', Static).update(content)
        self.query_one('#dialogbody', VerticalScroll).scroll_home(animate=False)
        self.query_one('#cancel').display = False
        button = self.query_one('#execute', Button)
        button.label = '返回列表'
        button.disabled = False
        button.focus()
        self.query_one('#action-hint', Static).update('Enter / Esc 返回')
