#!/usr/bin/env python3
"""Interleaved whole-process comparison on generated files, with equal process snapshots."""
import argparse,importlib.util,json,os,statistics,subprocess,sys,tempfile,time
from pathlib import Path
REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('parity',REPO/'dev/rust-parity.py');parity=importlib.util.module_from_spec(spec);spec.loader.exec_module(parity)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--binary',type=Path,default=REPO/'target/release/clean-my-agent');parser.add_argument('--files',type=int,default=20000);parser.add_argument('--projects',type=int,default=4);parser.add_argument('--runs',type=int,default=3);args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='cma-rust-benchmark-')as temp:
        fixture=parity.Fixtures(Path(temp).resolve());home=fixture.home;bin=home/'bin';bin.mkdir()
        for name,script in {'lsof':"#!/bin/sh\nprintf 'p99999999\\0fcwd\\0n/nonexistent-fixture\\0\\n'\n",'ps':"#!/bin/sh\nprintf '99999999 fixture-process\\n'\n"}.items():
            path=bin/name;path.write_text(script);path.chmod(0o700)
        fixture.env['PATH']=str(bin)+os.pathsep+fixture.env.get('PATH','')
        for n in range(args.projects):
            project=home/f'.codex/worktrees/project-{n}';target=project/'target';target.mkdir(parents=True)
            fixture.file(project/'Cargo.toml','[package]\nname="fixture"\nversion="0.1.0"\n');fixture.file(project/'.gitignore','target/\n')
            fixture.git(project,'init','-q');fixture.git(project,'add','Cargo.toml','.gitignore');fixture.git(project,'-c','user.name=fixture','-c','user.email=test@example.invalid','-c','commit.gpgsign=false','commit','-qm','fixture')
            for i in range(args.files):
                path=target/f'group-{i//100}'/f'file-{i}';path.parent.mkdir(exist_ok=True);path.write_bytes(b'benchmark fixture\n')
            os.link(target/'group-0/file-0',project/'shared-source');(target/'outside-link').symlink_to(home);fixture.old(target)
        commands={'python':[sys.executable,'-c','from clean_my_agent.cli import main; raise SystemExit(main())','deps','--agent','codex','--json'],'rust':[str(args.binary.resolve()),'deps','--agent','codex','--json']}
        results={name:[]for name in commands};reports={}
        for trial in range(args.runs+1):
            for name in list(commands)[::1 if trial%2==0 else -1]:
                started=time.perf_counter();result=subprocess.run(commands[name],cwd=parity.BASELINE,env=fixture.env,capture_output=True,text=True,timeout=180);elapsed=time.perf_counter()-started
                if result.returncode:raise RuntimeError(result.stderr+result.stdout)
                doc=parity.normalized(json.loads(result.stdout));reports[name]=doc
                if trial:results[name].append(elapsed)
            diff=list(parity.differences(reports['python'],reports['rust']))
            if diff:raise RuntimeError('\n'.join(diff[:20]))
        summary={'projects':args.projects,'files':args.files*args.projects,'runs':args.runs,'warm_cache':True,'process_checks':'identical isolated stubs','identical_reports':True,'errors':reports['rust']['errors'],'timing_scope':'whole process including startup, Git checks, traversal and JSON serialization','results':{name:{'median_seconds':round(statistics.median(values),4),'runs_seconds':[round(v,4)for v in values]}for name,values in results.items()}}
        print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
