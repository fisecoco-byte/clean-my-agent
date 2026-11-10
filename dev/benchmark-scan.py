#!/usr/bin/env python3
"""Repeatable scanner benchmark on generated files; never scans the user's home.

Example: .venv/bin/python dev/benchmark-scan.py --baseline /tmp/previous-source
The baseline directory must contain clean_my_agent/. Process discovery is stubbed
identically in both versions to isolate filesystem scanning and accounting.
"""
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k != 'observed_at_ns'}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def worker(args):
    sys.path.insert(0, str(args.module_root))
    from clean_my_agent import dependency_report, policy
    class Idle:
        def check(self, *a, **kw):
            return dict(state='idle', reason='benchmark fixture')
    dependency_report.ProcessSnapshot = Idle
    roots = {'codex': args.worker_root}
    options = dict(agents=('codex',))
    first = []
    if 'workers' in inspect.signature(dependency_report.inspect_agents).parameters:
        options['workers'] = args.workers
    if 'on_preview' in inspect.signature(dependency_report.inspect_agents).parameters:
        options['on_preview'] = lambda report: first.append(time.perf_counter() - started)
    started = time.perf_counter()
    report = dependency_report.inspect_agents(roots, policy.load(), **options)
    elapsed = time.perf_counter() - started
    digest = hashlib.sha256(json.dumps(normalized(report), sort_keys=True).encode()).hexdigest()
    print(json.dumps(dict(seconds=elapsed, first_list_seconds=first[0] if first else elapsed,
                          digest=digest, errors=len(report['errors']), artifacts=len(report['entries']))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--projects', type=int, default=4)
    parser.add_argument('--files', type=int, default=4000, help='files per project')
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--worker-root', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--module-root', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--workers', type=int, default=4, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_root:
        worker(args)
        return
    if min(args.projects, args.files, args.runs) < 1:
        parser.error('projects, files and runs must be positive')
    current = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='cma-benchmark-') as tmp:
        home = Path(tmp).resolve()
        root = home / 'codex'
        for index in range(args.projects):
            project = root / 'worktrees' / f'project-{index}'
            tree = project / 'target'
            tree.mkdir(parents=True)
            (project / 'Cargo.toml').write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n')
            for number in range(args.files):
                folder = tree / f'pkg-{number // 100}'
                folder.mkdir(exist_ok=True)
                (folder / f'file-{number}').write_bytes(b'benchmark fixture\n')
            # A source/artifact hard link must count once in the project total.
            os.link(tree / 'pkg-0/file-0', project / 'shared-source')
            (tree / 'outside-link').symlink_to(home)
        env = {**os.environ, 'HOME': str(home), 'CODEX_HOME': str(root),
               'CLAUDE_CONFIG_DIR': str(home / 'claude'), 'XDG_CONFIG_HOME': str(home / 'config')}
        variants = ([('baseline', args.baseline.resolve(), 1)] if args.baseline else [])
        variants += [(f'current-{n}-workers', current, n) for n in (1, 2, 4)]
        results = {name: [] for name, _, _ in variants}
        # Warm each variant, then interleave trials to reduce ordering bias.
        for trial in range(args.runs + 1):
            for name, module_root, workers in variants:
                cmd = [sys.executable, str(Path(__file__).resolve()), '--worker-root', str(root),
                       '--module-root', str(module_root), '--workers', str(workers)]
                result = json.loads(subprocess.check_output(cmd, env=env))
                if trial:
                    results[name].append(result)
        summary = {name: dict(median_seconds=round(statistics.median(r['seconds'] for r in runs), 4),
                             first_list_seconds=round(statistics.median(r['first_list_seconds'] for r in runs), 4),
                             errors=max(r['errors'] for r in runs)) for name, runs in results.items()}
        digests = {r['digest'] for runs in results.values() for r in runs}
        print(json.dumps(dict(projects=args.projects, files=args.projects*args.files, runs=args.runs,
                              warm_cache=True, process_checks='stubbed', identical_reports=len(digests) == 1,
                              results=summary), indent=2))
        if len(digests) != 1:
            raise SystemExit('Report mismatch: investigate before accepting timings')


if __name__ == '__main__':
    main()
