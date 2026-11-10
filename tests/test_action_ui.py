import unittest
from unittest.mock import Mock

try:
    from clean_my_agent.session_tui import SessionApp
    from clean_my_agent.storage_tui import StorageApp
    from clean_my_agent.tui_common import ActionScreen
    from textual.widgets import Button, DataTable, Input
except ImportError:
    SessionApp = None

from test_storage_ui import fixture
from clean_my_agent.inventory import BUCKETS


def sessions():
    rows = {}
    doc = dict(limit=1,agents={})
    for agent in ('codex','claude'):
        rows[agent] = [dict(session_id=f'01234567-89ab-cdef-9012-{i:012x}', title=f'任务 {i} [red]',
                           state='archived' if i==3 else 'open', route='delete' if i==3 else 'archive',
                           idle_days=40-i, bytes=4096, latest_ns=1, hold_reason=None) for i in (1,2,3)]
        doc['agents'][agent] = dict(policy={'archive_days':10,'delete_days':20}, errors=[], session_count=3,
            groups={}, histogram=dict(zip(BUCKETS,[0,0,0,0,3])))
    return doc,rows


@unittest.skipUnless(SessionApp, 'install optional UI dependencies')
class ActionUI(unittest.IsolatedAsyncioTestCase):
    async def finished(self, pilot, screen):
        for _ in range(50):
            if screen.finished: return
            await pilot.pause(.05)
        self.fail('operation did not finish')

    async def test_resource_file_confirmation_and_worktrees_without_artifacts(self):
        doc=fixture(); row=doc['entries'][0]
        row.update(kind='file',path='/tmp/cursor/logs/old.log',label='cursor / logs / old.log')
        doc['entries']=[row]; doc['title']='Agent 缓存 / 版本 / 残留'
        controller=Mock()
        controller.stage.return_value=dict(row=row,batch='0123456789abcdef',moved_items=1,errors=[])
        app=StorageApp(doc,actions=controller)
        async with app.run_test(size=(80,26)) as pilot:
            await pilot.press('t')
            self.assertIn('cursor / logs / old.log',app.screen.body.plain)
            self.assertIn(row['path'],app.screen.body.plain)
            screen=app.screen; await pilot.click('#execute'); await self.finished(pilot,screen)
            controller.stage.assert_called_once()
        doc.update(entries=[],projects=[dict(agent='t3',path='/tmp/worktrees/source-only',directory_bytes=8192,artifacts=0)])
        app=StorageApp(doc)
        async with app.run_test(size=(44,16)) as pilot:
            await pilot.press('w')
            self.assertEqual(app.screen.rows[0]['path'],'/tmp/worktrees/source-only')

    async def test_session_selection_confirmation_and_delete_phrase(self):
        doc,rows=sessions()
        controller=Mock()
        controller.apply.return_value={'results':[]}
        controller.refresh.return_value=(doc,rows)
        app=SessionApp(doc,rows,actions=controller)
        async with app.run_test(size=(140,36)) as pilot:
            self.assertEqual(len(app.rows),2)  # UI is not truncated by the print-report limit.
            await pilot.press('space','down','space','x')
            self.assertIsInstance(app.screen,ActionScreen)
            self.assertIn(rows['codex'][0]['session_id'],app.screen.body.plain)
            self.assertIn('任务 2 [red]',app.screen.body.plain)
            self.assertIn('codex archive',app.screen.body.plain)
            await pilot.press('escape')
            controller.apply.assert_not_called()
            await pilot.press('x')
            screen=app.screen
            await pilot.click('#execute')
            await self.finished(pilot,screen)
            self.assertEqual(controller.apply.call_args.args[0],'archive')
            self.assertEqual(len(controller.apply.call_args.args[1]),2)
            await pilot.press('escape','d','space','x')
            self.assertTrue(app.screen.query_one('#execute',Button).disabled)
            field=app.screen.query_one('#confirmation',Input)
            field.value='delete'
            await pilot.pause()
            self.assertTrue(app.screen.query_one('#execute',Button).disabled)
            field.value='DELETE'
            await pilot.pause()
            screen=app.screen
            await pilot.click('#execute')
            await self.finished(pilot,screen)
            self.assertEqual(controller.apply.call_count,2)
            self.assertEqual(controller.apply.call_args.args[0],'delete')
            await pilot.press('escape','2','space','x')
            self.assertEqual(controller.apply.call_count,2)
            self.assertFalse(app.checked)

    async def test_switch_search_and_threshold_clear_selection(self):
        doc,rows=sessions()
        app=SessionApp(doc,rows,actions=Mock())
        async with app.run_test(size=(90,30)) as pilot:
            await pilot.press('space')
            self.assertEqual(len(app.checked),1)
            await pilot.press('slash','1')
            self.assertFalse(app.checked)
            await pilot.press('enter','space')
            self.assertEqual(len(app.checked),1)
            await pilot.click('#archive_days')
            self.assertFalse(app.checked)
            self.assertEqual(app.rules['codex']['archive_days'],15)
            await pilot.resize_terminal(48,18)
            await pilot.press('escape')
            self.assertEqual(app.query_one(DataTable).row_count,2)

    async def test_dependency_cancel_confirm_and_snapshot_guards(self):
        controller=Mock()
        doc=fixture()
        chosen=doc['entries'][2]
        controller.stage.return_value=dict(row=chosen,batch='0123456789abcdef',moved_items=1,errors=[])
        app=StorageApp(doc,actions=controller)
        async with app.run_test(size=(140,36)) as pilot:
            await pilot.press('t')
            self.assertIn(chosen['path'],app.screen.body.plain)
            await pilot.press('escape')
            controller.stage.assert_not_called()
            await pilot.press('t')
            screen=app.screen
            await pilot.click('#execute')
            await self.finished(pilot,screen)
            self.assertIn('restore 0123456789abcdef',app.operation_results[0])
            await pilot.press('escape')
            self.assertEqual(len(app.rows),1)
            await pilot.press('2','t')
            self.assertEqual(controller.stage.call_count,1)
        app=StorageApp(fixture(),actions=controller,source='snapshot.json')
        async with app.run_test(size=(100,30)) as pilot:
            await pilot.press('t')
            self.assertNotIsInstance(app.screen,ActionScreen)
            self.assertEqual(controller.stage.call_count,1)
