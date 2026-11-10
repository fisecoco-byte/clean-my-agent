import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from clean_my_agent import activity, dependency_report, inventory, policy
from clean_my_agent.policy import DAY_NS


class ActivityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.now = time.time_ns()

    def old_file(self, path, days=20, text='source'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        stamp = self.now - days * DAY_NS
        os.utime(path, ns=(stamp, stamp))
        return path

    def test_boundaries_unknown_and_future(self):
        for days, expected in [(0,'high'),(4,'high'),(5,'medium'),(9,'medium'),(10,'low'),(14,'low'),(15,'very_low'),(20,'very_low')]:
            with self.subTest(days=days):
                result = activity.classify(self.now-days*DAY_NS, now_ns=self.now)
                self.assertEqual(result['level'], expected)
                self.assertEqual(result['idle_days'], days)
        for stamp in (None, 0, self.now+1, 'bad'):
            self.assertEqual(activity.classify(stamp, now_ns=self.now)['level'], 'unknown')
        self.assertEqual(activity.classify(self.now-5*DAY_NS+1, now_ns=self.now)['level'], 'high')

    def test_reading_and_rebuilt_dependencies_do_not_refresh_workspace(self):
        project = self.root/'app'
        self.old_file(project/'src/main.py')
        self.old_file(project/'package.json', text='{}')
        self.old_file(project/'node_modules/new-install/index.js', days=0)
        self.old_file(project/'dist/bundle.js', days=0)
        before = (project/'src/main.py').stat().st_mtime_ns
        for _ in range(2):
            result = activity.workspace(project, now_ns=self.now)
            self.assertEqual(result['level'], 'very_low')
        self.assertEqual((project/'src/main.py').stat().st_mtime_ns, before)

    def test_linked_worktree_uses_own_git_metadata_and_session_cwd(self):
        project = self.root/'app'
        gitdir = self.root/'repo/.git/worktrees/app'
        self.old_file(project/'main.py')
        self.old_file(project/'.git', text=f'gitdir: {gitdir}')
        self.old_file(gitdir/'HEAD')
        self.old_file(gitdir/'logs/HEAD')
        self.old_file(self.root/'repo/.git/logs/HEAD', days=0)
        result = activity.workspace(project, now_ns=self.now)
        self.assertEqual(result['level'], 'very_low')
        result = activity.workspace(project, now_ns=self.now,
            records=[(project/'src', self.now-6*DAY_NS, 'Codex 会话索引'),
                     (self.root/'app-other', self.now, '其他项目')])
        self.assertEqual(result['level'], 'medium')
        self.assertEqual(result['basis'], 'Codex 会话索引')

    def test_unknown_last_in_both_directions_and_running_override(self):
        rows = [dict(path=str(i), activity=activity.classify(stamp, now_ns=self.now))
                for i, stamp in enumerate([None, self.now, self.now-20*DAY_NS])]
        self.assertEqual([r['path'] for r in sorted(rows, key=activity.sort_key)], ['2','1','0'])
        self.assertEqual([r['path'] for r in sorted(rows, key=lambda r: activity.sort_key(r,'hot'))], ['1','2','0'])
        self.assertEqual(activity.workspace(self.root, now_ns=self.now)['level'], 'unknown')
        self.assertEqual(activity.workspace(self.root, now_ns=self.now, runtime={'state':'busy'})['level'], 'high')
        self.old_file(self.root/'main.py')
        self.assertEqual(activity.workspace(self.root, now_ns=self.now, limit=0)['level'], 'unknown')
        self.assertEqual(activity.workspace(self.root, now_ns=self.now, warnings=['索引不可读'])['level'], 'unknown')
        for invalid in (None, [], {'level': []}, {'level':'high', 'idle_days':'20'}, {'level':'low', 'basis':{}}):
            row = {'activity':invalid}
            self.assertEqual(activity.badge(row), '未知')
            self.assertTrue(activity.sort_key(row)[0])
            self.assertIn('缺少活动记录', activity.describe(row))

    def test_codex_index_recent_file_and_claude_explicit_project_path(self):
        codex = self.root/'.codex'; codex.mkdir()
        project = self.root/'app'
        session = self.old_file(codex/'sessions/chat.jsonl', days=2)
        with sqlite3.connect(codex/'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id, archived, cwd, updated_at_ms, rollout_path)')
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?)', ('id',0,str(project),(self.now-20*DAY_NS)//10**6,str(session)))
        dbstamp = (codex/'state_5.sqlite').stat().st_mtime_ns
        records, warnings = activity.session_records({'codex':codex})
        self.assertFalse(warnings)
        self.assertEqual(records[0][:2], (project,self.now-2*DAY_NS))
        self.assertEqual(inventory.thread_index(codex)[0]['id'][2], ((self.now-20*DAY_NS)//10**6)*10**6)
        self.assertEqual((codex/'state_5.sqlite').stat().st_mtime_ns, dbstamp)
        claude = self.root/'.claude'
        self.old_file(claude/'projects/encoded/sessions-index.json', text=json.dumps({'entries':[
            {'projectPath':str(project),'modified':'2026-01-01T00:00:00Z'},
            {'modified':'2026-01-01T00:00:00Z'}]}))
        records, warnings = activity.session_records({'claude':claude})
        self.assertFalse(warnings)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][0], project)

    def test_plain_project_discovery_and_artifact_inheritance(self):
        root = self.root/'.codex'
        project = root/'projects/demo'
        self.old_file(project/'package.json', text='{}')
        self.old_file(project/'node_modules/pkg/index.js')
        self.old_file(project/'packages/inner/package.json', text='{}')
        report = dependency_report.inspect(root, policy.load(), full=False, activity_records=([], []))
        self.assertEqual([r['path'] for r in report['projects']], [str(project)])
        self.assertEqual(report['projects'][0]['activity']['level'], 'very_low')
        row = report['entries'][0]
        self.assertEqual(row['activity']['workspace'], str(project))
        self.assertNotEqual(row['status'], 'candidate')


