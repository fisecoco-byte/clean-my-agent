import threading
import unittest
from unittest.mock import Mock

from textual.widgets import Button, DataTable, ProgressBar, Static

from clean_my_agent._loading_frames import CAT_FRAMES, CAT_HEIGHT, CAT_PALETTE, CAT_WIDTH, SCAN_FRAMES
from clean_my_agent.dashboard_tui import DashboardApp, LoadingApp
from clean_my_agent.loading_tui import ScanLoading
from test_dashboard import snapshot


class ScanAnimation(unittest.IsolatedAsyncioTestCase):
    async def settle(self, pilot, app):
        for _ in range(70):
            if not app.operation_busy:
                return
            await pilot.pause(.03)
        self.fail('scan did not finish')

    async def test_animation_moves_during_blocked_scan_without_advancing_real_progress(self):
        ready = threading.Event()
        def scan(progress):
            progress('测量依赖目录',1,4,'Codex / demo')
            if not ready.wait(5):
                raise TimeoutError('fixture timed out')
            return snapshot()
        service = Mock(agent='all',snapshot=scan)
        service.selected.return_value={'codex','claude'}
        app=DashboardApp(service)
        try:
            async with app.run_test(size=(110,34)) as pilot:
                await pilot.pause(.15)
                loading=app.query_one(ScanLoading)
                frame=loading.frame
                self.assertTrue(app.operation_busy)
                self.assertFalse(app.query_one('#layout').display)
                await pilot.pause(.25)
                self.assertGreater(loading.frame,frame)
                self.assertEqual((loading.done,loading.total),(1,4))
                self.assertEqual(loading.query_one(ProgressBar).progress,1)
                self.assertEqual(loading.phase,'测量依赖目录')
                self.assertFalse(loading.has_class('compact'))
                self.assertEqual(loading.query_one('.scan-art').size.height,CAT_HEIGHT)
                self.assertFalse(loading.query('.scan-title, .scan-phase, .scan-detail'))
                ready.set()
                await self.settle(pilot,app)
                self.assertFalse(loading.display)
                self.assertIsNone(loading.started)
                self.assertEqual(app.query_one(DataTable).row_count,7)
                frame=loading.frame
                await pilot.pause(.2)
                self.assertEqual(loading.frame,frame)
        finally:
            ready.set()

    async def test_refresh_resize_and_failure_stop_animation_and_allow_retry(self):
        ready=threading.Event()
        def fail(progress):
            progress('核对目录',detail='project\x1b[2J')
            ready.wait(5)
            raise OSError('无法读取示例目录')
        service=Mock(agent='all',snapshot=Mock(side_effect=fail))
        service.selected.return_value={'codex','claude'}
        app=DashboardApp(service,snapshot=snapshot())
        try:
            async with app.run_test(size=(110,34)) as pilot:
                await pilot.press('r')
                await pilot.resize_terminal(44,16)
                await pilot.pause(.15)
                loading=app.query_one(ScanLoading)
                self.assertTrue(app.operation_busy)
                self.assertTrue(loading.display)
                self.assertTrue(loading.has_class('compact'))
                self.assertFalse(loading.query_one(ProgressBar).display)
                self.assertIsNone(loading.total)
                self.assertNotIn('\x1b',loading.detail)
                for widget in loading.query(Static):
                    self.assertLessEqual(widget.region.bottom,app.size.height-1)
                ready.set()
                await self.settle(pilot,app)
                self.assertFalse(loading.display)
                self.assertIn('读取失败',str(app.query_one('#home-message',Static).render()))
                service.snapshot.side_effect=None
                service.snapshot.return_value=snapshot()
                await pilot.press('r')
                await self.settle(pilot,app)
                self.assertFalse(loading.display)
                self.assertFalse(app.query_one('#open',Button).disabled)
        finally:
            ready.set()

    async def test_child_loading_has_animation_without_confirmation_buttons(self):
        ready=threading.Event()
        def load(progress):
            progress('检查工作树',2,5)
            ready.wait(5)
            return {'fixture':'result'}
        app=LoadingApp('核对项目产物',load)
        try:
            async with app.run_test(size=(44,16)) as pilot:
                await pilot.pause(.15)
                self.assertFalse(app.query(Button))
                loading=app.query_one(ScanLoading)
                self.assertEqual((loading.done,loading.total),(2,5))
                frame=loading.frame
                await pilot.pause(.25)
                self.assertGreater(loading.frame,frame)
                await pilot.resize_terminal(46,40)
                await pilot.pause(.1)
                self.assertTrue(loading.has_class('compact'))
                await pilot.resize_terminal(110,34)
                await pilot.pause(.1)
                self.assertFalse(loading.has_class('compact'))
                self.assertEqual(loading.query_one('.scan-art').size.height,CAT_HEIGHT)
                ready.set()
                await self.settle(pilot,app)
            self.assertEqual(app.return_value,{'fixture':'result'})
        finally:
            ready.set()

    def test_generated_frames_are_bounded_and_looping(self):
        self.assertGreater(len(set(CAT_FRAMES)),20)
        self.assertGreater(len(set(SCAN_FRAMES)),5)
        for frame, colors in CAT_FRAMES:
            self.assertEqual(len(frame.splitlines()),CAT_HEIGHT)
            self.assertTrue(all(len(line)==CAT_WIDTH for line in frame.splitlines()))
            self.assertEqual(len(colors),len(frame))
            self.assertTrue(all(len(line)==CAT_WIDTH for line in colors.splitlines()))
            self.assertTrue(all(c == '\n' or 0 <= int(c) < len(CAT_PALETTE) for c in colors))
            self.assertNotIn('\x1b',frame)
