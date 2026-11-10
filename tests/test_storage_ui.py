import json
import io
import os
import pty
import fcntl
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from clean_my_agent import storage_view
from clean_my_agent.cli import main

try:
    from clean_my_agent.storage_tui import StorageApp, DetailScreen
    from textual.widgets import DataTable, Input
except ImportError:
    StorageApp = None


def fixture():
    def row(branch, freed, status='candidate', target=None):
        return dict(path=f'/tmp/worktrees/{branch}/app/node_modules', kind='symlink' if target else 'directory',
                    status=status, target=target, reason=None, referenced_by=[], directory_bytes=100000,
                    shared_bytes=100000-(freed or 0), known_shared_bytes=0, unknown_bytes=0,
                    reclaimable_bytes=freed, reclaimable_min_bytes=freed, reclaimable_max_bytes=freed)
    held=row('共享 [red]', 0, 'retained')
    small=row('small', 8000)
    big=row('big', 70000)
    big['directory_bytes']=90000
    alias=row('alias',None,'retained',target=held['path'])
    alias2=row('another',None,'retained',target=held['path'])
    held['referenced_by']=[alias['path'],alias2['path']]
    return dict(mode='measured',entries=[small,held,big,alias,alias2],errors=[],external_targets={})


class SnapshotTests(unittest.TestCase):
    def test_read_only_replay_does_not_scan_or_apply(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'saved.json'
            p.write_text(json.dumps({'node_modules': fixture()}))
            with patch('clean_my_agent.dependency_report.inspect',side_effect=AssertionError('rescanned')):
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(main(['deps','--from-report',str(p),'--table']),0)
                self.assertIn('2 个链接指向 1 个目标',out.getvalue())
                with patch('sys.stderr', new=io.StringIO()), patch('clean_my_agent.vault.save',side_effect=AssertionError):
                    self.assertNotEqual(main(['deps','--from-report',str(p),'--apply']),0)
            d=fixture();d['entries'][0]['reclaimable_bytes']=-1
            p.write_text(json.dumps(d))
            with self.assertRaises(ValueError):
                storage_view.load_report(p)


@unittest.skipUnless(StorageApp, 'install .[ui] to exercise Textual')
class InteractiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_sort_search_detail_and_resize(self):
        app=StorageApp(fixture())
        async with app.run_test(size=(140,55)) as pilot:
            self.assertEqual(app.rows[0]['path'].split('/')[-3],'big')
            await pilot.press('s')
            self.assertEqual(app.rows[0]['path'].split('/')[-3],'small')
            await pilot.press('2','enter')
            self.assertIsInstance(app.screen,DetailScreen)
            self.assertIn('/tmp/worktrees/alias/app/node_modules',app.screen.body.plain)
            self.assertIn('[red]',app.screen.body.plain)
            await pilot.resize_terminal(48,16)
            await pilot.press('escape','3')
            grid=app.query_one(DataTable)
            self.assertEqual(grid.row_count,1)
            self.assertEqual(len(app.rows[0]['sources']),2)
            await pilot.press('1','slash','s','m','a','l','l','enter')
            self.assertEqual(app.query_one(Input).value,'small')
            self.assertEqual(grid.row_count,1)
            await pilot.press('escape')
            self.assertEqual(grid.row_count,2)
            self.assertEqual(len(grid.columns),2)
            await pilot.press('q')

    def test_actual_shell_replay_and_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'fixture.json'
            source.write_text(json.dumps(fixture()))
            env={**os.environ,'TERM':'xterm-256color','COLORTERM':'truecolor','HOME':tmp}
            env.pop('NO_COLOR',None)
            m,s=pty.openpty()
            try:
                fcntl.ioctl(s,termios.TIOCSWINSZ,struct.pack('HHHH',28,110,0,0))
                prior=termios.tcgetattr(s)
                proc=subprocess.Popen([sys.executable,'-m','clean_my_agent','deps','--from-report',source],env=env,
                                      stdin=s,stdout=s,stderr=s)
                try:
                    data=b''
                    def drain_until(check):
                        nonlocal data
                        end=time.monotonic()+6
                        while not check() and time.monotonic()<end:
                            if select.select([m],[],[],0.1)[0]:
                                data+=os.read(m,65536)
                        self.assertTrue(check(), data[-600:])
                    drain_until(lambda: '预计释放'.encode() in data)
                    os.write(m,b'q')
                    drain_until(lambda: proc.poll() is not None)
                    self.assertEqual(proc.returncode,0)
                    self.assertIn(b'\x1b[?1049l',data)
                    self.assertEqual(prior[3] & (termios.ECHO|termios.ICANON),
                                     termios.tcgetattr(s)[3] & (termios.ECHO|termios.ICANON))
                finally:
                    if proc.poll() is None:
                        proc.kill();proc.wait()
            finally:
                os.close(m);os.close(s)
