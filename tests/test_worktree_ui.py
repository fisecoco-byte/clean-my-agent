import unittest
from unittest.mock import Mock
from pathlib import Path
from clean_my_agent.storage_tui import StorageApp
from clean_my_agent.worktree_tui import WorktreeScreen
from clean_my_agent.tui_common import ActionScreen, DetailScreen
from textual.widgets import Button, DataTable, Input, Static
from test_storage_ui import fixture


def report():
    doc=fixture()
    one=doc['entries'][0].copy(); two=one.copy()
    one['path']='/demo/worktrees/one/app/target'; two['path']='/demo/worktrees/one/app/build'
    three=one.copy(); three['path']='/demo/worktrees/two/app/target'
    doc['entries']=[one,two,three]
    doc['projects']=[dict(path=f'/demo/worktrees/{name}/app',agent='codex',directory_bytes=100000,artifacts=count)
                     for name,count in [('one',2),('two',1)]]
    return doc


class WorktreeUI(unittest.IsolatedAsyncioTestCase):
    async def wait_screen(self,app,pilot,kind):
        for _ in range(90):
            if isinstance(app.screen,kind) and not app.operation_busy:
                await pilot.pause(.05)
                if isinstance(app.screen,kind):
                    return
            await pilot.pause(.03)
        self.fail('screen did not settle: '+str(type(app.screen)))

    async def finished(self,app,pilot,screen):
        for _ in range(90):
            if screen.finished:return
            await pilot.pause(.03)
        self.fail('operation did not settle')

    async def test_worktree_details_scoped_multi_selection_and_confirmation(self):
        doc=report(); controller=Mock()
        controller.worktree_details.return_value=dict(doc['projects'][0],can_delete=True,token='fixture',checks={'未提交改动':'通过'},reasons=[])
        controller.stage_many.return_value=dict(batch='a'*16,moved_items=2,moved_paths=[r['path'] for r in doc['entries'][:2]],errors=[])
        app=StorageApp(doc,actions=controller,start_worktrees=True)
        async with app.run_test(size=(110,30)) as pilot:
            await self.wait_screen(app,pilot,WorktreeScreen)
            self.assertEqual(app.screen.query_one(DataTable).row_count,2)
            await pilot.press('enter'); await self.wait_screen(app,pilot,DetailScreen)
            self.assertIn('未提交改动',app.screen.body.plain)
            controller.delete_worktree.assert_not_called()
            await pilot.press('escape','p')
            self.assertIs(app.screen,app.screen_stack[0])
            self.assertEqual(len(app.rows),2)
            self.assertTrue(all('/one/' in r['path'] for r in app.rows))
            await pilot.press('space','down','space','t')
            self.assertIsInstance(app.screen,ActionScreen)
            self.assertIn('/one/app/target',app.screen.body.plain)
            self.assertIn('/one/app/build',app.screen.body.plain)
            self.assertNotIn('/two/',app.screen.body.plain)
            await pilot.press('escape')
            controller.stage_many.assert_not_called()
            await pilot.press('t'); screen=app.screen
            await pilot.click('#execute'); await self.finished(app,pilot,screen)
            self.assertEqual(len(controller.stage_many.call_args.args[0]),2)
            await pilot.press('escape','w')
            self.assertEqual(app.screen.query_one(DataTable).row_count,2)
            self.assertFalse(app.checked)

    async def test_delete_phrase_native_result_and_refresh(self):
        doc=report(); controller=Mock()
        preview=dict(doc['projects'][0],can_delete=True,token='fixture',checks={'引用关系':'通过'},reasons=[],ignored=['target'])
        controller.worktree_details.return_value=preview
        controller.delete_worktree.return_value=dict(status='succeeded',path=preview['path'],message='已通过 Git 删除工作树')
        app=StorageApp(doc,actions=controller,start_worktrees=True)
        async with app.run_test(size=(100,28)) as pilot:
            await self.wait_screen(app,pilot,WorktreeScreen)
            await pilot.press('d'); await self.wait_screen(app,pilot,ActionScreen)
            self.assertEqual(app.screen.phrase,'DELETE')
            self.assertIn(preview['path'],app.screen.body.plain)
            self.assertTrue(app.screen.query_one('#execute',Button).disabled)
            await pilot.press('escape')
            controller.delete_worktree.assert_not_called()
            await pilot.press('d'); await self.wait_screen(app,pilot,ActionScreen)
            app.screen.query_one(Input).value='DELETE'; await pilot.pause()
            screen=app.screen; await pilot.click('#execute'); await self.finished(app,pilot,screen)
            controller.delete_worktree.assert_called_once()
            await pilot.press('escape')
            self.assertEqual(app.screen.query_one(DataTable).row_count,1)
            self.assertEqual(len(doc['entries']),1)
            self.assertTrue(app.operation_results)

    async def test_snapshot_and_retained_worktree_have_no_delete_executor(self):
        controller=Mock(); doc=report()
        app=StorageApp(doc,actions=controller,source='snapshot.json')
        async with app.run_test(size=(44,16)) as pilot:
            await pilot.press('w')
            self.assertIsInstance(app.screen,WorktreeScreen)
            self.assertTrue(app.screen.query_one('#wt-delete',Button).disabled)
            await pilot.press('d')
            self.assertIsInstance(app.screen,DetailScreen)
            self.assertIn('只读查看',app.screen.body.plain)
            controller.worktree_details.assert_not_called()
            await pilot.press('escape','p','space','t')
            self.assertFalse(app.checked)
            controller.stage.assert_not_called()
        controller.worktree_details.return_value=dict(doc['projects'][0],can_delete=False,reasons=['未提交改动'])
        app=StorageApp(doc,actions=controller,start_worktrees=True)
        async with app.run_test(size=(44,16)) as pilot:
            await self.wait_screen(app,pilot,WorktreeScreen)
            await pilot.press('d'); await self.wait_screen(app,pilot,DetailScreen)
            self.assertIn('未提交改动',app.screen.body.plain)
            controller.delete_worktree.assert_not_called()

    async def test_delete_blocker_is_prominent_and_follows_selected_directory(self):
        doc=report(); controller=Mock()
        reason='主工作区或独立仓库保留；仅删除附加 worktree'
        blocked=dict(doc['projects'][0],can_delete=False,reasons=[reason])
        allowed=dict(doc['projects'][0],can_delete=True,reasons=[],token='fresh-check')
        controller.worktree_details.side_effect=[blocked,allowed,blocked]
        app=StorageApp(doc,actions=controller,start_worktrees=True)
        async with app.run_test(size=(100,28)) as pilot:
            await self.wait_screen(app,pilot,WorktreeScreen)
            screen=app.screen
            await pilot.press('d'); await self.wait_screen(app,pilot,DetailScreen)
            self.assertIn('当前不能删除',app.screen.heading)
            body=app.screen.body.plain
            self.assertLess(body.index(reason),body.index('目录占用'))
            await pilot.press('escape')
            self.assertIn(reason,str(screen.query_one('#wt-status',Static).render()))
            self.assertEqual(str(screen.query_one('#wt-delete',Button).label),'d 原因')
            await pilot.press('down')
            self.assertNotIn(reason,str(screen.query_one('#wt-status',Static).render()))
            self.assertEqual(str(screen.query_one('#wt-delete',Button).label),'d 清理')
            await pilot.press('up')
            self.assertIn(reason,str(screen.query_one('#wt-status',Static).render()))
            await pilot.press('d'); await self.wait_screen(app,pilot,ActionScreen)
            self.assertEqual(controller.worktree_details.call_count,2)
            self.assertEqual(app.screen.phrase,'DELETE')
            controller.delete_worktree.assert_not_called()
            await pilot.press('escape','d'); await self.wait_screen(app,pilot,DetailScreen)
            self.assertEqual(controller.worktree_details.call_count,3)
            controller.delete_worktree.assert_not_called()
            await pilot.press('escape')
            screen.replace_report(); await pilot.pause()
            self.assertNotIn(reason,str(screen.query_one('#wt-status',Static).render()))

    async def test_delete_after_incomplete_scan_explains_retry(self):
        controller=Mock()
        app=StorageApp(report(),actions=controller,start_worktrees=True)
        async with app.run_test(size=(80,24)) as pilot:
            await self.wait_screen(app,pilot,WorktreeScreen)
            app.scan_complete=False
            app.screen.show_status()
            self.assertTrue(app.screen.query_one('#wt-delete',Button).disabled)
            await pilot.press('d')
            self.assertIsInstance(app.screen,DetailScreen)
            self.assertIn('请按 r 重新扫描',app.screen.body.plain)
            controller.worktree_details.assert_not_called()
            controller.delete_worktree.assert_not_called()

    async def test_standalone_repository_uses_trash_confirmation_and_executor(self):
        doc=report(); controller=Mock()
        preview=dict(doc['projects'][0],repository_kind='standalone',can_trash=True,can_delete=False,
                     token='fixture',reasons=[])
        controller.worktree_details.return_value=preview
        controller.trash_repository.return_value=dict(status='succeeded',path=preview['path'],
            trash_path='/demo/.Trash/app',message='已移入系统废纸篓，尚未释放空间')
        app=StorageApp(doc,actions=controller,start_worktrees=True)
        async with app.run_test(size=(100,28)) as pilot:
            await self.wait_screen(app,pilot,WorktreeScreen)
            screen=app.screen
            await pilot.press('enter'); await self.wait_screen(app,pilot,DetailScreen)
            self.assertIn('独立仓库',app.screen.body.plain)
            await pilot.press('escape')
            self.assertEqual(str(screen.query_one('#wt-delete',Button).label),'d 废纸篓')
            await pilot.press('d'); await self.wait_screen(app,pilot,ActionScreen)
            self.assertEqual(app.screen.heading,'确认移入废纸篓')
            self.assertEqual(app.screen.phrase,'TRASH')
            self.assertIn('Git 历史',app.screen.body.plain)
            self.assertIn('不释放空间',app.screen.body.plain)
            self.assertIn('移入废纸篓',app.screen.query_one(Input).placeholder)
            self.assertNotIn('永久删除',app.screen.query_one(Input).placeholder)
            app.screen.query_one(Input).value='DELETE'; await pilot.pause()
            self.assertTrue(app.screen.query_one('#execute',Button).disabled)
            await pilot.press('escape')
            controller.trash_repository.assert_not_called()
            await pilot.press('d'); await self.wait_screen(app,pilot,ActionScreen)
            app.screen.query_one(Input).value='TRASH'; await pilot.pause()
            action=app.screen; await pilot.press('enter'); await self.finished(app,pilot,action)
            controller.trash_repository.assert_called_once_with(preview,unittest.mock.ANY)
            controller.delete_worktree.assert_not_called()
            self.assertTrue(app.cache_invalidated)
            await pilot.press('escape')
            self.assertEqual(screen.query_one(DataTable).row_count,1)
            self.assertEqual(len(doc['entries']),1)
