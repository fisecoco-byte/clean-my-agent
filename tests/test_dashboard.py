import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from clean_my_agent.dashboard import DashboardService
from clean_my_agent.cli import main


def snapshot():
    return dict(groups=[dict(agent='codex',category='worktrees',bytes=70*1024**3,eligible_bytes=0,eligible_files=0),
                        dict(agent='codex',category='sessions',bytes=3*1024**3,eligible_bytes=0,eligible_files=0),
                        dict(agent='claude',category='logs',bytes=8192,eligible_bytes=4096,eligible_files=1)],
                bytes=73*1024**3+8192, eligible_bytes=4096, eligible_files=1, full=True,
                deferred_dirs=0, errors=[], missing_roots=[], roots={'codex':'/demo/.codex','claude':'/demo/.claude'},
                measured_at='2026-11-09T12:00:00+08:00',
                agent_resources={'entries':[]}, extensions={'groups':[]})


class ServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {'HOME':str(self.home), 'CODEX_HOME':str(self.home/'codex'),
            'CLAUDE_CONFIG_DIR':str(self.home/'claude'), 'XDG_STATE_HOME':str(self.home/'state'),
            'XDG_CONFIG_HOME':str(self.home/'config')})
        env.start()
        self.addCleanup(env.stop)
        self.service = DashboardService()
        self.path = self.home/'codex/log/old.log'
        self.path.parent.mkdir(parents=True)
        self.path.write_text('fixture')
        os.utime(self.path,(1,1))

    def test_home_is_readonly_counts_full_worktree_and_defers_dep_measurement(self):
        tree = self.home/'codex/worktrees/demo/node_modules/pkg'
        tree.parent.mkdir(parents=True)
        tree.write_bytes(b'x'*10000)
        with patch('clean_my_agent.dependency_report.inspect',side_effect=AssertionError('eager dependency scan')):
            doc = self.service.snapshot()
        self.assertTrue(doc['full'])
        self.assertGreaterEqual(doc['bytes'], tree.stat().st_blocks*512)
        self.assertTrue(self.path.exists())
        self.assertFalse(self.service.state.exists())

    def test_selected_files_stage_restore_and_purge_use_existing_vault(self):
        selected = self.service.clean_preview('codex')['items']
        result = self.service.stage_files('codex', selected, lambda *a,**kw:None)
        self.assertEqual(result['count'],1)
        self.assertFalse(self.path.exists())
        key=result['batch']
        restore=self.service.batch_preview(key,'restore')
        self.assertEqual(len(restore['files']),1)
        self.service.batch_apply(restore,lambda *a,**kw:None)
        self.assertTrue(self.path.exists())
        result=self.service.stage_files('codex',self.service.clean_preview('codex')['items'],lambda *a,**kw:None)
        purge=self.service.batch_preview(result['batch'],'purge')
        done=self.service.batch_apply(purge,lambda *a,**kw:None)
        self.assertEqual(done['count'],1)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.service.batches()['items'],[])

    def test_changed_file_or_new_protection_blocks_reviewed_operation(self):
        selected=self.service.clean_preview('codex')['items']
        self.path.write_text('changed')
        with self.assertRaisesRegex(ValueError,'变化'):
            self.service.stage_files('codex',selected,lambda *a,**kw:None)
        os.utime(self.path,(1,1))
        result=self.service.stage_files('codex',self.service.clean_preview('codex')['items'],lambda *a,**kw:None)
        preview=self.service.batch_preview(result['batch'],'purge')
        config=self.home/'config/clean-my-agent/policy.json'
        config.parent.mkdir(parents=True)
        config.write_text('{"protect":["codex:log"]}')
        with self.assertRaisesRegex(ValueError,'变化'):
            self.service.batch_apply(preview,lambda *a,**kw:None)
        self.service.batch_apply(self.service.batch_preview(result['batch'],'restore'),lambda *a,**kw:None)
        self.assertTrue(self.path.exists())

    def test_default_and_agent_flag_route_home_json_never_opens_ui(self):
        with patch('clean_my_agent.dashboard.browse',return_value=True) as browse:
            self.assertEqual(main([]),0)
            self.assertTrue(browse.call_args.kwargs['full'])
            self.assertEqual(main(['--agent','codex']),0)
            self.assertEqual(browse.call_args.kwargs['agent'],'codex')
        with patch('clean_my_agent.dashboard.browse',side_effect=AssertionError('UI in JSON')), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['dashboard','--json']),0)
        self.assertTrue(json.loads(output.getvalue())['full'])
        with patch('clean_my_agent.cli.run',return_value=0) as run:
            self.assertEqual(main(['--plain','scan','--quick']),0)
            self.assertEqual(run.call_args.args[0].cmd,'scan')
            self.assertTrue(run.call_args.args[0].plain)


