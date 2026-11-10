#!/usr/bin/env python3
"""Compare the Rust executable with the frozen Python migration baseline.

Every fixture lives in a temporary HOME. Neither implementation is authorized
to mutate real Agent data. Only observation timestamps are discarded.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
BASELINE = REPO / 'build/rust-migration/python-baseline'
DAY = 86400 * 10**9


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k not in ('observed_at_ns', 'generated_at')}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def differences(a, b, path='$'):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(a.keys() | b.keys()):
            if key not in a or key not in b:
                yield f'{path}.{key}: missing in {"Python" if key not in a else "Rust"}'
            else:
                yield from differences(a[key], b[key], f'{path}.{key}')
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            yield f'{path}: lengths {len(a)} != {len(b)}'
        for i, (left, right) in enumerate(zip(a, b)):
            yield from differences(left, right, f'{path}[{i}]')
    elif a != b:
        yield f'{path}: {a!r} != {b!r}'


class Fixtures:
    def __init__(self, home):
        self.home = home
        self.now = time.time_ns()
        self.env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home/'.codex'),
                        CLAUDE_CONFIG_DIR=str(home/'.claude'), T3_HOME=str(home/'.t3'),
                        XDG_STATE_HOME=str(home/'state'), XDG_CONFIG_HOME=str(home/'config'),
                        XDG_CACHE_HOME=str(home/'cache'), npm_config_cache=str(home/'.npm'),
                        PYTHONPATH=str(BASELINE))
        self.env.pop('CODEX_THREAD_ID', None)
        self.env.pop('AGENT_BROWSER_EXECUTABLE_PATH', None)

    def file(self, relative, value='fixture', days=80):
        path = self.home/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        stamp = self.now - days*DAY
        os.utime(path, ns=(stamp, stamp))
        return path

    def old(self, tree, days=80):
        stamp = self.now - days*DAY
        for base, dirs, files in os.walk(tree):
            for name in dirs+files:
                os.utime(Path(base)/name, ns=(stamp, stamp), follow_symlinks=False)
        os.utime(tree, ns=(stamp, stamp))

    def git(self, repo, *args):
        subprocess.run(['git', '-C', str(repo), *map(str, args)], env=self.env,
                       check=True, capture_output=True)

    def sessions(self):
        sid = lambda n: f'01234567-89ab-cdef-9012-{n:012x}'
        for n, days in enumerate([2, 6, 11, 16, 21, 31]):
            self.file(f'.codex/sessions/rollout-t-{sid(n)}.jsonl', 'PRIVATE_BODY', days)
            self.file(f'.claude/projects/project/{sid(n)}.jsonl', json.dumps({
                'type': 'custom-title', 'sessionId': sid(n), 'customTitle': f'标题 {n}'})+'\n', days)
        self.file(f'.codex/archived_sessions/rollout-t-{sid(10)}.jsonl', 'PRIVATE_BODY', 31)
        with sqlite3.connect(self.home/'.codex/state_5.sqlite') as db:
            db.execute('create table threads(id, archived, is_pinned, updated_at_ms, name, title, first_user_message)')
            for n in range(6):
                db.execute('insert into threads values(?,?,?,?,?,?,?)', (sid(n), 0, int(n==5), 0, None, f'标题 {n}', 'PRIVATE_BODY'))
            db.execute('insert into threads values(?,?,?,?,?,?,?)', (sid(10), 1, 0, 0, '用户名称', '旧标题', 'PRIVATE_BODY'))
        self.file('.codex/session_index.jsonl', json.dumps({'id':sid(2), 'thread_name':'\x1b[31m复核\n</script>\u202e周报\x1b[0m'}))
        self.file(f'.claude/projects/project/{sid(5)}/subagents/agent-child.jsonl', 'PRIVATE_BODY', 2)
        self.old(self.home/f'.claude/projects/project/{sid(5)}', 2)
        self.file('.claude/projects/project/sessions-index.json', json.dumps({'entries':[
            {'sessionId':sid(2), 'summary':'索引摘要', 'firstPrompt':'PRIVATE_BODY'}]}))

    def artifacts(self):
        repo = self.home/'.codex/worktrees/tree é with space'
        repo.mkdir(parents=True)
        self.git(repo, 'init', '-q')
        cases = {'target':'Cargo.toml', 'build':'Cargo.toml', 'dist':'package.json',
                 'node_modules':'package.json', '.venv':'pyproject.toml', '__pycache__':'pyproject.toml',
                 '.gradle':'build.gradle', 'vendor':'composer.json', 'obj':'demo.csproj',
                 'Pods':'Podfile', '.dart_tool':'pubspec.yaml', '.zig-cache':'build.zig',
                 '.build':'Package.swift', '.next':'package.json', 'coverage':'go.mod'}
        self.file(repo/'.gitignore', ''.join(n+'/\n' for n in cases))
        for n, (name, manifest) in enumerate(cases.items()):
            base = repo/str(n)
            self.file(base/manifest, '{}')
            self.file(base/name/('cache.pyc' if name=='__pycache__' else 'generated'))
            if name=='node_modules':
                self.file(base/'pnpm-lock.yaml', '{}')
                self.file(base/name/'.modules.yaml', 'layoutVersion: 5\n', 79)
            if name=='.venv': self.file(base/name/'pyvenv.cfg', 'home = /usr/bin')
            if name=='vendor':
                self.file(base/'composer.lock', '{}')
                self.file(base/name/'composer/installed.json', '{}')
            if name=='Pods':
                self.file(base/'Podfile.lock', 'PODS: []')
                self.file(base/name/'Manifest.lock', 'PODS: []')
            if name=='.build':
                self.file(base/name/'arm64-apple-macosx/debug/app')
                self.file(base/name/'workspace-state.json', '{}')
            self.old(base/name)
            if name=='node_modules':
                os.utime(base/name/'.modules.yaml', ns=(self.now-79*DAY,)*2)
        self.git(repo, 'add', '.')
        self.git(repo, '-c','user.name=fixture','-c','user.email=test@example.invalid',
                 '-c','commit.gpgsign=false','commit','-qm','fixture')
        target = repo/'0/target'
        hard = self.file(target/'hard-one', 'allocated'*3000)
        os.link(hard, target/'hard-two')
        os.link(hard, self.home/'external-hardlink')
        sparse = target/'sparse'
        with sparse.open('wb') as f:
            f.seek(2**24); f.write(b'x')
        if sys.platform == 'darwin':
            source = self.file('clone-source', 'clone'*20000)
            ctypes.CDLL(None).clonefile(os.fsencode(source), os.fsencode(target/'clone'), 0)
        self.old(target)
        return repo, target

    def resources(self):
        self.file('Library/Caches/Codex/Default/Cache/data', days=1)
        self.file('Library/Caches/Codex/Default/Cookies', 'KEEP')
        for version in ['1.0', '2.0', '3.0']:
            self.file(f'.codex/packages/app-server-daemon/releases/{version}/codex')
        current = self.home/'.codex/packages/app-server-daemon/current'
        current.symlink_to('releases/3.0')
        self.file('.codex/packages/app-server-daemon/install.lock')
        self.file('.codex/.tmp/marketplaces/.staging/incomplete/data')
        for version in ['1.0', '2.0']:
            base = self.home/f'.claude/plugins/cache/market/tool/{version}'
            self.file(base/'.claude-plugin/plugin.json', json.dumps({'name':'tool', 'version':version}))
            self.file(base/'skills/check/SKILL.md', '---\nname: check\n---\nUNTRUSTED_BODY')
            self.file(base/'.mcp.json', '{"mcpServers":{"api":{"command":"node","env":{"TOKEN":"SECRET_SENTINEL"}}}}')
        self.file('.claude/plugins/cache/market/tool/1.0/.orphaned_at')
        self.file('.claude/plugins/installed_plugins.json', json.dumps({'plugins':{'tool@market':[
            {'scope':'project', 'installPath':str(self.home/'.claude/plugins/cache/market/tool/2.0')} ]}}))
        link = self.home/'.agents/skills/check'
        link.parent.mkdir(parents=True); link.symlink_to(self.home/'.claude/plugins/cache/market/tool/1.0/skills/check')
        self.file('.claude.json', json.dumps({'mcpServers':{'api':{'command':'npx', 'args':['-y','@vendor/server@^2.0.0','--token','SECRET_SENTINEL']}}}))
        for tag, version in [('one','1.0.0'),('two','2.0.0')]:
            self.file(f'.npm/_npx/{tag}/node_modules/@vendor/server/package.json', json.dumps({'name':'@vendor/server', 'version':version}))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, default=REPO/'target/debug/clean-my-agent')
    args=parser.parse_args()
    evidence=REPO/'build/rust-migration/parity'; evidence.mkdir(parents=True, exist_ok=True)
    failed=[]; checks=[]
    with tempfile.TemporaryDirectory(prefix='cma-rust-parity-') as temp:
        fixture=Fixtures(Path(temp).resolve())
        def compare(name, *command):
            outputs=[]
            for implementation, entry in [('python',[sys.executable,'-c','from clean_my_agent.cli import main; raise SystemExit(main())']),('rust',[str(args.binary.resolve())])]:
                start=time.monotonic()
                result=subprocess.run([*entry,*command,'--json'],cwd=BASELINE,env=fixture.env,capture_output=True,text=True,timeout=180)
                if result.returncode not in (0,1):
                    raise RuntimeError(f'{name}/{implementation}: {result.stderr}')
                value=json.loads(result.stdout)
                assert not any(secret in result.stdout for secret in ('PRIVATE_BODY','UNTRUSTED_BODY','SECRET_SENTINEL'))
                (evidence/f'{name}-{implementation}.json').write_text(json.dumps(value,ensure_ascii=False,indent=2))
                outputs.append((result.returncode,normalized(value),time.monotonic()-start))
            diff=list(differences(outputs[0][1],outputs[1][1]))
            if outputs[0][0]!=outputs[1][0]: diff.insert(0,f'exit: {outputs[0][0]} != {outputs[1][0]}')
            if diff:
                failed.append(name)
                (evidence/f'{name}-diff.txt').write_text('\n'.join(diff))
                print(f'FAIL {name}: {len(diff)} differences', flush=True)
                for line in diff[:8]: print('  '+line, flush=True)
            else: print(f'PASS {name}',flush=True)
            checks.append(dict(name=name,passed=not diff,python_seconds=outputs[0][2],rust_seconds=outputs[1][2]))
        compare('empty-policy','policy')
        compare('empty-inventory','inventory')
        compare('empty-scan','scan','--quick')
        fixture.sessions()
        compare('sessions','inventory','--limit','50')
        compare('sessions-thresholds','inventory','--archive-days','5','--delete-days','10','--limit','1')
        repo,target=fixture.artifacts()
        compare('artifacts','deps','--agent','codex')
        compare('single-artifact','deps',str(target),'--agent','codex')
        other=fixture.home/'external é " checkout'
        fixture.git(repo,'worktree','add','-q','--detach',other)
        (other/'shared-build').symlink_to(target)
        compare('external-reference','deps',str(target),'--agent','codex')
        fixture.file('.codex/cache/old')
        fixture.file('.codex/log/new',days=0)
        fixture.file('.claude/debug/old')
        fixture.resources()
        compare('resource-quick','resources','--quick')
        compare('resource-full','resources')
        compare('extensions-quick','extensions','--quick')
        compare('extensions-full','extensions')
        compare('full-scan','scan','--full','--details')
        compare('quick-scan','scan','--quick','--details')
        compare('full-inventory','inventory','--full')
    (evidence/'summary.json').write_text(json.dumps(checks,indent=2))
    print(f'{len(checks)-len(failed)}/{len(checks)} parity cases passed; evidence: {evidence}',flush=True)
    return bool(failed)


if __name__=='__main__':
    raise SystemExit(main())
