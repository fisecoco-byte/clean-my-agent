import unittest
from unittest.mock import Mock

from clean_my_agent.storage_tui import StorageApp
from clean_my_agent.tui_common import ActionScreen
from textual.widgets import Button, DataTable, Input
from test_storage_ui import fixture


def report():
    data = fixture()
    seed = data['entries'][0]
    candidates = [dict(seed,path=f'/demo/worktrees/one/app/packages/pkg{i:02}/dist') for i in range(49)]
    other = dict(seed,path='/demo/worktrees/two/app/dist')
    held = dict(data['entries'][1],path='/demo/worktrees/one/app/shared/node_modules')
    data['entries'] = [*candidates, other, held]
    data['projects'] = [dict(path=f'/demo/worktrees/{name}/app',agent='codex',artifacts=count,directory_bytes=100000)
                        for name,count in [('one',50),('two',1)]]
    return data


class SelectAllUI(unittest.IsolatedAsyncioTestCase):
    async def test_click_selects_scrolled_candidates_only_in_current_worktree(self):
        controller = Mock()
        app = StorageApp(report(),actions=controller)
        async with app.run_test(size=(120,26)) as pilot:
            await pilot.press('w','p','space')
            self.assertEqual(len(app.checked),1)
            self.assertEqual(len(app.rows),49)
            self.assertLessEqual(app.query_one('#select-all',Button).region.bottom,
                                 app.query_one(DataTable).region.y)
            await pilot.click('#select-all')
            self.assertEqual(len(app.checked),49)
            self.assertEqual(str(app.query_one('#select-all',Button).label),'a 取消全选')
            self.assertIsInstance(app.focused,DataTable)
            await pilot.pause(.25)  # Wait for the button's active feedback before another click.
            self.assertIn('取消全选',app.query_one('#select-all',Button).render_line(0).text)
            await pilot.click('#select-all')
            self.assertFalse(app.checked)
            await pilot.press('a','s','t')
            self.assertIsInstance(app.screen,ActionScreen)
            self.assertIn('已选 49 项',app.screen.body.plain)
            self.assertIn('/pkg48/dist',app.screen.body.plain)
            self.assertNotIn('/two/',app.screen.body.plain)
            self.assertNotIn('/shared/',app.screen.body.plain)
            controller.stage_many.assert_not_called()
            await pilot.press('escape','ctrl+a')
            self.assertFalse(app.checked)

    async def test_search_input_and_filtered_all_in_narrow_terminal(self):
        controller=Mock()
        app=StorageApp(report(),actions=controller)
        async with app.run_test(size=(44,16)) as pilot:
            await pilot.press('slash','a','ctrl+a')
            self.assertFalse(app.checked)
            search=app.query_one('#search',Input)
            search.value='pkg0'
            await pilot.press('enter')
            self.assertEqual(len(app.rows),10)
            await pilot.click('#select-all')
            self.assertEqual(len(app.checked),10)
            self.assertLessEqual(app.query_one('#select-all',Button).region.bottom,
                                 app.query_one(DataTable).region.y)
            # All three actions stay within the supported narrow terminal.
            for selector in ('#select-all','#stage','#vault'):
                button=app.query_one(selector,Button)
                self.assertGreater(button.region.width,0)
                self.assertLessEqual(button.region.right,app.size.width-2)
                self.assertLessEqual(button.region.bottom,app.size.height-1)
            await pilot.pause(.25)
            await pilot.click('#select-all')
            self.assertFalse(app.checked)
            await pilot.press('a')
            self.assertEqual(len(app.checked),10)
            await pilot.press('2','a')
            self.assertFalse(app.checked)
            self.assertTrue(app.query_one('#select-all',Button).disabled)
            controller.stage_many.assert_not_called()

    async def test_snapshot_errors_and_single_item_controller_disable_all(self):
        for mode in ('snapshot','errors','single'):
            with self.subTest(mode=mode):
                data=report()
                if mode=='errors':
                    data['errors']=['盘点不完整']
                controller=Mock(spec=['stage']) if mode=='single' else Mock()
                app=StorageApp(data,actions=controller,source='snapshot.json' if mode=='snapshot' else None)
                async with app.run_test(size=(80,26)) as pilot:
                    self.assertTrue(app.query_one('#select-all',Button).disabled)
                    await pilot.press('a','ctrl+a')
                    self.assertFalse(app.checked)
                    controller.stage.assert_not_called()