try:
    from clean_my_agent.storage_tui import StorageApp
    from clean_my_agent.session_tui import SessionApp
    from clean_my_agent.worktree_tui import WorktreeScreen
    from textual.widgets import DataTable
except ImportError:
    StorageApp = None


@unittest.skipUnless(StorageApp, 'install UI dependencies')
class ActivityUI(unittest.IsolatedAsyncioTestCase):
    async def test_session_sort_preserves_checked_id_and_focus(self):
        from test_action_ui import sessions
        doc, rows = sessions()
        now = time.time_ns()
        for row, days in zip(rows['codex'], (25,15,30)):
            row['activity'] = activity.classify(now-days*DAY_NS, now_ns=now)
        controller = Mock()
        app = SessionApp(doc,rows,actions=controller)
        async with app.run_test(size=(140,36)) as pilot:
            chosen = app.rows[0]['session_id']
            await pilot.press('space','s')
            self.assertEqual(app.rows[0]['session_id'],rows['codex'][1]['session_id'])
            self.assertEqual(app.checked, {chosen})
            self.assertEqual(app.rows[app.query_one(DataTable).cursor_row]['session_id'],chosen)
            await pilot.resize_terminal(44,16)
            await pilot.press('x')
            self.assertIn(chosen,app.screen.body.plain)
            self.assertNotIn(rows['codex'][1]['session_id'],app.screen.body.plain)
            controller.apply.assert_not_called()

    async def test_worktree_sort_preserves_target_in_narrow_terminal(self):
        from test_worktree_ui import report
        doc = report(); now = time.time_ns()
        for row,days in zip(doc['projects'],(20,2)):
            row['activity'] = activity.classify(now-days*DAY_NS, now_ns=now)
        app = StorageApp(doc,start_worktrees=True)
        async with app.run_test(size=(80,28)) as pilot:
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen,WorktreeScreen)
            selected = screen.selected()['path']
            await pilot.press('s')
            self.assertEqual(screen.rows[0]['activity']['level'],'high')
            self.assertEqual(screen.selected()['path'],selected)
            await pilot.resize_terminal(44,16)
            await pilot.pause()
            self.assertEqual(screen.selected()['path'],selected)
            await pilot.press('enter')
            self.assertIn('极低',app.screen.body.plain)

    async def test_artifact_sort_does_not_change_quarantine_selection(self):
        from test_storage_ui import fixture
        doc=fixture(); now=time.time_ns()
        for row,days in zip(doc['entries'],(20,0,2,0,0)):
            row['activity']=activity.classify(now-days*DAY_NS, now_ns=now)
        controller=Mock()
        app=StorageApp(doc,actions=controller)
        async with app.run_test(size=(140,36)) as pilot:
            chosen=app.rows[0]['path']
            await pilot.press('space','s','s')
            self.assertEqual(app.rows[0]['activity']['level'],'very_low')
            self.assertEqual(app.checked,{chosen})
            self.assertEqual(app.rows[app.query_one(DataTable).cursor_row]['path'],chosen)
            await pilot.press('t')
            self.assertIn(chosen,app.screen.body.plain)
            controller.stage_many.assert_not_called()
