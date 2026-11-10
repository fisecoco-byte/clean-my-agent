"""Exercise the frozen executable against an isolated home on every release OS."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('executable', type=Path)
args = parser.parse_args()
exe = args.executable.resolve()
with tempfile.TemporaryDirectory() as temp:
    base = Path(temp).resolve()
    fixture = base/'home/.codex/log/old.log'
    fixture.parent.mkdir(parents=True)
    fixture.write_text('release fixture\n'*1024,encoding='utf-8')
    os.utime(fixture,(1,1))
    codex = fixture.parent.parent
    sid = '019a5290-7f49-7c91-8d5e-000000000001'
    session = codex/'sessions'/('rollout-'+sid+'.jsonl')
    session.parent.mkdir()
    session.write_text('{}\n',encoding='utf-8')
    (codex/'session_index.jsonl').write_text(json.dumps({'id':sid,'thread_name':'release fixture'})+'\n',encoding='utf-8')
    env = {**os.environ,'HOME':str(base/'home'),'USERPROFILE':str(base/'home'),
           'CODEX_HOME':str(base/'home/.codex'),'CLAUDE_CONFIG_DIR':str(base/'home/.claude'),
           'T3_HOME':str(base/'home/.t3'),
           'XDG_CONFIG_HOME':str(base/'config'),'XDG_STATE_HOME':str(base/'state'),
           'LOCALAPPDATA':str(base/'local'),'APPDATA':str(base/'roaming'),'PYTHONUTF8':'1',
           'TERM':'dumb'}
    def run(*argv):
        return subprocess.run([str(exe),*argv],cwd=base,env=env,capture_output=True,encoding='utf-8',timeout=90)
    for argv in [('--version',),('--help',),('mcp','--help')]:
        result=run(*argv)
        assert result.returncode==0,result.stderr
    result=run('dashboard','--agent','codex','--json')
    assert result.returncode==0,result.stderr
    data=json.loads(result.stdout)
    assert data['bytes']>0 and not data['errors'],data
    result=run('inventory','--agent','codex','--json')
    assert result.returncode==0,result.stderr
    inventory=json.loads(result.stdout)
    assert not inventory['errors']
    assert inventory['agents']['codex']['session_count']==1
    artifact=codex/'worktrees/rust-demo/target'
    artifact.mkdir(parents=True)
    (artifact/'fixture').write_bytes(b'x'*4096)
    result=run('deps','--agent','codex','--json')
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['entries'][0]['artifact']=='target'
    cache=base/'home/.cache/opencode'
    cache.mkdir(parents=True)
    (cache/'fixture').write_text('cache')
    result=run('resources','--agent','opencode','--quick','--json')
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['entries'][0]['can_stage'] is False
    if os.name=='nt':
        result=run('clean','--apply')
        assert result.returncode!=0
    assert fixture.exists() and not (base/'state').exists()
print('Frozen release smoke checks passed.')
