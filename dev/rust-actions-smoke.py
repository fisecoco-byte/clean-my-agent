#!/usr/bin/env python3
"""Mutation regression tests; all inputs and vaults use an isolated temporary HOME."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('parity',REPO/'dev/rust-parity.py')
parity=importlib.util.module_from_spec(spec);spec.loader.exec_module(parity)
BINARY=Path(os.environ.get('CMA_RUST_BINARY',REPO/'target/debug/clean-my-agent')).resolve()


class Actions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='cma-rust-actions-');self.addCleanup(self.temp.cleanup)
        self.fixture=parity.Fixtures(Path(self.temp.name).resolve());self.home=self.fixture.home
        self.state=self.home/'state/clean-my-agent'

    def cli(self,*args,ok=True):
        process=subprocess.run([str(BINARY),*map(str,args),'--json'],env=self.fixture.env,cwd=REPO,
                               capture_output=True,text=True,timeout=90)
        self.assertEqual(process.returncode==0,ok,(process.stdout,process.stderr))
        return json.loads(process.stdout) if process.stdout else {'errors':[process.stderr]}

    def python(self,code):
        program='from clean_my_agent import vault, policy, dependency_report\nfrom clean_my_agent.scan import layout, scan\nimport json\nroots,state=layout()\n'+code
        output=subprocess.check_output([sys.executable,'-c',program],env=self.fixture.env,cwd=parity.BASELINE,text=True)
        return json.loads(output)

    def file(self,name='old.log'):
        return self.fixture.file('.codex/log/'+name,'fixture log')

    def stage(self):
        result=self.cli('clean','--agent','codex','--apply');self.assertEqual(result['errors'],[]);return result['batch']

    def test_file_roundtrip_and_permanent_purge(self):
        path=self.file();key=self.stage();self.assertFalse(path.exists());self.assertEqual((self.state/key).stat().st_mode&0o777,0o700)
        self.assertEqual((self.state/key/'manifest.json').stat().st_mode&0o777,0o600)
        self.assertEqual(self.cli('restore',key)['count'],1);self.assertEqual(path.read_text(),'fixture log');self.assertFalse((self.state/key).exists())
        key=self.stage();self.assertEqual(self.cli('purge',key)['count'],1);self.assertTrue((self.state/key/'0').exists())
        self.assertEqual(self.cli('purge',key,'--apply')['count'],1);self.assertFalse((self.state/key).exists())

    def test_restore_conflict_preserves_both_files(self):
        path=self.file();key=self.stage();path.write_text('new data');result=self.cli('restore',key,ok=False)
        self.assertTrue(result['errors']);self.assertEqual(path.read_text(),'new data');self.assertEqual((self.state/key/'0').read_text(),'fixture log')

    def test_policy_change_blocks_purge_but_allows_restore(self):
        path=self.file();key=self.stage();self.fixture.file('config/clean-my-agent/policy.json',json.dumps({'protect':['codex:log']}))
        self.cli('purge',key,'--apply',ok=False);self.assertTrue((self.state/key/'0').exists());self.cli('restore',key);self.assertTrue(path.exists())

    def test_python_batch_restores_in_rust(self):
        path=self.file();saved=self.python('result=scan(roots,{"codex"},policy.load())\nprint(json.dumps(vault.save(result.items,roots,state)))')
        self.assertEqual(saved[1],1);self.cli('restore',saved[0]);self.assertEqual(path.read_text(),'fixture log')

    def test_rust_batch_restores_in_python(self):
        path=self.file();key=self.stage();result=self.python(f'print(json.dumps(vault.process(state,{key!r},roots,"restore",True)))')
        self.assertEqual(result,[1,4096,[]]);self.assertEqual(path.read_text(),'fixture log')

    def test_linked_files_and_linked_parents_are_not_cleaned(self):
        path=self.file();os.link(path,self.home/'other-link');self.assertEqual(self.cli('clean','--apply')['moved_files'],0)
        path.unlink();(self.home/'.codex/log').rmdir();(self.home/'.codex/log').symlink_to(self.home/'outside')
        outside=self.fixture.file('outside/old.log');self.assertEqual(self.cli('clean','--apply')['moved_files'],0);self.assertTrue(outside.exists())

    def test_changed_quarantined_file_blocks_purge(self):
        self.file();key=self.stage();(self.state/key/'0').write_text('user changes');self.cli('purge',key,'--apply',ok=False)
        self.cli('restore',key);self.assertEqual((self.home/'.codex/log/old.log').read_text(),'user changes')

    def test_malformed_manifest_cannot_escape_original_root(self):
        self.file();key=self.stage();manifest=self.state/key/'manifest.json';data=json.loads(manifest.read_text());data['items'][0]['path']='../outside';manifest.write_text(json.dumps(data))
        self.cli('restore',key,ok=False);self.cli('purge',key,'--apply',ok=False);self.assertTrue((self.state/key/'0').exists())

    def test_external_link_added_after_staging_blocks_tree_purge(self):
        repo,target=self.fixture.artifacts();result=self.cli('deps',target,'--agent','codex','--apply');key=result['batch'];self.assertEqual(result['moved_items'],1)
        other=self.home/'external-checkout';self.fixture.git(repo,'worktree','add','-q','--detach',other);(other/'shared-build').symlink_to(target)
        self.cli('purge',key,'--apply',ok=False);self.assertTrue((self.state/key/'0').is_dir());self.cli('restore',key);self.assertTrue(target.is_dir())

    def test_modified_tree_is_retained_then_restored(self):
        _,target=self.fixture.artifacts();key=self.cli('deps',target,'--apply')['batch'];(self.state/key/'0/generated').write_text('changed')
        self.cli('purge',key,'--apply',ok=False);self.cli('restore',key);self.assertEqual((target/'generated').read_text(),'changed')

    def test_python_tree_digest_is_compatible(self):
        _,target=self.fixture.artifacts();self.fixture.file(target/'é\n"\u202e😺','unusual name');self.fixture.old(target)
        key=self.cli('deps',target,'--apply')['batch'];result=self.python(f'print(json.dumps(vault.process(state,{key!r},roots,"purge",True)))')
        self.assertEqual(result[0],1,result);self.assertEqual(result[2],[]);self.assertFalse((self.state/key).exists())

    def test_tagged_cache_restores_after_original_disappears(self):
        repo,_=self.fixture.artifacts();cache=repo/'custom-cache';self.fixture.file(cache/'CACHEDIR.TAG','Signature: 8a477f597d28d172789f06886806bc55\n');self.fixture.file(cache/'generated')
        with (repo/'.gitignore').open('a') as stream:stream.write('custom-cache/\n')
        self.fixture.file(repo/'pyproject.toml','[project]')
        self.fixture.git(repo,'add','.gitignore','pyproject.toml');self.fixture.git(repo,'-c','user.name=fixture','-c','user.email=test@example.invalid','-c','commit.gpgsign=false','commit','-qm','cache rule')
        self.fixture.old(cache);key=self.cli('deps',cache,'--apply')['batch'];self.cli('restore',key);self.assertTrue((cache/'generated').exists())

    def test_active_installation_and_previous_version_stay(self):
        for version in ('1.0','2.0','3.0'):
            self.fixture.file(f'.local/share/claude/versions/{version}','binary',days=100-int(version[0]))
        link=self.home/'.local/bin/claude';link.parent.mkdir(parents=True);link.symlink_to(self.home/'.local/share/claude/versions/3.0')
        report=self.cli('resources','--agent','claude');rows={Path(r['path']).name:r for r in report['entries']};self.assertEqual(rows['3.0']['status'],'retained');self.assertEqual(rows['2.0']['status'],'retained')
        old=self.home/'.local/share/claude/versions/1.0';key=self.cli('resources',old,'--agent','claude','--apply')['batch'];self.assertFalse(old.exists());self.cli('restore',key);self.assertTrue(old.exists())


if __name__=='__main__':unittest.main(verbosity=2)
