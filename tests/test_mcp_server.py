"""Exercise the public stdio protocol against isolated agent data, not live homes."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'install the optional [mcp] extra')
class StdioTools(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name).resolve()
        self.home = home
        self.root = home / 'codex'
        self.log = self.root / 'log' / 'old.log'
        self.log.parent.mkdir(parents=True)
        self.log.write_text('recover me')
        os.utime(self.log, (1, 1))
        self.env = dict(HOME=str(home), CODEX_HOME=str(self.root), CLAUDE_CONFIG_DIR=str(home/'claude'),
                        XDG_STATE_HOME=str(home/'state'), XDG_CONFIG_HOME=str(home/'config'),
                        XDG_CACHE_HOME=str(home/'cache'), PATH=os.environ['PATH'])

    def client(self, *opts):
        from mcp import Client, StdioServerParameters
        return Client(StdioServerParameters(command=sys.executable,
                      args=['-I','-m','clean_my_agent.mcp_server',*opts], env=self.env, cwd=self.home))

    async def test_readonly_catalog_and_reports(self):
        async with self.client() as client:
            tools = (await client.list_tools()).tools
            self.assertEqual({t.name for t in tools},
                {'get_policy','scan_usage','preview_clean','inspect_dependencies','session_inventory','list_batches','extension_inventory','resource_inventory'})
            for t in tools:
                self.assertTrue(t.annotations.read_only_hint)
            for name, args, key in (('get_policy',{},'retention_days'),('scan_usage',{'agent':'codex'},'groups'),
                    ('inspect_dependencies',{},'entries'),('session_inventory',{'agent':'claude'},'agents'),
                    ('list_batches',{},'batches'),('preview_clean',{'agent':'codex'},'files'),
                    ('extension_inventory',{},'entries'), ('resource_inventory',{},'entries')):
                result = await client.call_tool(name,args)
                self.assertFalse(result.is_error, result.content)
                self.assertIn(key, result.structured_content)
            for name, args in [('stage_clean',{'agent':'codex'}),('inspect_dependencies',{'path':'--apply'}),
                               ('session_inventory',{'archive_days':3}),('preview_clean',{'limit':0})]:
                self.assertTrue((await client.call_tool(name,args)).is_error)
            self.assertTrue(self.log.exists())
            self.assertFalse((self.home/'state'/'clean-my-agent').exists())

    async def test_opt_in_stage_and_restore(self):
        async with self.client('--allow-stage') as client:
            tools = (await client.list_tools()).tools
            self.assertEqual({t.name for t in tools if not t.annotations.read_only_hint},
                             {'stage_clean','stage_dependency','restore_batch','stage_resource'})
            other = self.home/'elsewhere'/'node_modules'
            other.mkdir(parents=True)
            refused = await client.call_tool('stage_dependency',{'path':str(other)})
            self.assertTrue(refused.is_error)
            self.assertTrue(other.exists())
            staged = await client.call_tool('stage_clean',{'agent':'codex','days':60})
            self.assertFalse(staged.is_error,staged.content)
            key = staged.structured_content['batch']
            self.assertTrue(key)
            self.assertFalse(self.log.exists())
            self.assertTrue((await client.call_tool('purge',{'batch':key})).is_error)
            info = (await client.call_tool('list_batches',{'batch':key})).structured_content
            self.assertEqual(info['batches'][0]['files'][0]['path'],str(self.log))
            restored = (await client.call_tool('restore_batch',{'batch':key})).structured_content
            self.assertEqual(restored['errors'],[])
            self.assertEqual(restored['restored_items'],1)
            self.assertEqual(self.log.read_text(),'recover me')
