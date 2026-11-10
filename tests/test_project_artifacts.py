import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from clean_my_agent import deps, dependency_report, policy, project_types, vault
from clean_my_agent.scan import layout, fingerprint


class Idle:
    error = None
    commands = []
    files = []
    def check(self, *a, **kw):
        return dict(state='idle', reason='fixture')


class Artifacts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {'HOME':str(self.home), 'CODEX_HOME':str(self.home/'.codex'),
            'CLAUDE_CONFIG_DIR':str(self.home/'.claude'), 'T3_HOME':str(self.home/'.t3'),
            'XDG_STATE_HOME':str(self.home/'state'), 'XDG_CONFIG_HOME':str(self.home/'config')})
        env.start(); self.addCleanup(env.stop)
        self.roots, self.state = layout()
        self.root = self.roots['codex']
        self.repo = self.root/'worktrees/demo'
        self.repo.mkdir(parents=True)
        self.git('init','-q')
        (self.repo/'.gitignore').write_text(''.join(name+'/\n' for name in sorted(project_types.TARGETS)))
        self.policy = policy.load()
        for module in ('clean_my_agent.dependency_report.ProcessSnapshot', 'clean_my_agent.dependency_safety.ProcessSnapshot'):
            mock = patch(module, Idle); mock.start(); self.addCleanup(mock.stop)

    def git(self, *args):
        return subprocess.run(['git','-C',str(self.repo),*map(str,args)], check=True, capture_output=True)

    def commit(self):
        self.git('add','.')
        self.git('-c','user.name=fixture','-c','user.email=test@example.invalid','-c','commit.gpgsign=false','commit','-qm','fixture')

    def file(self, path, value='fixture'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def old(self, path):
        for base, dirs, files in os.walk(path):
            for name in dirs+files:
                os.utime(Path(base)/name, (1,1), follow_symlinks=False)
        os.utime(path,(1,1))

    def item(self, row):
        p = Path(row['path'])
        return dict(source='codex',root=str(self.root),path=str(p.relative_to(self.root)),
                    category='dependencies',size=row['directory_bytes'],stamp=row['stamp'])

    def test_every_mole_artifact_rule_has_a_real_candidate(self):
        cases = {
            'node_modules': ('package.json','{}'), 'target':('Cargo.toml','[package]\nname="demo"'),
            'build':('build.gradle.kts','plugins {}'), 'dist':('package.json','{}'),
            'venv':('requirements.txt','demo==1'), '.venv':('pyproject.toml','[project]'),
            '.pytest_cache':('pyproject.toml','[project]'), '.mypy_cache':('pyproject.toml','[project]'),
            '.tox':('pyproject.toml','[project]'), '.nox':('pyproject.toml','[project]'),
            '.ruff_cache':('pyproject.toml','[project]'), '__pycache__':('pyproject.toml','[project]'),
            '.gradle':('build.gradle','plugins {}'), '.terragrunt-cache':('terragrunt.hcl',''),
            'vendor':('composer.json','{}'), 'bin':('demo.csproj','<Project/>'), 'obj':('demo.csproj','<Project/>'),
            '.dart_tool':('pubspec.yaml','name: demo'), '.zig-cache':('build.zig',''), 'zig-out':('build.zig',''),
            'DerivedData':('demo.xcodeproj/project.pbxproj','{}'), 'Pods':('Podfile',''),
            '.cxx':('build.gradle',''), '.build':('Package.swift',''), 'coverage':('go.mod','module demo'),
        }
        for name in ('.next','.nuxt','.output','.turbo','.parcel-cache','.angular','.svelte-kit','.astro','.expo'):
            cases[name] = ('package.json','{}')
        self.assertEqual(set(cases), project_types.TARGETS)
        for number, (name, (manifest, value)) in enumerate(cases.items()):
            p = self.repo/str(number)
            self.file(p/manifest,value)
            artifact = p/name
            self.file(artifact/('cache.pyc' if name=='__pycache__' else 'Debug/file' if name=='bin' else 'generated'))
            if name=='node_modules':
                self.file(p/'pnpm-lock.yaml','{}')
                self.file(artifact/'.modules.yaml','layoutVersion: 5\n')
                os.utime(p/'package.json',(1,1)); os.utime(p/'pnpm-lock.yaml',(1,1))
            if name in ('venv','.venv'):
                self.file(artifact/'pyvenv.cfg','home = /usr/bin\n')
            if name=='vendor':
                self.file(p/'composer.lock','{}'); self.file(artifact/'composer/installed.json','{}')
            if name=='Pods':
                self.file(p/'Podfile.lock','PODS: []'); self.file(artifact/'Manifest.lock','PODS: []')
            self.old(artifact)
        self.commit()
        report=dependency_report.inspect(self.root,self.policy)
        self.assertEqual(report['errors'],[])
        self.assertEqual(len(report['entries']),len(cases))
        for row in report['entries']:
            with self.subTest(artifact=row['path']):
                self.assertEqual(row['status'],'candidate',row['reason'])
        self.assertEqual(len(report['projects']),1)

    def test_python_staging_restore_and_modified_quarantine_is_retained(self):
        self.file(self.repo/'pyproject.toml','[project]')
        tree=self.repo/'.venv'
        self.file(tree/'pyvenv.cfg','home = /usr/bin')
        self.file(tree/'lib/package.py','original')
        self.old(tree); self.commit()
        row=dependency_report.inspect(self.root,self.policy)['entries'][0]
        key,n,_,errors=vault.save([self.item(row)],self.roots,self.state,self.policy)
        self.assertEqual((n,errors),(1,[])); self.assertFalse(tree.exists())
        self.assertEqual(vault.process(self.state,key,self.roots,'restore',True)[:1],(1,))
        row=dependency_report.inspect(self.root,self.policy)['entries'][0]
        key,n,_,errors=vault.save([self.item(row)],self.roots,self.state,self.policy)
        self.file(self.state/key/'0/lib/package.py','changed after staging')
        n,_,errors=vault.process(self.state,key,self.roots,'purge',True)
        self.assertEqual(n,0); self.assertTrue(errors)
        self.assertEqual(vault.process(self.state,key,self.roots,'restore',True)[0],1)
        self.assertEqual((tree/'lib/package.py').read_text(),'changed after staging')

    def test_external_worktree_alias_protects_non_js_artifact(self):
        self.file(self.repo/'Cargo.toml','[package]')
        target=self.repo/'target'
        self.file(target/'generated'); self.old(target); self.commit()
        other=self.home/'other checkout'
        self.git('worktree','add','-q','--detach',other)
        (other/'shared-build').symlink_to(target)
        row=dependency_report.inspect(self.root,self.policy)['entries'][0]
        self.assertEqual(row['status'],'retained')
        self.assertIn(str(other/'shared-build'),row['referenced_by'])
        self.assertEqual(vault.save([self.item(row)],self.roots,self.state,self.policy)[1],0)
        self.assertTrue(target.exists())

    def test_go_vendor_plain_bin_keys_and_tracked_outputs_stay(self):
        self.file(self.repo/'go.mod','module demo')
        for name in ('vendor','bin','build','target'):
            self.file(self.repo/name/'generated')
            self.old(self.repo/name)
        self.file(self.repo/'build/secret-keypair.json','private')
        self.file(self.repo/'target/source.rs','source')
        self.git('add','-f','target/source.rs')
        self.old(self.repo/'build'); self.old(self.repo/'target'); self.commit()
        report=dependency_report.inspect(self.root,self.policy)
        self.assertTrue(all(r['status']=='retained' for r in report['entries']))

    def test_swift_state_stays_while_compile_output_can_stage(self):
        self.file(self.repo/'Package.swift','')
        build=self.repo/'.build'
        self.file(build/'checkouts/dependency/.git/HEAD','fixture')
        self.file(build/'workspace-state.json','{}')
        output=build/'arm64-apple-macosx'
        self.file(output/'debug/demo'); self.old(build); self.commit()
        rows={r['path']:r for r in dependency_report.inspect(self.root,self.policy)['entries']}
        self.assertEqual(rows[str(build)]['status'],'retained')
        self.assertEqual(rows[str(output)]['status'],'candidate')
        key,n,_,errors=vault.save([self.item(rows[str(output)])],self.roots,self.state,self.policy)
        self.assertEqual((n,errors),(1,[]))
        self.assertEqual(vault.process(self.state,key,self.roots,'purge',True)[0],1)
        self.assertTrue((build/'workspace-state.json').exists())

    def test_unknown_project_is_visible_and_kept(self):
        self.file(self.repo/'README.md','source')
        self.file(self.repo/'build/generated'); self.old(self.repo/'build'); self.commit()
        report=dependency_report.inspect(self.root,self.policy)
        self.assertEqual(report['entries'][0]['status'],'retained')
        self.assertGreater(report['projects'][0]['directory_bytes'],0)

    def test_tagged_cache_and_other_agent_worktrees_restore(self):
        for agent in ('claude','t3'):
            with self.subTest(agent=agent):
                self.root=self.roots[agent]; self.repo=self.root/'worktrees/demo'
                self.repo.mkdir(parents=True); self.git('init','-q')
                self.file(self.repo/'.gitignore','local-cache/\n')
                self.file(self.repo/'pyproject.toml','[project]')
                cache=self.repo/'local-cache'
                self.file(cache/'CACHEDIR.TAG',project_types.CACHE_TAG+'\n')
                self.file(cache/'generated'); self.old(cache); self.commit()
                report=dependency_report.inspect(self.root,self.policy,source=agent)
                row=report['entries'][0]; self.assertEqual(row['status'],'candidate',row['reason'])
                item=self.item(row); item['source']=agent
                key,n,_,errors=vault.save([item],self.roots,self.state,self.policy)
                self.assertEqual((n,errors),(1,[]))
                self.assertEqual(vault.process(self.state,key,self.roots,'restore',True)[0],1)
                self.assertTrue(cache.exists())

    def test_python_ca_bundle_is_rebuildable_but_private_key_is_not(self):
        self.file(self.repo/'pyproject.toml','[project]')
        tree=self.repo/'.venv'; self.file(tree/'pyvenv.cfg','home = /usr/bin')
        cert=self.file(tree/'lib/certifi/cacert.pem','-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----')
        self.old(tree); self.commit()
        self.assertEqual(dependency_report.inspect(self.root,self.policy)['entries'][0]['status'],'candidate')
        cert.write_text('-----BEGIN PRIVATE KEY-----\nfixture'); self.old(tree)
        self.assertEqual(dependency_report.inspect(self.root,self.policy)['entries'][0]['status'],'retained')


if __name__=='__main__':
    unittest.main()