try:
    from clean_my_agent.dashboard_tui import DashboardApp, FilesApp, BatchesApp, LoadingApp
    from clean_my_agent.tui_common import ActionScreen
    from textual.widgets import DataTable, Button, Input
except ImportError:
    DashboardApp=None


@unittest.skipUnless(DashboardApp,'UI extra required')
class DashboardUI(unittest.IsolatedAsyncioTestCase):
    async def wait_done(self,pilot,screen):
        for _ in range(80):
            if screen.finished:return
            await pilot.pause(.05)
        self.fail('worker did not finish')

    async def test_home_filter_and_drilldown_without_writes(self):
        service=Mock(agent='all')
        service.selected.return_value={'codex','claude'}
        app=DashboardApp(service,snapshot=snapshot())
        async with app.run_test(size=(140,36)) as pilot:
            self.assertEqual(app.query_one(DataTable).row_count,7)
            await pilot.press('2','down','enter')
            self.assertEqual(app.return_value,('deps','claude'))
        service.stage_files.assert_not_called()
        service.batch_apply.assert_not_called()

    async def test_loading_page_returns_real_result(self):
        app=LoadingApp('Read',lambda p:{'hello':'world'})
        async with app.run_test(size=(100,30)) as pilot:
            await pilot.pause(.2)
        self.assertEqual(app.return_value,{'hello':'world'})

    async def test_scoped_home_does_not_expand_agent_and_small_screen_keeps_navigation(self):
        service=Mock(agent='codex')
        service.selected.return_value={'codex'}
        app=DashboardApp(service,snapshot=snapshot(),agent='codex')
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.press('0','2')
            self.assertEqual(app.agent_key,'codex')
            await pilot.resize_terminal(40,15)
            self.assertTrue(app.query_one('#too-small').display)
            await pilot.resize_terminal(100,30)
            await pilot.press('enter')
            self.assertEqual(app.return_value,('sessions','codex'))

    async def test_files_cancel_then_explicit_confirm(self):
        service=Mock()
        service.stage_files.return_value=dict(count=1,bytes=4096,batch='a'*16,errors=[])
        data=dict(items=[dict(source='codex',root='/demo/.codex',path='log/old.log',category='logs',size=4096,stamp=(1,2,3,4))],errors=[])
        app=FilesApp(service,'codex',data)
        async with app.run_test(size=(130,32)) as pilot:
            await pilot.press('space','x')
            self.assertIn('/demo/.codex/log/old.log',app.screen.body.plain)
            await pilot.press('escape')
            service.stage_files.assert_not_called()
            await pilot.press('x')
            screen=app.screen
            await pilot.click('#execute')
            await self.wait_done(pilot,screen)
            service.stage_files.assert_called_once()
            self.assertEqual(len(app.operation_results),1)

    async def test_batch_delete_requires_selection_and_delete_phrase(self):
        service=Mock(); actions=Mock()
        row=dict(batch='b'*16,created='2026-11-09',index=0,path='/demo/old.log',bytes=4096)
        actions.list.return_value=dict(items=[row],errors=[])
        actions.preview.return_value=dict(operation='purge',selected=[row],batches=[])
        actions.apply.return_value=dict(count=1,errors=[])
        with patch('clean_my_agent.quarantine_actions.QuarantineActions',return_value=actions):
            app=BatchesApp(service)
            async with app.run_test(size=(130,32)) as pilot:
                await pilot.pause(.2)
                await pilot.press('d')
                actions.preview.assert_not_called()
                await pilot.press('space','d')
                for _ in range(60):
                    if isinstance(app.screen,ActionScreen) and app.screen.phrase=='DELETE':break
                    await pilot.pause(.05)
                self.assertEqual(app.screen.phrase,'DELETE')
                self.assertIn('/demo/old.log',app.screen.body.plain)
                actions.apply.assert_not_called()
                self.assertTrue(app.screen.query_one('#execute',Button).disabled)
                await pilot.press('D','E','L','E','T','E')
                screen=app.screen
                await pilot.press('enter')
                await self.wait_done(pilot,screen)
                actions.apply.assert_called_once()


