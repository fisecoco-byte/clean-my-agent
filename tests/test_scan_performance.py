import os
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError
from unittest.mock import patch

from clean_my_agent import activity, dependency_report, deps, policy, storage
from clean_my_agent.progress import parallel
from clean_my_agent.scan import fingerprint
from test_dependency_usage import without_observation_time


class Idle:
    def check(self, *a, **kw):
        return dict(state='idle', reason='fixture')


class ScannerReuse(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.root = self.home / 'codex'
        self.repo = self.root / 'worktrees/demo'
        self.path = self.repo / 'target'
        self.path.mkdir(parents=True)
        (self.repo / 'Cargo.toml').write_text('[package]\nname="fixture"\nversion="0.1.0"\n')
        (self.repo / '.gitignore').write_text('target/\n')
        (self.path / 'compiled').write_bytes(b'x' * 5000)
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
        for path in (self.path / 'compiled', self.path):
            os.utime(path, (10, 10))
        env = patch.dict(os.environ, {'HOME':str(self.home), 'CODEX_HOME':str(self.root),
            'CLAUDE_CONFIG_DIR':str(self.home/'claude'), 'XDG_CONFIG_HOME':str(self.home/'config')})
        env.start()
        self.addCleanup(env.stop)
        idle = patch.object(dependency_report, 'ProcessSnapshot', Idle)
        idle.start()
        self.addCleanup(idle.stop)

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)

    def test_preview_is_published_before_measurement_and_indices_are_read_once(self):
        previews = []
        original = storage.measure
        def measure(*a, **kw):
            self.assertEqual(len(previews), 1)
            self.assertEqual(previews[0]['mode'], 'references_only')
            self.assertEqual(previews[0]['entries'][0]['status'], 'unmeasured')
            self.assertIsNone(previews[0]['projects'][0]['directory_bytes'])
            return original(*a, **kw)
        with patch.object(dependency_report, 'measure', side_effect=measure), \
             patch.object(activity, 'session_records', return_value=([], [])) as records, \
             patch.object(deps, 'usage', side_effect=AssertionError('rescanned measured artifacts')):
            report = dependency_report.inspect_agents({'codex':self.root, 'claude':self.home/'claude'}, policy.load(),
                agents=('codex','claude'), on_preview=previews.append)
        records.assert_called_once()
        self.assertEqual(report['entries'][0]['status'], 'candidate')
        self.assertFalse(report['errors'])

    def test_workspace_total_matches_du_with_shared_hardlinks_sparse_files_and_links(self):
        other = self.repo / 'build\nwith spaces'
        other.mkdir()
        os.link(self.path / 'compiled', other / 'shared')
        os.link(self.path / 'compiled', self.repo / 'source-link')
        os.link(self.path / 'compiled', self.path / 'second-name')
        (self.path / 'outside').symlink_to(self.home)
        with (other / 'sparse').open('wb') as stream:
            stream.seek(2**22)
            stream.write(b'x')
        measured = {}
        for path in (self.path, other):
            summary = storage.DirectoryUsage()
            summary.observe(path.lstat())
            storage.measure(path, on_entry=lambda _, s: summary.observe(s))
            measured[path] = (fingerprint(path.lstat()), summary)
        visited = []
        walk = os.fwalk
        def observe_walk(*a, **kw):
            for item in walk(*a, **kw):
                visited.append(item[0])
                yield item
        with patch.object(storage.os, 'fwalk', side_effect=observe_walk):
            size = storage.workspace_usage(self.repo, measured)
        self.assertEqual(size, deps.usage(self.repo))
        self.assertFalse(any('target' in Path(p).parts or other.name in Path(p).parts for p in visited))
        # A changed directory cannot reuse the old subtree summary.
        (other / 'new').write_bytes(b'y' * 9000)
        self.assertEqual(storage.workspace_usage(self.repo, measured), deps.usage(self.repo))

    def test_nested_file_changes_are_reread_even_if_artifact_root_mtime_is_unchanged(self):
        nested = self.path / 'nested'
        nested.mkdir()
        file = nested / 'file'
        file.write_bytes(b'a')
        before = dependency_report.inspect(self.root, policy.load())
        stamp = fingerprint(self.path.lstat())
        file.write_bytes(b'b' * 50000)
        self.assertEqual(stamp, fingerprint(self.path.lstat()))
        after = dependency_report.inspect(self.root, policy.load())
        self.assertGreater(after['projects'][0]['directory_bytes'], before['projects'][0]['directory_bytes'])
        self.assertEqual(after['projects'][0]['directory_bytes'], deps.usage(self.repo))

    def test_serial_parallel_reports_match_and_late_reference_blocks_candidate(self):
        expected = dependency_report.inspect(self.root, policy.load(), workers=1)
        actual = dependency_report.inspect(self.root, policy.load(), workers=2)
        self.assertEqual(without_observation_time(expected), without_observation_time(actual))
        original = storage.measure
        alias = self.root / 'projects/other/target'
        alias.parent.mkdir(parents=True)
        def measure(*a, **kw):
            result = original(*a, **kw)
            alias.symlink_to(self.path)
            return result
        with patch.object(dependency_report, 'measure', side_effect=measure):
            report = dependency_report.inspect(self.root, policy.load(), workers=2)
        self.assertEqual(report['entries'][0]['status'], 'retained')
        self.assertIn(str(alias), report['entries'][0]['referenced_by'])

    def test_find_batches_keep_nul_paths_and_completed_checkout_progress(self):
        extra = self.home / 'other\ncheckout'
        self.git('worktree', 'add', '-q', '--detach', str(extra))
        (extra / 'target').symlink_to(self.path)
        events, find_calls = [], []
        run = subprocess.run
        def record(command, *a, **kw):
            if command[0] == 'find':
                find_calls.append(command)
            return run(command, *a, **kw)
        with patch.object(deps.subprocess, 'run', side_effect=record):
            refs = deps.References(self.root, progress=lambda *a, **kw: events.append(a))
        self.assertFalse(refs.errors)
        self.assertIn(extra / 'target', refs.incoming(self.path))
        self.assertEqual(len(find_calls), 1)
        self.assertIn(str(extra), find_calls[0])
        self.assertEqual([e[1:3] for e in events if e[0] == '复核工作树引用'], [(0,2), (2,2)])

    def test_cli_opens_live_view_before_scanning_and_json_waits_for_complete_report(self):
        from clean_my_agent.cli import main
        with patch('clean_my_agent.storage_view.browse', return_value=True) as browse, \
             patch.object(dependency_report, 'inspect_agents', side_effect=AssertionError('eager scan')):
            self.assertEqual(main(['deps','--agent','codex']), 0)
        self.assertEqual(browse.call_args.args[1]['mode'], 'references_only')
        self.assertTrue(callable(browse.call_args.kwargs['loader']))
        with patch('clean_my_agent.storage_view.browse', side_effect=AssertionError('UI in JSON')), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['deps','--agent','codex','--json']), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['mode'], 'measured')
        self.assertEqual(result['entries'][0]['status'], 'candidate')
        self.assertIsNotNone(result['projects'][0]['directory_bytes'])


