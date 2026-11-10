import fcntl
import os
from pathlib import Path
import plistlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from clean_my_agent import agent_resources, policy, vault
from clean_my_agent.actions import ResourceActions
from clean_my_agent.scan import layout


class Idle:
    error = None
    commands = []
    files = []
    def check(self, *args, **kwargs):
        return dict(state='idle',reason='fixture')


class ResourceActionsTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.home=Path(tmp.name).resolve()
        for p in (patch.dict(os.environ,{'HOME':str(self.home),'CODEX_HOME':str(self.home/'.codex'),
                   'CLAUDE_CONFIG_DIR':str(self.home/'.claude'),'T3_HOME':str(self.home/'.t3'),
                   'XDG_STATE_HOME':str(self.home/'state'),'XDG_CONFIG_HOME':str(self.home/'config')}),
                  patch.object(agent_resources,'sys',SimpleNamespace(platform='darwin')),
                  patch.object(agent_resources,'ProcessSnapshot',Idle)):
            p.start(); self.addCleanup(p.stop)
        self.roots,self.state=layout(); self.policy=policy.load()
        self.actions=ResourceActions(self.roots,self.state,self.policy)

    def file(self, path, text='fixture'):
        p=self.home/path if isinstance(path,str) else path
        p.parent.mkdir(parents=True,exist_ok=True); p.write_text(text)
        os.utime(p,(1,1))
        return p

    def old(self, tree):
        for root,dirs,files in os.walk(tree):
            for name in dirs+files: os.utime(Path(root)/name,(1,1),follow_symlinks=False)
        os.utime(tree,(1,1))

    def row(self, path, agent='codex'):
        return agent_resources.inspect(self.roots,{agent},only=path)['entries'][0]

    def stage(self,row):
        return self.actions.stage(row,lambda *a,**kw:None)

    def test_cache_moves_restores_and_purges_but_cookies_survive(self):
        cache=self.file('Library/Caches/Codex/Default/Cache/data').parent
        cookies=self.file('Library/Caches/Codex/Default/Cookies','durable')
        self.old(cache)
        row=self.row(cache); self.assertTrue(row['can_stage'])
        result=self.stage(row); key=result['batch']
        self.assertEqual(result['errors'],[]); self.assertFalse(cache.exists())
        self.assertEqual(vault.process(self.state,key,self.roots,'restore',True)[0],1)
        self.assertTrue((cache/'data').exists())
        result=self.stage(self.row(cache))
        self.assertEqual(vault.process(self.state,result['batch'],self.roots,'purge',True)[0],1)
        self.assertEqual(cookies.read_text(),'durable')

    def test_process_start_between_report_and_stage_retains(self):
        cache=self.file('Library/Caches/Codex/Default/Cache/data').parent; self.old(cache)
        row=self.row(cache)
        busy=Idle(); busy.commands=[(123,'/Applications/Codex.app/Contents/MacOS/Codex')]
        with patch.object(agent_resources,'ProcessSnapshot',return_value=busy):
            result=self.stage(row)
        self.assertEqual(result['moved_items'],0); self.assertTrue(cache.exists())
        self.assertIn('123',result['errors'][0])

    def test_browser_pin_added_after_staging_blocks_purge(self):
        root=self.home/'.agent-browser/browsers'
        for version in ('100.0.0','200.0.0'):
            self.file(root/f'chrome-{version}/chrome-linux64/chrome')
            self.old(root/f'chrome-{version}')
        old=root/'chrome-100.0.0'
        result=self.stage(self.row(old,'agent-browser'))
        self.assertEqual(result['moved_items'],1,result['errors'])
        self.file(root.parent/'config.json','{"executablePath":"'+str(old/'chrome-linux64/chrome')+'"}')
        n,_,errors=vault.process(self.state,result['batch'],self.roots,'purge',True)
        self.assertEqual(n,0); self.assertTrue(errors)
        self.assertEqual(vault.process(self.state,result['batch'],self.roots,'restore',True)[0],1)

    def test_non_dictionary_update_metadata_is_unknown(self):
        path=self.home/'Info.plist'
        path.write_bytes(plistlib.dumps(['unexpected']))
        self.assertIsNone(agent_resources.app_build(path))

    def test_update_needed_by_either_installed_app_stays(self):
        stage=self.home/'update'
        self.file(stage/'Codex.app/Contents/Info.plist')
        def build(path):
            return 200 if stage in path.parents else 300 if self.home in path.parents else 100
        with patch.object(agent_resources,'app_build',side_effect=build):
            self.assertTrue(agent_resources.newer_update(stage))

    def test_other_agent_cache_families_stage_and_restore(self):
        cases={
            'claude':'Library/Application Support/Claude/Cache',
            't3':'Library/Application Support/t3code-v2/Partitions/t3code-preview-fixture/Cache',
            'antigravity':'.gemini/antigravity-browser-profile/Default/Cache',
            'filo':'Library/Application Support/Filo/production/Cache',
            'qoder':'Library/Application Support/Qoder/CachedData',
            'opencode':'.cache/opencode',
            'chrome-devtools':'.cache/chrome-devtools-mcp/chrome-profile/Default/Cache',
        }
        for agent,relative in cases.items():
            with self.subTest(agent=agent):
                path=self.file(relative+'/data').parent; self.old(path)
                result=self.stage(self.row(path,agent))
                self.assertEqual(result['moved_items'],1,result['errors'])
                self.assertEqual(vault.process(self.state,result['batch'],self.roots,'restore',True)[0],1)
                self.assertTrue((path/'data').exists())

    def test_cursor_copilot_and_sdk_keep_active_and_one_previous(self):
        for agent,relative,linkpath in (
            ('cursor','.local/share/cursor-agent/versions','.local/bin/cursor-agent'),
            ('copilot','.copilot/pkg/universal','.local/bin/copilot')):
            root=self.home/relative; self.versions(root,self.home/linkpath)
            rows=agent_resources.inspect(self.roots,{agent})['entries']
            self.assertEqual([Path(r['path']).name for r in rows if r['can_stage']],['1.0.0'])
        base=self.home/'Library/Application Support/Claude'
        for folder in ('claude-code','claude-code-vm'):
            for n in range(1,4):
                path=self.file(base/folder/f'{n}.0.0/agent').parent
                self.old(path); os.utime(path,(n,n))
        self.file(base/'claude-code-vm/.sdk-version','3.0.0')
        rows=agent_resources.inspect(self.roots,{'claude'})['entries']
        self.assertEqual(sorted(Path(r['path']).name for r in rows if r['can_stage']),['1.0.0','1.0.0'])

    def versions(self, root, link):
        for n in range(1,4):
            p=self.file(root/f'{n}.0.0/bin/agent').parent.parent
            self.old(p); os.utime(p,(n,n))
        link.parent.mkdir(parents=True,exist_ok=True)
        link.symlink_to(root/'3.0.0/bin/agent')

    def test_active_and_previous_versions_kept_and_pointer_flip_blocks_purge(self):
        root=self.home/'.local/share/claude/versions'; launch=self.home/'.local/bin/claude'
        self.versions(root,launch)
        rows=agent_resources.inspect(self.roots,{'claude'})['entries']
        candidates=[r for r in rows if r['can_stage']]
        self.assertEqual([Path(r['path']).name for r in candidates],['1.0.0'])
        result=self.stage(candidates[0]); key=result['batch']
        launch.unlink(); launch.symlink_to(root/'1.0.0/bin/agent')
        n,_,errors=vault.process(self.state,key,self.roots,'purge',True)
        self.assertEqual(n,0); self.assertTrue(errors)
        self.assertEqual(vault.process(self.state,key,self.roots,'restore',True)[0],1)
        self.assertTrue(launch.is_file())

    def test_daemon_install_lock_and_update_marker_protect_versions(self):
        daemon=self.roots['codex']/'packages/app-server-daemon'
        root=daemon/'releases'
        for n in range(1,5):
            self.file(root/f'{n}.0.0/agent'); self.old(root/f'{n}.0.0'); os.utime(root/f'{n}.0.0',(n,n))
        (daemon/'current').symlink_to(root/'4.0.0')
        self.file(daemon/'auto-update-version','3.0.0')
        lock=self.file(daemon/'install.lock','')
        row=self.row(root/'1.0.0'); self.assertTrue(row['can_stage'])
        with lock.open('r+') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=self.stage(row)
            self.assertEqual(result['moved_items'],0); self.assertTrue(result['errors'])
        self.assertEqual(self.stage(self.row(root/'1.0.0'))['moved_items'],1)
        for n in (2,3,4): self.assertTrue((root/f'{n}.0.0').exists())

    def test_runtime_and_marketplace_states_are_not_generic_tmp_cleanup(self):
        runtime=self.home/'.cache/codex-runtimes'
        for name in ('tmp-abandoned','complete','unknown'):
            self.file(runtime/name/'data'); self.old(runtime/name)
        self.file(runtime/'complete/runtime.json','{}')
        self.file(runtime/'complete/dependencies/node/package.json','{}')
        self.file(runtime/'unknown/dependencies/file','state')
        for name in ('complete','unknown'): self.old(runtime/name)
        market=self.roots['codex']/'.tmp/bundled-marketplaces'
        self.file(market/'openai-bundled.staging-old/.git/HEAD')
        self.file(market/'finished-market/data'); self.old(market)
        rows=agent_resources.inspect(self.roots,{'codex'})['entries']
        candidates={Path(r['path']) for r in rows if r['can_stage']}
        self.assertEqual(candidates,{runtime/'tmp-abandoned',market/'openai-bundled.staging-old'})

    def test_newer_sparkle_update_is_retained_even_when_old(self):
        staging=self.home/'Library/Caches/com.openai.codex/org.sparkle-project.Sparkle/Installation/old'
        plist=self.file(staging/'Codex.app/Contents/Info.plist','')
        plist.write_bytes(plistlib.dumps({'CFBundleVersion':'200'})); self.old(staging)
        original=agent_resources.app_build
        with patch.object(agent_resources,'app_build',side_effect=lambda p: original(p) if staging in p.parents else 100):
            row=self.row(staging)
        self.assertFalse(row['can_stage']); self.assertIn('更新',row['reason'])

    def test_symlink_and_incomplete_process_information_never_authorize(self):
        cache=self.home/'Library/Caches/Codex/Default/Cache'
        outside=self.file('private/data').parent; self.old(outside)
        cache.parent.mkdir(parents=True); cache.symlink_to(outside)
        self.assertFalse(self.row(cache)['can_stage'])
        cache.unlink(); cache.mkdir(); self.file(cache/'data'); self.old(cache)
        unknown=Idle(); unknown.error='lsof unavailable'
        with patch.object(agent_resources,'ProcessSnapshot',return_value=unknown):
            self.assertFalse(self.row(cache)['can_stage'])
        self.assertTrue((outside/'data').exists())

    def test_crash_report_and_cursor_log_can_restore_after_original_disappears(self):
        for relative,agent in [('Library/Application Support/Codex/Crashpad/pending/report.dmp','codex'),
                               ('.local/share/cursor-agent/logs/old.log','cursor')]:
            path=self.file(relative)
            result=self.stage(self.row(path,agent))
            self.assertEqual(result['moved_items'],1,result['errors'])
            self.assertEqual(vault.process(self.state,result['batch'],self.roots,'restore',True)[0],1)
            self.assertTrue(path.exists())


if __name__=='__main__':
    unittest.main()
