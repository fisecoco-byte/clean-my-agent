#!/usr/bin/env python3
"""Time the read-only dependency report on the real home directory; record counts, never paths."""
import argparse
import collections
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import tempfile
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--after', type=Path, required=True)
parser.add_argument('--before', type=Path)
parser.add_argument('--agent', default='all')
parser.add_argument('--quick', action='store_true')
parser.add_argument('--runs', type=int, default=1)
args = parser.parse_args()
# Volatile by design: collection time, session activity and running processes.
VOLATILE = {'activity', 'runtime'}


def run(binary):
    command = [str(binary.resolve()), 'deps', '--json', '--agent', args.agent]
    if args.quick:
        command.append('--quick')
    if '--timings' in subprocess.run(
        [command[0], '--help'], capture_output=True, text=True, check=True,
    ).stdout:
        command.append('--timings')
    with tempfile.TemporaryFile() as output:
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.monotonic()
        done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.PIPE)
        wall = time.monotonic() - started
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        # 1 means the report lists scan errors; anything else did not produce one.
        assert done.returncode in (0, 1), f'{binary.name} exited with {done.returncode}'
        output.seek(0)
        report = json.load(output)
    kinds = collections.Counter(row['kind'] for row in report['entries'])
    states = collections.Counter(row['status'] for row in report['entries'])
    stages = [
        {'seconds': float(seconds), 'count': int(count), 'stage': stage}
        for seconds, count, stage in re.findall(
            r'^\s*([\d.]+)\s+(\d+)\s+(.+)$', done.stderr.decode('utf8', 'replace'), re.M)
    ]
    return report, {
        'wall_seconds': round(wall, 3),
        'user_seconds': round(after.ru_utime - before.ru_utime, 3),
        'system_seconds': round(after.ru_stime - before.ru_stime, 3),
        'entries': len(report['entries']), 'kinds': dict(kinds), 'statuses': dict(states),
        'projects': len(report['projects']), 'errors': len(report['errors']),
        'external_targets': len(report['external_targets']), 'stages': stages,
    }


def differences(old, new):
    """Number of rows whose field differs; the home directory may change between runs."""
    result = collections.Counter()
    for section in ('entries', 'projects'):
        rows = [{row['path']: row for row in report[section]} for report in (old, new)]
        result[f'{section}: only before'] = len(rows[0].keys() - rows[1].keys())
        result[f'{section}: only after'] = len(rows[1].keys() - rows[0].keys())
        for path in rows[0].keys() & rows[1].keys():
            for field in (rows[0][path].keys() | rows[1][path].keys()) - VOLATILE:
                if rows[0][path].get(field) != rows[1][path].get(field):
                    result[f'{section}: {field}'] += 1
    result['errors'] = len(set(old['errors']) ^ set(new['errors']))
    result['external_targets'] = sum(
        old['external_targets'].get(key) != new['external_targets'].get(key)
        for key in old['external_targets'].keys() | new['external_targets'].keys())
    return {name: count for name, count in sorted(result.items()) if count}


binaries = [('before', args.before)] * bool(args.before) + [('after', args.after)]
runs, reports = [], {}
for index in range(args.runs):
    for name, binary in binaries:
        reports[name], summary = run(binary)
        runs.append({'build': name, 'run': index + 1, **summary})
print(json.dumps({
    'scope': f'actual home directory, deps --json --agent {args.agent}'
             f'{" --quick" if args.quick else ""}, read-only',
    'runs': runs,
    'differences_in_last_run': differences(reports['before'], reports['after']) if args.before else None,
}, ensure_ascii=False, indent=2))
