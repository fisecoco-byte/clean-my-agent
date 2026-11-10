import os
import threading
import unittest
from unittest.mock import Mock, patch

from clean_my_agent.dependency_report import empty_report
from clean_my_agent.storage_tui import StorageApp
from clean_my_agent.loading_tui import ScanLoading
from clean_my_agent.tui_common import DetailScreen
from clean_my_agent.worktree_tui import WorktreeScreen
from textual.widgets import Button, DataTable, ProgressBar, Static
from test_worktree_ui import report


def preview():
    data = report()
    data['mode'] = 'references_only'
    for row in data['entries']:
        row['status'] = 'unmeasured'
        for key in ('directory_bytes', 'shared_bytes', 'reclaimable_bytes', 'reclaimable_min_bytes', 'reclaimable_max_bytes'):
            row[key] = None
    for row in data['projects']:
        row['directory_bytes'] = None
    return data


class ProgressiveScanUI(unittest.IsolatedAsyncioTestCase):
    async def until(self, pilot, predicate):
        for _ in range(100):
            if predicate():
                return
            await pilot.pause(.03)
        self.fail('background scan did not settle')

    async def test_slow_discovery_shows_animation_until_real_rows_arrive(self):
        discovered, measured = threading.Event(), threading.Event()
        def load(progress, publish):
            progress('查找依赖目录')
            discovered.wait(8)
            publish(preview())
            progress('测量依赖目录', 0, 3)
            measured.wait(8)
            return report()
        app = StorageApp(empty_report(False), loader=load, actions=Mock(), start_worktrees=True)
        try:
            async with app.run_test(size=(110,34)) as pilot:
                await self.until(pilot, lambda: app.discovering)
                loading = app.query_one(ScanLoading)
                self.assertTrue(loading.display)
                self.assertFalse(app.query_one('#layout').display)
                self.assertFalse(app.query_one('#tabs').display)
                self.assertFalse(app.query_one('#worktrees').display)
                self.assertFalse(app.query_one('#empty').display)
                self.assertIsNone(loading.total)
                self.assertFalse(loading.query_one(ProgressBar).display)
                frame = loading.frame
                await pilot.pause(.25)
                self.assertGreater(loading.frame, frame)
                await pilot.press('w')
                self.assertIs(app.screen, app.screen_stack[0])
                await pilot.resize_terminal(44,16)
                await pilot.pause(.1)
                self.assertTrue(loading.has_class('compact'))
                self.assertLessEqual(loading.region.bottom, app.size.height - 1)
                discovered.set()
                await self.until(pilot, lambda: isinstance(app.screen, WorktreeScreen) and app.screen.is_mounted)
                self.assertFalse(loading.display)
                self.assertIsNone(loading.started)
                self.assertEqual(len(app.screen.rows), 2)
                measured.set()
                await self.until(pilot, lambda: app.scan_complete)
                self.assertTrue(all(r['directory_bytes'] is not None for r in app.screen.rows))
        finally:
            discovered.set()
            measured.set()

    async def test_empty_preview_keeps_loading_until_empty_scan_finishes(self):
        gate = threading.Event()
        def load(progress, publish):
            publish(empty_report(False))
            progress('核对工作目录')
            gate.wait(5)
            return empty_report()
        app = StorageApp(empty_report(False), loader=load)
        try:
            async with app.run_test(size=(100,30)) as pilot:
                await self.until(pilot, lambda: app.scan_message.endswith('核对工作目录'))
                self.assertTrue(app.query_one(ScanLoading).display)
                self.assertFalse(app.query_one('#empty').display)
                gate.set()
                await self.until(pilot, lambda: app.scan_complete)
                self.assertFalse(app.query_one(ScanLoading).display)
                self.assertTrue(app.query_one('#layout').display)
                self.assertTrue(app.query_one('#empty').display)
                self.assertIn('暂无记录', str(app.query_one('#empty', Static).render()))
        finally:
            gate.set()

    async def test_initial_failure_stops_loading_shows_error_and_allows_retry(self):
        def load(progress, publish):
            progress('查找依赖目录')
            raise OSError('fixture discovery failed')
        app = StorageApp(empty_report(False), loader=load)
        async with app.run_test(size=(100,30)) as pilot:
            await self.until(pilot, lambda: bool(app.report['errors']))
            self.assertFalse(app.query_one(ScanLoading).display)
            self.assertIsNone(app.query_one(ScanLoading).started)
            self.assertFalse(app.scan_complete)
            self.assertIn('fixture discovery failed', app.scan_message)
            self.assertNotIn('暂无记录', str(app.query_one('#empty', Static).render()))
            self.assertFalse(app.query_one('#reload', Button).disabled)
            app.loader = lambda progress, publish: report()
            await pilot.press('r')
            await self.until(pilot, lambda: app.scan_complete)
            self.assertEqual(len(app.report['projects']), 2)
            self.assertFalse(app.report['errors'])

    async def test_quit_during_initial_discovery_cancels_loading(self):
        stopped = threading.Event()
        def load(progress, publish):
            try:
                while not stopped.wait(.02):
                    progress('查找依赖目录')
            finally:
                stopped.set()
        app = StorageApp(empty_report(False), loader=load)
        async with app.run_test(size=(80,24)) as pilot:
            await self.until(pilot, lambda: app.query_one(ScanLoading).started is not None)
            self.assertFalse(app.query_one('#layout').display)
            await pilot.press('q')
        self.assertTrue(stopped.wait(2))
        self.assertFalse(app.scan_complete)

    async def test_list_is_browsable_before_measurement_and_selection_survives_update(self):
        ready = threading.Event()
        def load(progress, publish):
            publish(preview())
            progress('测量依赖目录', 0, 3)
            if not ready.wait(8):
                raise TimeoutError('fixture timed out')
            return report()
        actions = Mock()
        app = StorageApp(empty_report(False), loader=load, actions=actions, start_worktrees=True)
        try:
            async with app.run_test(size=(100,30)) as pilot:
                await self.until(pilot, lambda: isinstance(app.screen, WorktreeScreen) and app.screen.is_mounted)
                screen = app.screen
                self.assertTrue(app.scan_pending)
                self.assertFalse(app.operation_busy)
                self.assertEqual(screen.query_one(DataTable).row_count, 2)
                self.assertTrue(screen.query_one('#wt-delete', Button).disabled)
                await pilot.press('d')
                self.assertIsInstance(app.screen, DetailScreen)
                self.assertIn('后台扫描尚未完成', app.screen.body.plain)
                actions.worktree_details.assert_not_called()
                actions.delete_worktree.assert_not_called()
                await pilot.press('escape')
                await pilot.press('down', 'enter')
                self.assertIsInstance(app.screen, DetailScreen)
                actions.worktree_details.assert_not_called()
                await pilot.press('escape', 's')
                selected = screen.selected()['path']
                ready.set()
                await self.until(pilot, lambda: app.scan_complete)
                self.assertEqual(screen.selected()['path'], selected)
                self.assertFalse(screen.query_one('#wt-delete', Button).disabled)
                self.assertIn('measured_at', app.report)
                await pilot.press('r')
                await self.until(pilot, lambda: app.scan_complete)
                self.assertIs(app.screen, screen)
                self.assertEqual(screen.selected()['path'], selected)
                await pilot.press('p')
                self.assertTrue(all(r['path'].startswith(selected+'/') for r in app.rows))
                self.assertTrue(app.can_stage(0))
                actions.stage.assert_not_called()
        finally:
            ready.set()

    async def test_failed_refresh_disables_cached_candidates_and_retry_replaces_errors(self):
        gate = threading.Event()
        def fail(progress, publish):
            gate.wait(5)
            raise OSError('fixture unreadable')
        app = StorageApp(report(), loader=fail, load_on_mount=False, actions=Mock())
        try:
            async with app.run_test(size=(100,30)) as pilot:
                self.assertTrue(app.can_stage(0))
                await pilot.press('r')
                self.assertFalse(app.can_stage(0))
                gate.set()
                await self.until(pilot, lambda: not app.scan_pending)
                self.assertFalse(app.scan_complete)
                await pilot.press('space', 't')
                self.assertFalse(app.checked)
                self.assertIs(app.screen, app.screen_stack[0])
                self.assertIn('fixture unreadable', app.scan_message)
                app.loader = lambda progress, publish: report()
                await pilot.press('r')
                await self.until(pilot, lambda: app.scan_complete)
                self.assertFalse(app.report['errors'])
                self.assertTrue(app.can_stage(0))
        finally:
            gate.set()

    async def test_quit_cancels_background_callbacks_without_waiting_for_full_scan(self):
        stopped = threading.Event()
        def load(progress, publish):
            try:
                publish(preview())
                while not stopped.wait(.02):
                    progress('测量依赖目录', 0, 3)
            finally:
                stopped.set()
        app = StorageApp(empty_report(False), loader=load)
        async with app.run_test(size=(80,24)) as pilot:
            await self.until(pilot, lambda: bool(app.report['entries']))
            await pilot.press('q')
        self.assertTrue(stopped.wait(2))
        self.assertFalse(app.scan_complete)

    async def test_cached_list_does_not_start_scan_until_refresh(self):
        loader = Mock(return_value=report())
        app = StorageApp(report(), loader=loader, load_on_mount=False)
        async with app.run_test(size=(44,16)) as pilot:
            loader.assert_not_called()
            await pilot.press('r')
            await self.until(pilot, lambda: not app.scan_pending)
            loader.assert_called_once()

    async def test_selected_artifact_survives_classification_and_small_window(self):
        gate = threading.Event()
        final = report()
        final['entries'][-1]['status'] = 'retained'
        selected = final['entries'][-1]['path']
        def load(progress, publish):
            publish(preview())
            gate.wait(5)
            return final
        app = StorageApp(empty_report(False), loader=load, actions=Mock())
        try:
            async with app.run_test(size=(44,16)) as pilot:
                await self.until(pilot, lambda: len(app.rows) == 3)
                table = app.query_one('#grid', DataTable)
                table.move_cursor(row=next(i for i,r in enumerate(app.rows) if r['path'] == selected))
                await pilot.pause()
                self.assertLessEqual(app.query_one('#storage-actions').region.bottom, app.size.height)
                self.assertGreaterEqual(table.size.height, 2)
                gate.set()
                await self.until(pilot, lambda: app.scan_complete)
                self.assertEqual(app.category, 'retained')
                self.assertEqual(app.rows[table.cursor_row]['path'], selected)
        finally:
            gate.set()

    async def test_failed_write_invalidates_cache_but_cancel_does_not(self):
        actions = Mock()
        actions.stage.side_effect = OSError('fixture outcome unknown')
        app = StorageApp(report(), actions=actions)
        async with app.run_test(size=(100,30)) as pilot:
            await pilot.press('t', 'escape')
            self.assertFalse(app.cache_invalidated)
            await pilot.press('t', 'enter')
            await self.until(pilot, lambda: app.screen.finished)
            self.assertTrue(app.cache_invalidated)
            self.assertFalse(app.operation_results)


