import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clean_my_agent import extensions, resources, policy
from clean_my_agent.scan import layout, scan


class ResourceInventory(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {'HOME': str(self.home), 'CODEX_HOME': str(self.home/'custom-codex'),
                         'CLAUDE_CONFIG_DIR': str(self.home/'custom-claude'),
                         'XDG_CONFIG_HOME': str(self.home/'config')})
        env.start()
        self.addCleanup(env.stop)
        self.roots, self.state = layout()

    def file(self, relative, text='data'):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def plugin(self, agent, version):
        base = f'custom-{agent}/plugins/cache/market/tool/{version}'
        self.file(base + '/.claude-plugin/plugin.json', json.dumps({'name':'tool','version':version}))
        self.file(base + '/skills/check/SKILL.md', '---\nname: check\n---\nDo not execute this fixture.')
        self.file(base + '/.mcp.json', '{"mcpServers":{"api":{"command":"node","args":["server.js"],"env":{"TOKEN":"secret"}}}}')
        return self.home / base

    def test_known_external_paths_readonly_and_internal_count_scope(self):
        external = self.file('Library/Caches/Codex/Default/Cache/test')
        self.file('Library/Caches/Codex/Default/Cookies/keep')
        internal = self.file('custom-codex/packages/app-server-daemon/releases/1.0/codex')
        active = internal.parent.parent.parent / 'current'
        active.symlink_to(internal.parent)
        self.file('custom-codex/.tmp/marketplaces/.staging/incomplete/data')
        self.file('.local/share/claude/versions/1.0')
        with patch.object(resources.sys, 'platform', 'darwin'):
            result = resources.inspect(self.roots, {'codex','claude'})
        self.assertFalse(result['errors'], result['errors'])
        by_path = {r['path']:r for r in result['entries']}
        self.assertFalse(by_path[str(external.parent)]['included_in_primary'])
        self.assertTrue(by_path[str(internal.parent)]['included_in_primary'])
        self.assertEqual(by_path[str(internal.parent)]['referenced_by'], [str(active)])
        self.assertNotIn('Cookies', json.dumps(list(by_path)))
        self.assertTrue(all(r['can_stage'] is False for r in result['entries']))
        self.assertFalse(scan(self.roots, {'codex','claude'}, policy.load(), full=True).items)
        self.assertFalse(self.state.exists())

    def test_missing_roots_are_not_errors_and_quick_does_not_measure(self):
        self.file('.local/share/claude/versions/1.0')
        with patch.object(resources, 'measure', side_effect=AssertionError('quick must not measure')):
            result = resources.inspect(self.roots, {'claude'}, full=False)
        self.assertFalse(result['errors'])
        self.assertIsNone(result['entries'][0]['directory_bytes'])

    def test_symlink_external_resource_never_follows_target(self):
        target = self.file('private/location/data').parent
        link = self.home/'Library/Caches/Codex/Default/Cache'
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        with patch.object(resources.sys, 'platform', 'darwin'), \
                patch.object(resources, 'measure', side_effect=AssertionError('must not traverse link')):
            result = resources.inspect(self.roots, {'codex'})
        self.assertEqual(result['entries'][0]['measurement'], 'link')

    def test_plugin_and_bundled_components_group_versions_without_double_count(self):
        old, new = self.plugin('claude', '1.0'), self.plugin('claude', '2.0')
        self.file('custom-claude/plugins/installed_plugins.json', json.dumps({'plugins':{
            'tool@market':[{'scope':'project','installPath':str(new),'version':'2.0'}]}}))
        self.file(str(old.relative_to(self.home))+'/.orphaned_at', 'native-marker')
        result = extensions.inspect(self.roots, {'claude'})
        self.assertEqual({g['category'] for g in result['groups']}, {'plugin','skill','mcp'})
        plugins = [r for r in result['entries'] if r['category']=='plugin']
        self.assertEqual(next(r for r in plugins if r['version']=='1.0')['status'], 'owner-managed')
        self.assertTrue(next(r for r in plugins if r['version']=='2.0')['referenced_by'])
        nested = [r for r in result['entries'] if r.get('parent_path')]
        self.assertTrue(all(r['directory_bytes'] is None for r in nested))
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('TOKEN', json.dumps(result))
        self.assertTrue(all(not r['can_stage'] for r in result['entries']))
        self.assertTrue(old.is_dir() and new.is_dir())

    def test_skill_alias_protects_old_plugin_across_agents(self):
        old = self.plugin('claude', '1.0')
        self.plugin('claude', '2.0')
        self.file(str(old.relative_to(self.home))+'/.orphaned_at')
        self.file('custom-claude/plugins/installed_plugins.json', '{"plugins":{}}')
        link = self.home/'.agents/skills/check'
        link.parent.mkdir(parents=True)
        link.symlink_to(old/'skills/check')
        result = extensions.inspect(self.roots, {'claude'}, full=False)
        plugin = next(r for r in result['entries'] if r['category']=='plugin' and r['version']=='1.0')
        self.assertIn(str(link), plugin['referenced_by'])
        self.assertEqual(plugin['status'], 'retained')

    def test_mcp_real_versions_not_declared_ranges_and_redacts_secrets(self):
        self.file('.claude.json', json.dumps({'mcpServers':{
            'api':{'command':'npx','args':['-y','@vendor/server@^2.0.0','--token','TOPSECRET'], 'env':{'TOKEN':'TOPSECRET'}},
            'remote':{'type':'http','url':'https://user:TOPSECRET@example.test/path?token=TOPSECRET'}}}))
        for tag, ver in [('one','1.0.0'), ('two','2.0.0')]:
            self.file(f'.npm/_npx/{tag}/node_modules/@vendor/server/package.json',
                      json.dumps({'name':'@vendor/server','version':ver}))
        result = extensions.inspect(self.roots, {'claude'}, full=False)
        group = next(g for g in result['groups'] if g['identity']=='mcp:npm:@vendor/server')
        self.assertEqual(group['versions'], ['1.0.0','2.0.0'])
        self.assertNotIn('TOPSECRET', json.dumps(result))
        self.assertTrue(all(not r['can_stage'] for r in result['entries']))

    def test_invalid_registry_retains_every_version(self):
        self.plugin('claude','1.0')
        self.file('custom-claude/plugins/installed_plugins.json', '{ broken')
        result = extensions.inspect(self.roots, {'claude'}, full=False)
        self.assertTrue(result['errors'])
        self.assertTrue(all(not r['can_stage'] for r in result['entries']))


if __name__ == '__main__':
    unittest.main()
