import unittest
from pathlib import Path

from clean_my_agent.resources import row

try:
    from clean_my_agent.resource_tui import make_screen
    from clean_my_agent.tui_common import ManagedApp, SHARED_CSS, DetailScreen
    from textual.widgets import DataTable, Input, Button
except ImportError:
    ManagedApp = None


@unittest.skipUnless(ManagedApp, 'UI extra required')
class ResourceUI(unittest.IsolatedAsyncioTestCase):
    async def test_search_versions_details_resize_and_no_write_control(self):
        rows = [row('codex', 'plugin', Path('/fixture/plugins/one')),
                row('claude', 'mcp', Path('/fixture/mcp/two'))]
        rows[0].update(version='1.0', duplicate_group='plugin:one')
        data = dict(entries=rows, errors=[], totals_note='Read only')
        class App(ManagedApp):
            CSS = SHARED_CSS
            resource_only = True
            def on_mount(self):
                self.push_screen(make_screen(data))
        app = App()
        async with app.run_test(size=(132, 32)) as pilot:
            await pilot.pause()
            self.assertEqual(app.screen.query_one(DataTable).row_count, 2)
            await pilot.press('2')
            self.assertEqual(app.screen.query_one(DataTable).row_count, 1)
            await pilot.press('enter')
            self.assertIsInstance(app.screen, DetailScreen)
            await pilot.press('escape', '1', 'slash')
            app.screen.query_one(Input).value = 'two'
            await pilot.pause()
            self.assertEqual(app.screen.query_one(DataTable).row_count, 1)
            await pilot.press('escape')
            self.assertEqual(app.screen.query_one(DataTable).row_count, 2)
            await pilot.resize_terminal(54, 20)
            await pilot.pause()
            self.assertTrue(app.screen.has_class('narrow'))
            self.assertFalse(any(button.id in ('stage','execute','delete') for button in app.screen.query(Button)))
            await pilot.press('q')