class ReportCache(unittest.TestCase):
    def test_dashboard_reuses_complete_reports_and_invalidates_after_changes(self):
        from clean_my_agent.dashboard import browse
        from test_dashboard import snapshot
        service = Mock()
        service.dependency_reports = {}
        homes = [Mock(snapshot=snapshot()) for _ in range(4)]
        for home in homes[:3]:
            home.run.return_value = ('deps','codex')
        homes[-1].run.return_value = None
        children = [Mock(report=report(), scan_complete=True, operation_results=[]) for _ in range(3)]
        children[1].cache_invalidated = True
        with patch.dict(os.environ, {'TERM':'xterm'}), patch('sys.stdin.isatty', return_value=True), \
             patch('clean_my_agent.dashboard.DashboardService', return_value=service), \
             patch('clean_my_agent.dashboard_tui.DashboardApp', side_effect=homes), \
             patch('clean_my_agent.storage_tui.StorageApp', side_effect=children) as storage:
            self.assertTrue(browse(Mock(active=True)))
        self.assertEqual([c.kwargs['load_on_mount'] for c in storage.call_args_list], [True, False, True])
        self.assertEqual(storage.call_args_list[1].args[0], children[0].report)
        self.assertIsNot(storage.call_args_list[1].args[0], children[0].report)