@unittest.skipUnless(DashboardApp,'UI extra required')
class NavigationTests(unittest.TestCase):
    def test_coordinator_returns_home_and_refreshes_only_after_operation(self):
        from clean_my_agent.dashboard import browse
        service=Mock()
        doc=snapshot()
        homes=[Mock(snapshot=doc) for _ in range(3)]
        homes[0].run.return_value=('clean','codex')
        homes[1].run.return_value=('clean','codex')
        homes[2].run.return_value=None
        children=[Mock(operation_results=[]),Mock(operation_results=['已暂存 1 项'])]
        with patch.dict(os.environ,{'TERM':'xterm'}), patch('sys.stdin.isatty',return_value=True), \
             patch('clean_my_agent.dashboard.DashboardService',return_value=service), \
             patch('clean_my_agent.dashboard_tui.DashboardApp',side_effect=homes) as home, \
             patch('clean_my_agent.dashboard_tui.FilesApp',side_effect=children), \
             patch('clean_my_agent.dashboard_tui.load_page',return_value={}):
            self.assertTrue(browse(Mock(active=True)))
        self.assertIs(home.call_args_list[1].kwargs['snapshot'],doc)
        self.assertIsNone(home.call_args_list[2].kwargs['snapshot'])
        self.assertEqual(home.call_args_list[2].kwargs['agent'],'codex')
        self.assertTrue(all(c.return_to_dashboard for c in children))

    def test_real_terminal_home_child_home_exit_restores_terminal(self):
        import fcntl, pty, select, struct, subprocess, sys, termios, time
        with tempfile.TemporaryDirectory() as td:
            td=str(Path(td).resolve())
            env={**os.environ,'TERM':'xterm-256color','HOME':td,'CODEX_HOME':td+'/codex',
                 'CLAUDE_CONFIG_DIR':td+'/claude','XDG_STATE_HOME':td+'/state','XDG_CONFIG_HOME':td+'/config'}
            log=Path(td)/'codex/log/old.log'
            log.parent.mkdir(parents=True)
            log.write_text('fixture')
            os.utime(log,(1,1))
            master,slave=pty.openpty()
            try:
                fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',35,130,0,0))
                before=termios.tcgetattr(slave)
                proc=subprocess.Popen([sys.executable,'-m','clean_my_agent'],env=env,stdin=slave,stdout=slave,stderr=slave)
                data=b''
                def until(check):
                    nonlocal data
                    end=time.monotonic()+10
                    while not check() and time.monotonic()<end:
                        if select.select([master],[],[],.05)[0]:
                            data+=os.read(master,65536)
                    self.assertTrue(check(),data[-1000:])
                try:
                    until(lambda:'采集于'.encode() in data)
                    data=b''
                    os.write(master,b'\x1b[B\x1b[B\r')  # Cache/logs page.
                    until(lambda:b'Space ' in data)
                    until(lambda:b'old.log' in data)
                    data=b''
                    os.write(master,b'q')
                    until(lambda:'采集于'.encode() in data)
                    os.write(master,b'q')
                    until(lambda:proc.poll() is not None)
                    self.assertEqual(proc.returncode,0)
                    self.assertIn(b'\x1b[?1049l',data)
                    self.assertEqual(before[3] & (termios.ECHO|termios.ICANON),
                                     termios.tcgetattr(slave)[3] & (termios.ECHO|termios.ICANON))
                    self.assertTrue(log.exists())
                    self.assertFalse((Path(td)/'state').exists())
                finally:
                    if proc.poll() is None:proc.kill();proc.wait()
            finally:
                os.close(master);os.close(slave)
