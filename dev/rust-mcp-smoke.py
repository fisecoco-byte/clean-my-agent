#!/usr/bin/env python3
"""Protocol and CLI regression checks against isolated HOME and the real binary."""
import importlib.util,json,os,queue,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('actions',REPO/'dev/rust-actions-smoke.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class Client:
    def __init__(self,env,write=False):
        self.process=subprocess.Popen([str(module.BINARY),'mcp',*(['--allow-stage'] if write else [])],env=env,cwd=REPO,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.queue=queue.Queue();self.number=0
        def read():
            for line in self.process.stdout:self.queue.put(json.loads(line))
            self.queue.put(None)
        self.reader=threading.Thread(target=read,daemon=True);self.reader.start()
        self.request('initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'migration-fixture','version':'1'}})
        self.send({'jsonrpc':'2.0','method':'notifications/initialized'})
    def send(self,message):self.process.stdin.write(json.dumps(message)+'\n');self.process.stdin.flush()
    def request(self,method,params=None):
        self.number+=1;self.send({'jsonrpc':'2.0','id':self.number,'method':method,**({'params':params}if params is not None else {})})
        while True:
            message=self.queue.get(timeout=90)
            if message is None:raise AssertionError(self.process.stderr.read())
            if message.get('id')==self.number:return message
    def call(self,name,args={}):return self.request('tools/call',{'name':name,'arguments':args})
    def close(self):
        self.process.stdin.close()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=5)
        self.process.stdout.close();self.process.stderr.close()

class Migration(module.Actions):
    def test_mcp_readonly_and_parameter_validation(self):
        self.file();self.fixture.sessions();self.fixture.artifacts();client=Client(self.fixture.env);self.addCleanup(client.close)
        tools=client.request('tools/list')['result']['tools'];self.assertEqual(len(tools),8);self.assertTrue(all(t['annotations']['readOnlyHint'] for t in tools))
        for name in [t['name']for t in tools]:
            response=client.call(name);self.assertNotIn('error',response,response);self.assertFalse(response['result'].get('isError'),response)
            self.assertIn('structuredContent',response['result'])
        for args in [{'limit':0},{'limit':101},{'archive_days':True},{'delete_days':7},{'agent':'t3'},{'unknown':True}]:
            result=client.call('session_inventory',args);self.assertTrue(result.get('error') or result['result'].get('isError'),result)
        self.assertIn('error',client.call('stage_clean',{'agent':'codex'}));self.assertTrue((self.home/'.codex/log/old.log').exists());self.assertFalse(self.state.exists())

    def test_mcp_opt_in_stage_and_restore(self):
        path=self.file();client=Client(self.fixture.env,True);self.addCleanup(client.close)
        tools=client.request('tools/list')['result']['tools'];self.assertEqual(len(tools),12);self.assertFalse(any('purge'in t['name']or'delete'in t['name']for t in tools))
        result=client.call('stage_clean',{'agent':'codex'})['result'];self.assertFalse(result.get('isError'),result);key=result['structuredContent']['batch'];self.assertFalse(path.exists())
        batches=client.call('list_batches',{'batch':key})['result']['structuredContent'];self.assertEqual(batches['batches'][0]['items'],1);self.assertEqual(batches['batches'][0]['files'][0]['path'],str(path))
        restored=client.call('restore_batch',{'batch':key})['result'];self.assertFalse(restored.get('isError'),restored);self.assertEqual(restored['structuredContent']['restored_items'],1);self.assertTrue(path.exists())

    def test_policy_defaults_preserve_existing_protection(self):
        config=self.fixture.file('config/clean-my-agent/policy.json',json.dumps({'profile':'deep','protect':['codex:log/keep.log'],'sessions':{'claude':{'archive_days':15}}}))
        result=self.cli('policy','--agent','codex','--set-session-defaults','--archive-days','5','--delete-days','15')
        self.assertEqual(result['sessions']['codex'],{'archive_days':5,'delete_days':15});self.assertEqual(result['sessions']['claude']['archive_days'],15)
        self.assertEqual(json.loads(config.read_text())['protect'],['codex:log/keep.log']);self.assertEqual(config.stat().st_mode&0o777,0o600)
        before=config.read_bytes();self.cli('policy','--set-session-defaults','--archive-days','3',ok=False);self.assertEqual(config.read_bytes(),before)

    def test_snapshot_readonly_and_html_export(self):
        self.fixture.sessions();_,target=self.fixture.artifacts();report=self.cli('deps','--agent','codex');snapshot=self.home/'report.json';snapshot.write_text(json.dumps(report));self.assertEqual(self.cli('deps','--from-report',snapshot),report)
        self.cli('deps','--from-report',snapshot,'--apply',ok=False);self.assertTrue(target.exists())
        html=self.home/'sessions.html';doc=self.cli('inventory','--html',html);self.assertEqual(doc['html_path'],str(html));text=html.read_text();self.assertIn('Content-Security-Policy',text);self.assertNotIn('@@PAYLOAD@@',text)
        before=html.read_bytes();self.cli('inventory','--html',html,ok=False);self.assertEqual(html.read_bytes(),before)

    def test_restore_dry_run_cannot_write(self):
        path=self.file();key=self.stage();self.cli('restore',key,'--dry-run',ok=False);self.assertFalse(path.exists());self.assertTrue((self.state/key/'0').exists())

if __name__=='__main__':unittest.main(verbosity=2)
