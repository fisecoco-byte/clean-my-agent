import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from clean_my_agent import policy, vault
from clean_my_agent.scan import layout, scan, fingerprint, disk_bytes

CLI = Path(__file__).resolve().parents[1] / 'clean-my-agent'


class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.env = {**os.environ, 'HOME': str(self.home), 'CODEX_HOME': str(self.home / '.codex'),
                    'CLAUDE_CONFIG_DIR': str(self.home / '.claude'), 'XDG_STATE_HOME': str(self.home / 'state'),
                    'XDG_CACHE_HOME': str(self.home / 'cache'), 'XDG_CONFIG_HOME': str(self.home / 'config')}
        self.st = self.home / 'state/clean-my-agent'

    def cli(self, *args, ok=True):
        p = subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode == 0, ok, (p.stdout, p.stderr))
        return p.stdout

    def file(self, rel, age=61):
        path = self.home / rel
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text('fixture-' + path.name)
        t = time.time() - age * 86400
        os.utime(path, (t, t))
        return path

    def config(self, data):
        path = self.home / 'config/clean-my-agent/policy.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def age_tree(self, tree, days=40):
        stamp = time.time() - days * 86400
        for p in [*tree.rglob('*'), tree]:
            os.utime(p, (stamp, stamp), follow_symlinks=False)

    def test_selection_and_no_side_effects(self):
        old = self.file('.claude/debug/old.txt')
        self.file('.claude/debug/new.txt', 0)
        self.file('.codex/auth.json')
        self.file('.codex/sessions/old.jsonl')
        self.file('.codex/archived_sessions/unexpected.txt')
        archive = self.file('.codex/archived_sessions/rollout-old.jsonl.zst', 100)
        output = self.file('.claude/projects/example/session/tool-results/old.txt', 100)
        d = json.loads(self.cli('clean', '--details', '--json'))
        self.assertEqual([i['path'] for i in d['files']], [str(old)])
        self.assertFalse(self.st.exists())
        self.assertTrue(old.exists())
        snapshots = [self.file('.codex/shell_snapshots/old.sh'), self.file('.claude/shell-snapshots/old.sh')]
        tree = self.file('.codex/worktrees/demo/main.py')
        self.cli('clean', '--include-history', '--apply', ok=False)
        self.assertFalse(self.st.exists())
        self.config({'retention_days': {'snapshots': 1, 'archives': 1, 'tool-results': 1}})
        permitted = self.file('.codex/cache/codex_apps_tools/old.json')
        d = json.loads(self.cli('clean', '--profile', 'deep', '--days', '1', '--apply', '--details', '--json'))
        self.assertEqual({i['path'] for i in d['files']}, {str(old), str(permitted)})
        self.assertEqual(set(d['policy']['retention_days']), {'cache', 'logs', 'dependencies'})
        self.assertTrue(all(f.exists() for f in [archive, output, tree, *snapshots]))
        self.assertFalse(permitted.exists())
        self.cli('clean', '--days', '0', '--apply', ok=False)

    def test_restore_conflict_and_purge(self):
        old = self.file('.codex/log/tui.log')
        original = old.read_text()
        d = json.loads(self.cli('clean', '--apply', '--json'))
        key = d['batch']
        self.assertFalse(old.exists())
        self.assertIn(key, self.cli('trash'))
        self.cli('purge', key)
        old.write_text('replacement')
        self.cli('restore', key, ok=False)
        self.assertEqual(old.read_text(), 'replacement')
        old.unlink()
        self.cli('restore', key)
        self.assertEqual(old.read_text(), original)
        key = json.loads(self.cli('clean', '--apply', '--json'))['batch']
        self.cli('purge', key, '--apply')
        self.assertFalse(old.exists())
        self.assertFalse((self.st / key).exists())

    def test_log_written_after_quarantine_is_recoverable(self):
        old = self.file('.codex/log/agent.log')
        with old.open('a') as writer:
            key = json.loads(self.cli('clean', '--apply', '--json'))['batch']
            writer.write('still logging')
        self.assertIn(str(old), self.cli('trash', key))
        self.cli('purge', key, '--apply', ok=False)
        self.cli('restore', key)
        self.assertTrue(old.read_text().endswith('still logging'))

    def test_links_and_changed_files(self):
        old = self.file('.codex/log/test.log')
        other = self.file('outside/test.log')
        (old.parent / 'link.log').symlink_to(other)
        (old.parent.parent / 'shell_snapshots').symlink_to(other.parent)
        os.link(other, old.parent / 'hard.log')
        with patch.dict(os.environ, self.env):
            roots, state = layout()
            items = scan(roots, {'codex'}, policy.load()).items
            self.assertEqual(len(items), 1)
            old.write_text('changed since scan')
            key, count, _, errors = vault.save(items, roots, state)
            self.assertIsNone(key)
            self.assertEqual(count, 0)
            self.assertTrue(errors)
        self.assertEqual(other.read_text(), 'fixture-test.log')
        self.cli('purge', '../outside', '--apply', ok=False)

    def test_redirected_restore_parent(self):
        old = self.file('.codex/log/test.log')
        key = json.loads(self.cli('clean', '--apply', '--json'))['batch']
        old.parent.rmdir()
        outside = self.home / 'outside'
        outside.mkdir()
        old.parent.symlink_to(outside)
        self.cli('restore', key, ok=False)
        self.assertFalse((outside / old.name).exists())
        self.assertIn(key, self.cli('trash'))

    def test_full_report_and_absent_homes(self):
        d = json.loads(self.cli('scan', '--json'))
        self.assertEqual(d['bytes'], 0)
        self.assertFalse(self.st.exists())
        f = self.file('.codex/worktrees/a/code.py')
        d = json.loads(self.cli('scan', '--full', '--json'))
        self.assertGreaterEqual(d['bytes'], f.stat().st_blocks * 512)
        self.assertEqual(d['eligible_files'], 0)
        self.assertTrue(any(r['category'] == 'worktrees' for r in d['groups']))

    def repo(self):
        prefix = '.codex/worktrees/demo/checkout/'
        package = self.file(prefix + 'package.json')
        package.write_text('{}')
        os.utime(package, (1, 1))
        repo = package.parent
        def git(*args):
            subprocess.run(['git', '-C', repo, *args], env=self.env, check=True, capture_output=True)
        git('init', '-q')
        (repo / '.gitignore').write_text('/node_modules/\n')
        self.file(prefix + 'package-lock.json')
        git('add', '--', '.gitignore', 'package-lock.json', 'package.json')
        f = self.file(prefix + 'node_modules/pkg/index.js')
        marker = self.file(prefix + 'node_modules/.package-lock.json')
        marker.write_text('{"lockfileVersion":3,"packages":{}}')
        return f, git

    def test_single_dependency_restore_and_purge(self):
        f, _ = self.repo()
        path = f.parents[1]
        outside = self.file('keep')
        (path / 'external').symlink_to(outside.parent)
        self.age_tree(path)
        self.assertIn(str(path), self.cli('deps'))
        self.cli('deps', '--apply', ok=False)
        self.cli('deps', str(path))
        self.assertTrue(f.exists())
        key = re.search(r'restore ([a-f0-9]{16})', self.cli('deps', str(path), '--apply'))[1]
        self.assertFalse(path.exists())
        path.mkdir()  # Even an empty replacement directory must be kept.
        self.cli('restore', key, ok=False)
        path.rmdir()
        self.cli('restore', key)
        self.assertTrue(f.exists())
        key = re.search(r'restore ([a-f0-9]{16})', self.cli('deps', str(path), '--apply'))[1]
        self.cli('purge', key, '--apply')
        self.assertFalse(path.exists())
        self.assertTrue(outside.is_file())
        self.assertTrue((path.parent / 'package.json').is_file())

    def test_ineligible_dependency_tree(self):
        f, git = self.repo()
        path = f.parents[1]
        git('add', '-f', 'node_modules/pkg/index.js')
        self.cli('deps', str(path), '--apply', ok=False)
        git('rm', '--cached', '-f', 'node_modules/pkg/index.js')
        (path.parent / 'package-lock.json').unlink()
        self.cli('deps', str(path), '--apply', ok=False)
        self.assertTrue(f.exists())
        self.assertFalse(self.st.exists())

    def test_profiles_precedence_and_history_gate(self):
        self.file('.claude/cache/changelog.md', 12)
        log = self.file('.codex/log/older.log', 40)
        archive = self.file('.codex/archived_sessions/rollout-example.jsonl', 80)
        self.config({'profile': 'conservative'})
        def paths(*args):
            data = json.loads(self.cli('clean', '--json', '--details', *args))
            return {i['path'] for i in data['files']}
        self.assertFalse(paths())
        self.assertEqual(paths('--profile', 'standard'), {str(log)})
        self.assertEqual(len(paths('--profile', 'deep')), 2)
        self.assertNotIn(str(archive), paths('--profile', 'deep'))
        self.config({'profile': 'deep', 'retention_days': {'logs': 55}})
        self.assertNotIn(str(log), paths())
        self.assertIn(str(log), paths('--days', '10'))
        self.assertNotIn(str(archive), paths('--days', '1'))
        self.assertFalse(self.st.exists())

    def test_protected_paths_through_purge(self):
        protected = self.file('.codex/log/private.log')
        similar = self.file('.codex/log/private.log.1')
        self.config({'protect': ['codex:log/private.log']})
        preview = json.loads(self.cli('clean', '--dry-run', '--details', '--json'))
        self.assertEqual([str(similar)], [i['path'] for i in preview['files']])
        self.assertEqual(preview['kept']['protected']['files'], 1)
        key = json.loads(self.cli('clean', '--apply', '--json'))['batch']
        self.assertTrue(protected.exists())
        self.config({'protect': ['codex:log']})
        self.cli('purge', key, '--apply', ok=False)
        self.assertIn(str(similar), self.cli('trash', key))
        self.cli('restore', key)
        self.assertTrue(similar.exists())

    def test_invalid_policy_stops_clean_and_purge_but_not_restore(self):
        f = self.file('.codex/log/example.log')
        key = json.loads(self.cli('clean', '--apply', '--json'))['batch']
        other = self.file('.codex/log/other.log')
        config = self.config({'protect': ['codex:../log']})
        self.cli('clean', '--apply', ok=False)
        self.assertTrue(other.exists())
        config.write_text('{')
        self.cli('purge', key, '--apply', ok=False)
        config.unlink()
        config.symlink_to(self.home / 'missing.json')
        self.cli('clean', '--apply', ok=False)
        self.cli('restore', key)
        self.assertTrue(f.exists())

    def test_dependency_activity_and_nested_protection(self):
        f, _ = self.repo()
        path = f.parents[1]
        self.age_tree(path, 20)
        self.assertIn('符合策略 1', self.cli('deps'))
        self.assertIn('最近 30 天', self.cli('deps', '--profile', 'conservative'))
        os.utime(f, None)  # Root remains old; a recently written nested file vetoes the tree.
        self.assertIn('最近 7 天', self.cli('deps', str(path), '--dry-run'))
        self.cli('deps', str(path), '--apply', ok=False)
        self.age_tree(path)
        self.config({'protect': ['codex:' + str(f.relative_to(self.home / '.codex'))]})
        self.cli('deps', str(path), '--apply', ok=False)
        self.assertTrue(f.exists())
        self.assertFalse(self.st.exists())

    def test_policy_change_during_scan_aborts_before_quarantine(self):
        f = self.file('.claude/debug/work.log')
        with patch.dict(os.environ, self.env):
            rules = policy.load()
            roots, state = layout()
            candidates = scan(roots, {'claude'}, rules).items
            self.config({'protect': ['claude:debug']})
            with self.assertRaisesRegex(ValueError, '策略已改变'):
                vault.save(candidates, roots, state, rules)
        self.assertTrue(f.exists())
        self.assertFalse(self.st.exists())

    def test_incomplete_scan_never_applies_a_partial_batch(self):
        f = self.file('.claude/debug/example.log')
        (self.home / '.codex').symlink_to(self.home / '.claude')
        d = json.loads(self.cli('clean', '--apply', '--json', ok=False))
        self.assertEqual(d['mode'], 'aborted')
        self.assertEqual(d['moved_files'], 0)
        self.assertTrue(f.exists())
        self.assertFalse(self.st.exists())

    def test_legacy_mixed_batch_only_purges_still_allowed_paths(self):
        key = '0123456789abcdef'
        batch = self.st / key
        self.st.mkdir(mode=0o700, parents=True)
        batch.mkdir(mode=0o700)
        fixtures = [
            ('codex', 'archived_sessions/rollout-test.jsonl.zst', 'archives'),
            ('codex', 'shell_snapshots/old.sh', 'snapshots'),
            ('claude', 'shell-snapshots/test.sh', 'snapshots'),
            ('claude', 'projects/demo/session/tool-results/result.txt', 'tool-results'),
            ('codex', 'log/tui.log', 'logs'),
        ]
        items = []
        for i, (source, rel, cat) in enumerate(fixtures):
            root = self.home / ('.' + source)
            path = self.file(str(Path('.' + source) / rel))
            s = path.stat()
            items.append(dict(root=str(root), path=rel, source=source, category=cat,
                              size=disk_bytes(s), stamp=fingerprint(s)))
            path.rename(batch / str(i))
        (batch / 'manifest.json').write_text(json.dumps({'version': 1, 'items': items}))
        self.assertIn('rollout-test', self.cli('trash', key))
        self.assertIn('1 项', self.cli('purge', key, ok=False))
        self.assertTrue(all((batch / str(i)).exists() for i in range(5)))
        self.cli('purge', key, '--apply', ok=False)
        self.assertFalse((batch / '4').exists())
        self.assertTrue(all((batch / str(i)).exists() for i in range(4)))
        self.cli('restore', key)
        for item in items[:-1]:
            self.assertEqual((Path(item['root']) / item['path']).read_text(), 'fixture-' + Path(item['path']).name)

    def test_mutation_boundary_rejects_read_only_and_mislabelled_items(self):
        archive = self.file('.codex/archived_sessions/rollout-test.jsonl')
        s = archive.stat()
        with patch.dict(os.environ, self.env):
            roots, state = layout()
            for cat in ['archives', 'cache', 'dependencies']:
                item = dict(source='codex', root=str(roots['codex']), path=str(archive.relative_to(roots['codex'])),
                            category=cat, stamp=fingerprint(s), size=disk_bytes(s))
                key, count, _, errors = vault.save([item], roots, state)
                self.assertIsNone(key)
                self.assertEqual(count, 0)
                self.assertTrue(errors)
                self.assertTrue(archive.exists())

    def test_inventory_reports_metadata_and_guides_without_dispatch(self):
        sid = '01234567-89ab-cdef-0123-456789abcdef'
        self.file(f'.codex/sessions/2026/11/rollout-prefix-{sid}.jsonl')
        self.file(f'.codex/archived_sessions/rollout-test-{sid}.jsonl.zst')
        self.file('.codex/archived_sessions/rollout-no-id.jsonl')
        tree = self.file('.codex/worktrees/tree-a/code.py')
        link = self.home / '.codex/worktrees/external'
        link.symlink_to(self.home)
        fake = self.home / 'codex'
        marker = self.home / 'called'
        fake.write_text('#!/bin/sh\ntouch "$HOME/called"\n')
        fake.chmod(0o700)
        self.env['PATH'] = str(self.home) + os.pathsep + self.env['PATH']
        doc = json.loads(self.cli('inventory', '--full', '--limit', '1', '--json'))
        self.assertEqual(doc['errors'], [])
        self.assertEqual(doc['groups']['sessions']['entries'][0]['session_id'], sid)
        self.assertEqual(doc['groups']['archived_sessions']['count'], 2)
        self.assertEqual(len(doc['groups']['archived_sessions']['entries']), 1)
        self.assertEqual(doc['groups']['worktrees']['count'], 1)
        self.assertGreater(doc['groups']['worktrees']['bytes'], 0)
        out = self.cli('inventory')
        for text in ('codex archive <SESSION_ID>', 'codex delete <SESSION_ID>', '归档关联聊天'):
            self.assertIn(text, out)
        self.cli('inventory', '--apply', ok=False)
        self.assertFalse(self.st.exists())
        self.assertTrue(tree.is_file())
        self.assertFalse(marker.exists())

    def test_dry_run_and_apply_are_exclusive(self):
        f = self.file('.claude/debug/old.txt')
        self.cli('clean', '--dry-run', '--apply', ok=False)
        self.assertTrue(f.exists())



if __name__ == '__main__':
    unittest.main()