class BoundedConcurrency(unittest.TestCase):
    def test_large_flat_directory_reports_progress_before_finishing_and_can_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(4200):
                (root / str(index)).touch()
            seen = []
            def cancel(count):
                seen.append(count)
                raise CancelledError()
            with self.assertRaises(CancelledError):
                storage.measure(root, on_entries=cancel)
            self.assertEqual(seen, [2048])

    def test_workers_are_bounded_and_progress_stays_on_calling_thread(self):
        lock, ready = threading.Lock(), threading.Event()
        current = peak = started = 0
        owner = threading.get_ident()
        events = []
        def work(item, count):
            nonlocal current, peak, started
            with lock:
                current += 1
                started += 1
                peak = max(peak, current)
            count(2048)
            self.assertTrue(ready.wait(5))
            with lock:
                current -= 1
            return item.name
        def progress(label, done, total, detail=''):
            self.assertEqual(threading.get_ident(), owner)
            events.append((done, total))
            if started >= 2:
                ready.set()
        result = dict(parallel([Path(str(i)) for i in range(6)], work, progress, 'scan', workers=2))
        self.assertEqual(len(result), 6)
        self.assertEqual(peak, 2)
        self.assertEqual(events[-1], (6, 6))
        self.assertEqual([d for d, _ in events], sorted(d for d, _ in events))

    def test_default_is_serial_and_invalid_override_is_rejected(self):
        owner = threading.get_ident()
        def work(item, count):
            self.assertEqual(threading.get_ident(), owner)
        with patch.dict(os.environ, {'CLEAN_MY_AGENT_SCAN_WORKERS':'1'}):
            list(parallel([Path('one')], work, None, 'scan'))
        for invalid in ('0', '5', 'unknown'):
            with patch.dict(os.environ, {'CLEAN_MY_AGENT_SCAN_WORKERS':invalid}), self.assertRaises(ValueError):
                list(parallel([Path('one')], work, None, 'scan'))
