import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from clean_my_agent import actions, dependency_report, deps, policy, storage, vault
from clean_my_agent.scan import fingerprint, layout, scan

# Exercise the retained Python baseline; the root launcher now starts Rust.
CLI = [sys.executable, '-m', 'clean_my_agent']


def without_observation_time(value):
    """Compare scan semantics, excluding only the new wall-clock observation stamp."""
    if isinstance(value, dict):
        return {k: without_observation_time(v) for k, v in value.items() if k != 'observed_at_ns'}
    if isinstance(value, list):
        return list(map(without_observation_time, value))
    return value


class Blocks(unittest.TestCase):
    def test_unavailable_attributes_hardlinks_and_sparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = root / 'node_modules'
            tree.mkdir()
            p = tree/'one'
            p.write_bytes(os.urandom(10000))
            os.link(p, tree/'two')
            os.link(p, root/'outside')
            q = tree/'sparse'
            with q.open('wb') as f:
                f.seek(2**21)
                f.write(b'x')
            (tree/'external').symlink_to(root)
            with patch.object(storage, 'DarwinAttributes', return_value=None):
                r, _, _ = storage.measure(tree)
            allocated = lambda f: f.lstat().st_blocks * 512
            self.assertEqual(r['directory_bytes'], sum(map(allocated, (p,q,tree,tree/'external'))))
            self.assertEqual(r['known_shared_bytes'], allocated(p))
            self.assertIsNone(r['reclaimable_bytes'])
            self.assertEqual(r['reclaimable_max_bytes'], r['directory_bytes'] - allocated(p))
            self.assertIsNone(r['shared_bytes'])

    @unittest.skipUnless(sys.platform == 'darwin', 'requires APFS clonefile')
    def test_apfs_store_clone_private_file_and_internal_hardlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = root/'node_modules'
            tree.mkdir()
            src = root/'store'
            src.write_bytes(os.urandom(64000))
            dest = tree/'copy'
            if ctypes.CDLL(None).clonefile(os.fsencode(src), os.fsencode(dest), 0):
                self.skipTest('volume does not support cloning')
            p = tree/'private'
            p.write_bytes(os.urandom(12000))
            os.link(p, tree/'second-name')
            r, _, _ = storage.measure(tree)
            self.assertEqual(r['shared_bytes'], src.stat().st_blocks * 512)
            self.assertEqual(r['reclaimable_bytes'], p.stat().st_blocks * 512)
            self.assertEqual(r['directory_bytes'], r['shared_bytes'] + r['reclaimable_bytes'])
            src.rename(tree/'last-copy')
            r, _, _ = storage.measure(tree)
            self.assertIsNone(r['reclaimable_bytes'])
            self.assertEqual(r['reclaimable_max_bytes'] - r['reclaimable_min_bytes'], dest.stat().st_blocks*512)


