import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from clean_my_agent import worktrees, dependency_report, policy, vault
from clean_my_agent.actions import DependencyActions
from clean_my_agent.scan import layout, fingerprint


class Idle:
    def check(self,*args,**kwargs):
        return dict(state='idle',reason='fixture')


class WorktreeActions(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.home=Path(temp.name).resolve()
        p=patch.dict(os.environ,dict(HOME=str(self.home),CODEX_HOME=str(self.home/'.codex'),
            CLAUDE_CONFIG_DIR=str(self.home/'.claude'),T3_HOME=str(self.home/'.t3'),
            XDG_STATE_HOME=str(self.home/'state'),XDG_CONFIG_HOME=str(self.home/'config')))
        p.start(); self.addCleanup(p.stop)
        for name in ('clean_my_agent.worktrees.ProcessSnapshot','clean_my_agent.dependency_report.ProcessSnapshot',
                     'clean_my_agent.dependency_safety.ProcessSnapshot'):
            p=patch(name,Idle); p.start(); self.addCleanup(p.stop)
        self.roots,self.state=layout(); self.policy=policy.load()
        self.main=self.home/'main'; self.main.mkdir()
        self.git(self.main,'init','-q')
        (self.main/'Cargo.toml').write_text('[package]\nname="demo"')
        (self.main/'.gitignore').write_text('target/\nbuild/\n.env\n')
        self.git(self.main,'add','.')
        self.commit(self.main)
        self.path=self.roots['codex']/'worktrees/feature/demo'
        self.git(self.main,'worktree','add','-q','-b','feature',str(self.path))
        self.actions=DependencyActions(self.roots,self.state,self.policy)

    def git(self,path,*args):
        return subprocess.run(['git','-C',str(path),*args],capture_output=True,check=True)

    def commit(self,path):
        self.git(path,'-c','user.name=fixture','-c','user.email=test@example.invalid','-c','commit.gpgsign=false','commit','-qam','fixture')

    def row(self,path=None,agent='codex'):
        p=path or self.path
        return dict(path=str(p),agent=agent,directory_bytes=8192,artifacts=0,stamp=fingerprint(p.lstat()))

    def inspect(self):
        return self.actions.worktree_details(self.row(),lambda *a,**kw:None)

    def old_artifact(self,name='target'):
        p=self.path/name; p.mkdir(); (p/'generated').write_text('build output')
        os.utime(p/'generated',(1,1)); os.utime(p,(1,1))
        return p

    def test_native_remove_preserves_main_and_branch(self):
        self.old_artifact()
        preview=self.inspect(); self.assertTrue(preview['can_delete'],preview['reasons'])
        self.assertEqual(preview['ignored'],['target'])
        original=worktrees.git
        with patch.object(worktrees,'git',wraps=original) as run:
            result=self.actions.delete_worktree(preview,lambda *a,**kw:None)
        self.assertEqual(result['status'],'succeeded',result)
        calls=[c for c in run.call_args_list if 'remove' in c.args]
        self.assertEqual(len(calls),1); self.assertNotIn('--force',calls[0].args)
        self.assertFalse(self.path.exists()); self.assertTrue((self.main/'Cargo.toml').exists())
        self.git(self.main,'show-ref','--verify','refs/heads/feature')
        self.assertNotIn(self.path,worktrees.registered(self.main))

    def test_dirty_untracked_and_ignored_data_retain(self):
        for filename in ('Cargo.toml','notes.txt','.env'):
            with self.subTest(filename=filename):
                p=self.path/filename; old=p.read_text() if p.exists() else None
                p.write_text('user data')
                result=self.inspect(); self.assertFalse(result['can_delete'],result)
                if old is None:p.unlink()
                else:p.write_text(old)
        self.assertTrue(self.path.exists())

    def test_changed_after_confirmation_is_rejected(self):
        preview=self.inspect(); self.assertTrue(preview['can_delete'])
        (self.path/'notes.txt').write_text('new work')
        with self.assertRaisesRegex(ValueError,'状态已变化'):
            self.actions.delete_worktree(preview,lambda *a,**kw:None)
        self.assertTrue(self.path.exists())

    def test_changed_ignored_artifact_after_confirmation_is_rejected(self):
        artifact=self.old_artifact()
        preview=self.inspect(); self.assertTrue(preview['can_delete'],preview['reasons'])
        (artifact/'generated').write_text('new build')
        with self.assertRaises(ValueError):self.actions.delete_worktree(preview,lambda *a,**kw:None)
        self.assertTrue(artifact.exists())

    def test_running_unknown_and_current_directory_retain(self):
        for state in ('busy','unknown'):
            idle=Idle(); idle.check=lambda *a,**kw:dict(state=state,reason='process fixture')
            with patch.object(worktrees,'ProcessSnapshot',return_value=idle):
                self.assertFalse(self.inspect()['can_delete'])
        with patch.object(worktrees.Path,'cwd',return_value=self.path):
            self.assertFalse(self.inspect()['can_delete'])

    def test_external_reference_and_parent_reference_retain(self):
        for target in (self.path,self.path.parent):
            alias=self.main/'shared-tree'; alias.symlink_to(target)
            result=self.inspect()
            self.assertFalse(result['can_delete']); self.assertIn(str(alias),result['referenced_by'])
            alias.unlink()

    def test_other_agent_repository_reference_retain(self):
        other=self.roots['claude']/'worktrees/other'; other.mkdir(parents=True)
        self.git(other,'init','-q')
        (other/'shared').symlink_to(self.path)
        result=self.inspect(); self.assertFalse(result['can_delete'],result)
        self.assertIn(str(other/'shared'),result['referenced_by'])

    def test_primary_locked_and_unique_detached_commits_retain(self):
        primary=self.roots['codex']/'worktrees/primary'
        self.git(self.main,'clone','-q',str(self.main),str(primary))
        row=worktrees.inspect(self.roots,self.state,self.policy,self.row(primary))
        self.assertFalse(row['can_delete'])
        self.git(self.main,'worktree','lock',str(self.path))
        self.assertFalse(self.inspect()['can_delete'])
        self.git(self.main,'worktree','unlock',str(self.path))
        self.git(self.path,'checkout','-q','--detach')
        (self.path/'Cargo.toml').write_text('changed')
        self.commit(self.path)
        result=self.inspect(); self.assertFalse(result['can_delete'])
        self.assertIn('HEAD',result['reasons'][0])

    def test_session_and_workspace_references(self):
        dbpath=self.roots['codex']/'state_1.sqlite'
        with sqlite3.connect(dbpath) as db:
            db.execute('CREATE TABLE threads(id TEXT,cwd TEXT,archived INTEGER,title TEXT)')
            db.execute('INSERT INTO threads VALUES(?,?,0,?)',('thread-id',str(self.path),'工作树会话'))
        result=self.inspect(); self.assertFalse(result['can_delete'])
        self.assertEqual(result['agent_references'][0]['id'],'thread-id')
        with sqlite3.connect(dbpath) as db:db.execute('UPDATE threads SET archived=1')
        self.assertTrue(self.inspect()['can_delete'])
        import json
        (self.roots['codex']/'.codex-global-state.json').write_text(json.dumps({'electron-saved-workspace-roots':[str(self.path)]}))
        self.assertFalse(self.inspect()['can_delete'])

    def test_stage_many_and_pending_restore_protects_worktree(self):
        self.old_artifact('target'); self.old_artifact('build')
        rows=dependency_report.inspect(self.roots['codex'],self.policy)['entries']
        self.assertTrue(all(r['status']=='candidate' for r in rows),rows)
        result=self.actions.stage_many(rows,lambda *a,**kw:None)
        self.assertEqual(result['moved_items'],2,result['errors'])
        self.assertEqual(len(result['moved_paths']),2)
        preview=self.inspect(); self.assertFalse(preview['can_delete'])
        self.assertIn('批次',str(preview['reasons']))
        self.assertEqual(vault.process(self.state,result['batch'],self.roots,'restore',True)[0],2)
        self.assertTrue(self.inspect()['can_delete'])

    def test_native_refusal_never_forces_or_removes_files_itself(self):
        preview=self.inspect(); original=worktrees.git
        def refuse(path,*args,**kwargs):
            if args[:2]==('worktree','remove'):
                return subprocess.CompletedProcess(args,1,b'',b'native refusal')
            return original(path,*args,**kwargs)
        with patch.object(worktrees,'git',side_effect=refuse):
            result=self.actions.delete_worktree(preview,lambda *a,**kw:None)
        self.assertEqual(result['status'],'failed'); self.assertTrue(self.path.exists())

    def test_snapshot_path_cannot_expand_scope(self):
        with self.assertRaises(ValueError):
            self.actions.delete_worktree(dict(self.row(self.main),can_delete=True,token='fake'),lambda *a,**kw:None)
        self.assertTrue(self.main.exists())
