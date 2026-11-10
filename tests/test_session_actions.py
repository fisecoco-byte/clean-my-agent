import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from clean_my_agent import actions, inventory, policy
from clean_my_agent.scan import layout

RULES = {'codex': {'archive_days':10, 'delete_days':20}, 'claude': {'archive_days':10, 'delete_days':20}}


class NativeActions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        env = patch.dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.home/'codex'),
            CLAUDE_CONFIG_DIR=str(self.home/'claude'), XDG_CONFIG_HOME=str(self.home/'config'),
            XDG_STATE_HOME=str(self.home/'state'))
        env.start(); self.addCleanup(env.stop)
        self.roots = layout()[0]
        self.root = self.roots['codex']
        self.controller = actions.SessionActions(self.roots)
        self.calls = []
        self.progress = lambda *args, **kwargs: None

    def session(self, n, archived=False):
        sid = f'01234567-89ab-cdef-9012-{n:012x}'
        path = self.root / ('archived_sessions' if archived else 'sessions') / f'rollout-test-{sid}.jsonl'
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text('session fixture; should not be read by actions')
        os.utime(path, (time.time()-40*86400,)*2)
        return sid, path

    def rows(self):
        return inventory.inspect(self.roots, policy.load(), agent='codex')[1]['codex']

    def native(self, argv, **kwargs):
        self.assertEqual(kwargs['env']['CODEX_HOME'], str(self.root))
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertNotIn('shell', kwargs)
        if '--help' in argv:
            return subprocess.CompletedProcess(argv, 0, 'Usage: codex archive / delete --force', '')
        self.calls.append(argv)
        sid = argv[2]
        matches = list(self.root.glob(f'**/rollout-test-{sid}.jsonl'))
        self.assertEqual(len(matches), 1)
        if argv[1] == 'archive':
            dest = self.root/'archived_sessions'/matches[0].name
            dest.parent.mkdir(exist_ok=True)
            matches[0].rename(dest)
        else:
            self.assertEqual(argv[-1], '--force')
            matches[0].unlink()
        return subprocess.CompletedProcess(argv, 0, 'Done', '')

    def apply(self, mode, rows):
        with patch.object(actions.shutil, 'which', return_value='/fake/codex'), patch.object(
                actions.subprocess, 'run', side_effect=self.native):
            return self.controller.apply(mode, rows, RULES, self.progress)

    def test_native_archive_and_delete_use_exact_ids(self):
        sid, _ = self.session(1)
        result = self.apply('archive', self.rows())
        self.assertEqual(result['results'][0]['status'], 'succeeded')
        self.assertEqual(self.calls, [['/fake/codex', 'archive', sid]])
        result = self.apply('delete', self.rows())
        self.assertEqual(result['results'][0]['status'], 'succeeded')
        self.assertEqual(self.calls[-1], ['/fake/codex', 'delete', sid, '--force'])

    def test_recent_activity_and_protection_prevent_native_call(self):
        _, path = self.session(2)
        original = self.rows()
        os.utime(path, None)
        self.assertEqual(self.apply('archive', original)['results'][0]['status'], 'skipped')
        self.assertEqual(self.calls, [])
        sid, path = self.session(3)
        original = next(r for r in self.rows() if r['session_id']==sid)
        cfg = policy.config_path(); cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({'protect':['codex:'+str(path.relative_to(self.root))]}))
        self.assertEqual(self.apply('archive', [original])['results'][0]['status'], 'skipped')
        self.assertEqual(self.calls, [])

    def test_pinned_and_unverified_index_are_not_executable(self):
        sid, _ = self.session(4)
        original = self.rows()
        with sqlite3.connect(self.root/'state_5.sqlite') as db:
            db.execute('create table threads (id,archived,is_pinned,updated_at)')
            db.execute('insert into threads values(?,?,?,?)',(sid,0,1,0))
        self.assertEqual(self.apply('archive', original)['results'][0]['status'], 'skipped')
        self.assertEqual(self.calls, [])

    def test_partial_failure_preserves_success_and_stops_later_commands(self):
        for n in range(3): self.session(10+n)
        rows = self.rows()
        def native(argv, **kwargs):
            if '--help' not in argv and argv[2]==rows[1]['session_id']:
                self.calls.append(argv)
                return subprocess.CompletedProcess(argv, 1, '', 'native failure')
            return self.native(argv, **kwargs)
        with patch.object(actions.shutil, 'which', return_value='/fake/codex'), patch.object(
                actions.subprocess, 'run', side_effect=native):
            result=self.controller.apply('archive', rows, RULES, self.progress)
        self.assertEqual([r['status'] for r in result['results']], ['succeeded','failed','skipped'])
        self.assertEqual(len(self.calls),2)

    def test_invalid_ids_and_missing_native_support_never_execute(self):
        self.session(6)
        rows=self.rows()
        rows[0]['session_id']='--help; rm -rf /'
        with self.assertRaises(ValueError):
            self.apply('delete',rows)
        self.assertEqual(self.calls,[])
        with patch.object(actions.shutil,'which',return_value=None):
            with self.assertRaisesRegex(ValueError,'未找到'):
                self.controller.apply('archive',self.rows(),RULES,self.progress)

    def test_same_day_activity_and_current_chat_are_revalidated(self):
        sid,path=self.session(7)
        original=self.rows()
        stamp=path.stat().st_mtime_ns + 10**9
        os.utime(path,ns=(stamp,stamp))
        self.assertEqual(self.apply('archive',original)['results'][0]['status'],'skipped')
        original=self.rows()
        with patch.dict(os.environ,CODEX_THREAD_ID=sid):
            self.assertEqual(self.apply('archive',original)['results'][0]['status'],'skipped')
        self.assertFalse(self.calls)

    def test_lost_native_result_is_uncertain_and_not_retried(self):
        for n in (8,9): self.session(n)
        def native(argv,**kwargs):
            if '--help' in argv: return self.native(argv,**kwargs)
            self.calls.append(argv)
            raise subprocess.TimeoutExpired(argv,90)
        with patch.object(actions.shutil,'which',return_value='/fake/codex'), patch.object(
                actions.subprocess,'run',side_effect=native):
            result=self.controller.apply('archive',self.rows(),RULES,self.progress)
        self.assertEqual([r['status'] for r in result['results']],['uncertain','skipped'])
        self.assertEqual(len(self.calls),1)