class References(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.home = Path(t.name).resolve()
        self.root = self.home/'.codex'
        self.repo = self.root/'worktrees'/'tree with space'
        self.repo.mkdir(parents=True)
        self.env = dict(HOME=str(self.home), CODEX_HOME=str(self.root), CLAUDE_CONFIG_DIR=str(self.home/'.claude'),
                        XDG_STATE_HOME=str(self.home/'state'), XDG_CONFIG_HOME=str(self.home/'config'))
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.git('init','-q')
        for f, data in [('.gitignore','node_modules/\n'),('package.json','{}'),('pnpm-lock.yaml','{}')]:
            (self.repo/f).write_text(data)
        self.git('add','.')
        self.git('-c','user.name=test','-c','user.email=test@example.invalid','-c','commit.gpgsign=false','commit','-qm','fixture')
        self.other = self.home/'external checkout'
        self.git('worktree','add','-q','--detach',self.other)
        self.path = self.repo/'node_modules'
        self.path.mkdir()
        self.file = self.path/'pkg'
        self.file.write_bytes(b'dependency')
        marker = self.path/'.modules.yaml'
        marker.write_text('layoutVersion: 5\npackageManager: pnpm@10.0.0\n')
        for p in [self.file,self.path,self.repo/'package.json',self.repo/'pnpm-lock.yaml']:
            os.utime(p,(10,10))
        os.utime(marker,(20,20))
        self.alias = self.other/'node_modules'

    def git(self, *args):
        return subprocess.run(['git','-C',self.repo,*args],check=True,capture_output=True)

    def cli(self, *args, ok=True):
        p = subprocess.run([*CLI,*map(str,args)],text=True,capture_output=True)
        self.assertEqual(p.returncode == 0,ok,(p.stdout,p.stderr))
        return p.stdout

    def test_older_git_preserves_quoted_worktree_paths_and_references(self):
        odd = self.home/'é quoted " checkout'
        self.git('worktree','add','-q','--detach',odd)
        (odd/'node_modules').symlink_to(self.path)
        original = deps.git
        def old_git(repo, *args):
            if '-z' in args:
                return subprocess.CompletedProcess(args,129,b'',b'unknown option z')
            return original(repo,*args)
        with patch('clean_my_agent.deps.git',side_effect=old_git):
            refs=deps.References(self.root)
            self.assertEqual(refs.errors,[])
            self.assertIn(odd/'node_modules',refs.incoming(self.path))
            row=dependency_report.inspect(self.root,policy.load())['entries'][0]
            self.assertEqual(row['status'],'retained')
            self.assertIn(str(odd/'node_modules'),row['referenced_by'])

    def test_older_git_retains_when_path_contains_newline(self):
        odd = self.home/'path\nwith newline'
        self.git('worktree','add','-q','--detach',odd)
        (odd/'node_modules').symlink_to(self.path)
        original = deps.git
        def old_git(repo, *args):
            if '-z' in args:
                return subprocess.CompletedProcess(args,129,b'',b'unknown option z')
            return original(repo,*args)
        with patch('clean_my_agent.deps.git',side_effect=old_git):
            refs=deps.References(self.root)
            self.assertTrue(refs.errors)
            with self.assertRaises(deps.Retain):
                refs.guard(self.path)

    def test_scan_deps_and_write_boundary_share_external_reference_protection(self):
        # Simulate a reference appearing after a successful read-only preview.
        self.assertEqual(json.loads(self.cli('deps',self.path,'--json'))['entries'][0]['status'],'candidate')
        item = dict(source='codex',root=str(self.root),path=str(self.path.relative_to(self.root)),
                    category='dependencies',size=4096,stamp=fingerprint(self.path.stat()))
        alias = self.root/'.chatgpt-projects'/'other'/'node_modules'
        alias.parent.mkdir(parents=True)
        alias.symlink_to(os.path.relpath(self.path, alias.parent))
        self.alias.symlink_to(alias)
        d = json.loads(self.cli('deps','--json'))
        s = json.loads(self.cli('scan','--agent','codex','--json'))
        self.assertEqual(without_observation_time(s['node_modules']), without_observation_time(d))
        r = next(r for r in d['entries'] if r['kind']=='directory')
        self.assertEqual(set(r['referenced_by']),{str(self.alias),str(alias)})
        self.assertEqual(r['status'],'retained')
        self.assertEqual(d['entries'][1]['target'],str(self.path))
        self.assertFalse(any(r['status']=='candidate' for r in d['entries']))
        self.cli('deps',self.path,'--apply',ok=False)
        roots,state=layout()
        self.assertEqual(vault.save([item],roots,state)[1],0)
        self.assertTrue(self.file.exists())
        q = json.loads(self.cli('scan','--quick','--json'))['node_modules']
        self.assertEqual(q['mode'],'references_only')
        self.assertIsNone(q['entries'][0]['directory_bytes'])

    def test_dangling_reference_requires_restoring_quarantine(self):
        d=json.loads(self.cli('deps',self.path,'--apply','--json'))
        self.alias.symlink_to(self.path)
        self.cli('purge',d['batch'],'--apply',ok=False)
        self.cli('restore',d['batch'])
        self.assertTrue((self.alias/'pkg').is_file())

    def test_nested_subproject_link_through_a_dependency_is_also_protected(self):
        external = self.home / 'outside packages'
        external.mkdir()
        (self.path/'redirect').symlink_to(external)
        alias = self.other/'packages'/'one'/'node_modules'
        alias.parent.mkdir(parents=True)
        alias.symlink_to(self.path/'redirect')
        refs = deps.References(self.root)
        self.assertEqual(refs.targets[alias], external)
        self.assertEqual(refs.incoming(self.path), [alias])
        with self.assertRaisesRegex(deps.Retain, '其他工作树'):
            refs.guard(self.path)

    def test_dependency_progress_counts_finished_directories_in_each_phase(self):
        self.alias.symlink_to(self.path)
        events = []
        def update(label, done=None, total=None, detail=''):
            events.append((label, done, total, detail))
        def measuring(path, *, on_entries, on_entry):
            self.assertEqual(events[-1][:3], ('测量依赖目录', 0, 1))
            on_entries(3000)
            self.assertIn('3,000 项', events[-1][3])
            return storage.measure(path, on_entry=on_entry)
        baseline = dependency_report.inspect(self.root, policy.load())
        with patch.object(dependency_report, 'measure', side_effect=measuring):
            displayed = dependency_report.inspect(self.root, policy.load(), progress=update)
        self.assertEqual(without_observation_time(baseline), without_observation_time(displayed))
        self.assertEqual(displayed['entries'][0]['status'], 'retained')
        self.assertIn(('测量依赖目录', 1, 1, ''), events)
        self.assertEqual([(n, total) for label,n,total,_ in events if label=='复核工作树引用'],
                         [(0,2), (2,2)])

    def test_scan_progress_keeps_du_hardlink_accounting_across_folders(self):
        other = self.root / 'another' / 'linked-package'
        other.parent.mkdir()
        os.link(self.file, other)
        roots = {'codex': self.root}
        events = []
        def update(label, done=None, total=None, detail=''):
            if label == '统计目录占用':
                events.append((done, total))
        args = roots, {'codex'}, policy.load()
        baseline = scan(*args, full=True)
        measured = scan(*args, full=True, progress=update)
        self.assertEqual(baseline, measured)
        self.assertEqual(events, [(0,2), (1,2), (2,2)])

    def test_incomplete_measurement_excludes_candidate(self):
        with patch.object(dependency_report,'measure',side_effect=PermissionError('unreadable')):
            doc=dependency_report.inspect(self.root,policy.load())
        self.assertTrue(doc['errors'])
        self.assertEqual(doc['entries'][0]['status'],'retained')
        self.assertIsNone(doc['entries'][0]['directory_bytes'])

    def test_install_edit_and_process_change_block_the_write_boundary(self):
        rules = policy.load()
        roots, state = layout()
        selected = dependency_report.inspect(self.root, rules)['entries'][0]
        self.assertEqual(selected['status'], 'candidate')
        item = dict(source='codex', root=str(self.root), path=str(self.path.relative_to(self.root)),
                    category='dependencies', size=selected['directory_bytes'], stamp=selected['stamp'])
        with patch.object(deps, 'require_idle', side_effect=ValueError('进程重新启动')):
            result = vault.save([item], roots, state, rules)
        self.assertEqual(result[1], 0)
        self.assertTrue(self.path.exists())
        os.utime(self.file, (30, 30))  # Ancient, but newer than the installation record.
        preview = dependency_report.inspect(self.root, rules)['entries'][0]
        self.assertEqual(preview['status'], 'retained')
        self.assertEqual(preview['installation']['state'], 'modified')
        self.assertEqual(vault.save([item], roots, state, rules)[1], 0)
        self.assertTrue(self.file.exists())

    def test_unavailable_runtime_and_open_quarantine_prevent_changes(self):
        with patch('clean_my_agent.dependency_report.ProcessSnapshot') as snapshot:
            snapshot.return_value.check.return_value = dict(state='unknown', reason='无法确认运行状态')
            preview = dependency_report.inspect(self.root, policy.load())['entries'][0]
        self.assertEqual(preview['status'], 'retained')
        self.assertEqual(preview['runtime']['state'], 'unknown')
        staged = json.loads(self.cli('deps', self.path, '--apply', '--json'))
        roots, state = layout()
        with patch.object(deps, 'require_idle', side_effect=ValueError('仍有打开的文件')):
            count, _, errors = vault.process(state, staged['batch'], roots, 'purge', True)
        self.assertEqual(count, 0)
        self.assertTrue(errors)
        held = state / staged['batch'] / '0'
        os.utime(held/'pkg', (30,30))
        count, _, errors = vault.process(state, staged['batch'], roots, 'purge', True)
        self.assertEqual(count, 0)
        self.assertTrue(any('安装后有修改' in e for e in errors))
        self.cli('restore', staged['batch'])
        self.assertTrue(self.file.exists())

    def test_panel_rechecks_new_reference_then_stages_and_restores(self):
        rules=policy.load()
        roots,state=layout()
        controller=actions.DependencyActions(roots,state,rules)
        selected=dependency_report.inspect(self.root,rules)['entries'][0]
        self.alias.symlink_to(self.path)
        result=controller.stage(selected,lambda *a,**kw:None)
        self.assertEqual(result['moved_items'],0)
        self.assertIn(str(self.alias),result['row']['referenced_by'])
        self.assertTrue(self.file.exists())
        self.alias.unlink()
        result=controller.stage(selected,lambda *a,**kw:None)
        self.assertEqual(result['moved_items'],1)
        self.assertFalse(self.path.exists())
        self.assertEqual(vault.process(state,result['batch'],roots,'restore',True)[0],1)
        self.assertTrue(self.file.exists())
