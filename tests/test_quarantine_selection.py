import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from clean_my_agent.dashboard import DashboardService
from clean_my_agent.quarantine_actions import QuarantineActions


class QuarantineSelection(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name).resolve()
        env = patch.dict(os.environ, {'HOME':str(home), 'CODEX_HOME':str(home/'codex'),
            'CLAUDE_CONFIG_DIR':str(home/'claude'), 'T3_HOME':str(home/'t3'),
            'XDG_STATE_HOME':str(home/'state'), 'XDG_CONFIG_HOME':str(home/'config')})
        env.start(); self.addCleanup(env.stop)
        self.service = DashboardService()
        self.actions = QuarantineActions(self.service.roots,self.service.state)
        self.paths = [home/'codex/log'/f'{n}.log' for n in range(2)]
        for path in self.paths:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('fixture'); os.utime(path,(1,1))
        selected = self.service.clean_preview('codex')['items']
        self.service.stage_files('codex',selected,Mock())

    def test_delete_one_entry_preserves_sibling_and_restores_it(self):
        rows = self.actions.list()['items']
        self.assertEqual(len(rows),2)
        preview = self.actions.preview('purge',rows[:1],Mock())
        self.assertEqual(self.actions.apply(preview,Mock())['count'],1)
        remaining = self.actions.list()['items']
        self.assertEqual(remaining,rows[1:])
        preview = self.actions.preview('restore',remaining,Mock())
        self.assertEqual(self.actions.apply(preview,Mock())['count'],1)
        self.assertFalse(Path(rows[0]['path']).exists())
        self.assertTrue(Path(rows[1]['path']).exists())
        self.assertEqual(self.actions.list()['items'],[])

    def test_missing_entry_or_changed_policy_stops_before_deleting_sibling(self):
        rows = self.actions.list()['items']
        preview = self.actions.preview('purge',rows,Mock())
        self.actions.apply(self.actions.preview('restore',rows[:1],Mock()),Mock())
        with self.assertRaisesRegex(ValueError,'变化'):
            self.actions.apply(preview,Mock())
        self.assertEqual(self.actions.list()['items'],rows[1:])
        preview = self.actions.preview('purge',rows[1:],Mock())
        config = Path(os.environ['XDG_CONFIG_HOME'])/'clean-my-agent/policy.json'
        config.parent.mkdir(parents=True);config.write_text('{"protect":["codex:log"]}')
        with self.assertRaisesRegex(ValueError,'保护'):
            self.actions.apply(preview,Mock())
        self.assertEqual(len(self.actions.list()['items']),1)

    def test_invalid_or_mismatched_selection_never_deletes(self):
        rows = self.actions.list()['items']
        for selected in ([], [rows[0],rows[0]], [dict(rows[0],index=-1)],
                         [dict(rows[0],path='/different/path')]):
            with self.assertRaises(ValueError):
                self.actions.preview('purge',selected,Mock())
        self.assertEqual(self.actions.list()['items'],rows)


from clean_my_agent.storage_tui import StorageApp
from clean_my_agent.tui_common import ActionScreen
from clean_my_agent.vault_tui import VaultScreen
from textual.widgets import Button, Input
from test_storage_ui import fixture


class QuarantineKeyboard(unittest.IsolatedAsyncioTestCase):
    async def settle(self, app, pilot, condition):
        for _ in range(100):
            if condition() and not app.operation_busy:
                await pilot.pause(.05)
                return
            await pilot.pause(.03)
        self.fail('UI did not settle')

    async def test_stage_list_keyboard_confirm_and_cancel_on_small_screen(self):
        controller=Mock()
        doc=fixture();chosen=doc['entries'][2]
        controller.stage.return_value=dict(batch='a'*16,moved_items=1,errors=[])
        app=StorageApp(doc,actions=controller)
        async with app.run_test(size=(44,16)) as pilot:
            await pilot.press('space','t')
            self.assertIsInstance(app.screen,ActionScreen)
            self.assertIn(chosen['path'],app.screen.body.plain)
            self.assertNotIn('安装检查',app.screen.body.plain)
            self.assertEqual(app.focused.id,'execute')
            self.assertLess(app.screen.query_one('#execute').region.y,16)
            await pilot.press('left','enter')
            controller.stage.assert_not_called()
            await pilot.press('t')
            screen=app.screen
            await pilot.press('enter')
            await self.settle(app,pilot,lambda:screen.finished)
            controller.stage.assert_called_once()
            await pilot.press('enter')
            self.assertIs(app.screen,app.screen_stack[0])

    async def test_vault_space_selection_delete_phrase_and_no_input_leak(self):
        controller=Mock();actions=controller.quarantine.return_value
        rows=[dict(batch='b'*16,index=i,path=f'/demo/old-{i}.log',bytes=4096,created='now') for i in range(2)]
        actions.list.return_value=dict(items=rows,errors=[])
        actions.preview.side_effect=lambda operation,selected,p:dict(operation=operation,selected=selected,batches=[])
        actions.apply.return_value=dict(count=1,errors=[])
        app=StorageApp(fixture(),actions=controller)
        async with app.run_test(size=(100,30)) as pilot:
            await pilot.press('space')
            selected=set(app.checked)
            await pilot.press('v')
            await self.settle(app,pilot,lambda:isinstance(app.screen,VaultScreen) and len(app.screen.rows)==2)
            screen=app.screen
            await pilot.press('d')
            actions.preview.assert_not_called()
            await pilot.press('space','d')
            await self.settle(app,pilot,lambda:isinstance(app.screen,ActionScreen))
            self.assertIn(rows[0]['path'],app.screen.body.plain)
            self.assertNotIn(rows[1]['path'],app.screen.body.plain)
            await pilot.press('enter')
            actions.apply.assert_not_called()
            await pilot.press('escape','d')
            await self.settle(app,pilot,lambda:isinstance(app.screen,ActionScreen))
            self.assertEqual(app.focused.id,'confirmation')
            await pilot.press('D','E','L','E','T','E')
            self.assertEqual(app.filter_text,'')
            self.assertEqual(app.checked,selected)
            modal=app.screen
            actions.list.return_value=dict(items=rows[1:],errors=[])
            await pilot.press('enter')
            await self.settle(app,pilot,lambda:modal.finished)
            self.assertEqual(actions.apply.call_args.args[0]['selected'],rows[:1])
            await pilot.press('enter')
            await self.settle(app,pilot,lambda:app.screen is screen and len(screen.rows)==1)
            self.assertFalse(screen.checked)
            await pilot.press('escape')
            self.assertIs(app.screen,app.screen_stack[0])

    async def test_snapshot_cannot_open_quarantine(self):
        controller=Mock()
        app=StorageApp(fixture(),source='snapshot.json',actions=controller)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.press('v','space','t','d')
            controller.quarantine.assert_not_called()
            self.assertIs(app.screen,app.screen_stack[0])
