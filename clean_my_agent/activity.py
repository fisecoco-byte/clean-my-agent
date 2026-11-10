"""Read-only recency estimates, independent of cleanup eligibility."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from .policy import DAY_NS
from .portable import linked
from .project_types import TARGETS, tagged
from .session_titles import INDEX_LIMIT, regular_file

LABELS = {'high': '高', 'medium': '中', 'low': '低', 'very_low': '极低', 'unknown': '未知'}
COLORS = {'high': '#7de2bf', 'medium': '#c7d98b', 'low': '#efc480', 'very_low': '#e6988d', 'unknown': '#9ab4ac'}
LEGEND = '高 <5 天 · 中 5–9 天 · 低 10–14 天 · 极低 ≥15 天 · 未知另列'
SORT_LABELS = {'cold': '活跃度 ↑', 'hot': '活跃度 ↓', 'size': '占用 ↓'}


def classify(latest_ns=None, *, now_ns=None, basis='', complete=True, running=False, warning=None):
    now = time.time_ns() if now_ns is None else now_ns
    valid = type(latest_ns) is int and 0 < latest_ns <= now
    days = (now - latest_ns) // DAY_NS if valid else None
    level = ('high' if running else 'unknown' if not complete or not valid else
             'very_low' if days >= 15 else 'low' if days >= 10 else 'medium' if days >= 5 else 'high')
    if running:
        latest_ns, days, basis = now, 0, '当前运行进程'
    return dict(level=level, label=LABELS[level], idle_days=days if level != 'unknown' else None,
                latest_ns=latest_ns, basis=basis, estimated=not running, warning=warning,
                observed_at_ns=now)


def value(row):
    data = row.get('activity')
    if not isinstance(data, dict) or not isinstance(data.get('level'), str) or data['level'] not in LABELS:
        return {}
    if any(data.get(k) is not None and not isinstance(data[k], str) for k in ('basis', 'warning', 'workspace')):
        return {}
    if data['level'] != 'unknown' and (type(data.get('idle_days')) is not int or data['idle_days'] < 0):
        return {}
    return data


def badge(row):
    data = value(row)
    level, days = data.get('level', 'unknown'), data.get('idle_days')
    return LABELS[level] + (f' · {days}天' if type(days) is int and days >= 0 and level != 'unknown' else '')


def describe(row):
    data = value(row)
    lines = [badge(row)]
    stamp = data.get('latest_ns')
    if type(stamp) is int and stamp > 0:
        try:
            lines.append('最近记录 ' + datetime.fromtimestamp(stamp / 10**9, timezone.utc).astimezone().isoformat(timespec='seconds'))
        except (ValueError, OverflowError, OSError):
            pass
    lines.append('依据：' + (data.get('basis') or '缺少活动记录'))
    if data.get('warning'):
        lines.append(str(data['warning']))
    if data.get('estimated'):
        lines.append('按可见记录估算，不代表每天使用或可安全删除。')
    return '\n'.join(lines)


def sort_key(row, mode='cold'):
    data = value(row)
    days = data.get('idle_days')
    known = data.get('level') not in (None, 'unknown') and type(days) is int and days >= 0
    # Unknowns always come last; stable identities are used by UI selections.
    return (not known, (days if mode == 'hot' else -days) if known else 0,
            -(row.get('directory_bytes') or row.get('bytes') or 0), row.get('path', row.get('session_id', '')))


def session_records(roots):
    """Only explicit cwd/projectPath metadata; never infer paths from encoded names."""
    records, warnings = [], []
    for agent, root in roots.items():
        try:
            if root.resolve() != root:
                raise ValueError('活动索引目录含符号链接')
            if agent == 'codex':
                from .inventory import thread_index
                thread_index(root, workspace_records=records)
            elif agent == 'claude':
                base = root / 'projects'
                if not base.exists():
                    continue
                if base.is_symlink():
                    raise ValueError('活动索引目录含符号链接')
                for directory in base.iterdir():
                    if directory.is_symlink() or not directory.is_dir():
                        continue
                    path = directory / 'sessions-index.json'
                    if not path.exists():
                        continue
                    with regular_file(path) as stream:
                        if os.fstat(stream.fileno()).st_size > INDEX_LIMIT:
                            raise ValueError('活动索引超过读取上限')
                        doc = json.loads(stream.read(INDEX_LIMIT))
                    for row in doc.get('entries', []):
                        cwd, modified = row.get('projectPath'), row.get('modified')
                        if isinstance(cwd, str) and Path(cwd).is_absolute() and isinstance(modified, str):
                            date = datetime.fromisoformat(modified.replace('Z', '+00:00'))
                            if date.tzinfo is not None:
                                records.append((Path(cwd), int(date.timestamp() * 10**9), 'Claude 会话索引'))
        except (OSError, ValueError, TypeError, AttributeError, RecursionError, sqlite3.Error) as exc:
            warnings.append(f'{agent} 活动索引未完整读取：{exc}')
    return records, warnings


def workspace(path, *, records=(), warnings=(), now_ns=None, runtime=None, progress=None, limit=50000):
    """Read source mtimes and this checkout's own Git metadata, pruning installs."""
    now = time.time_ns() if now_ns is None else now_ns
    if runtime and runtime.get('state') == 'busy':
        return classify(now, now_ns=now, running=True)
    latest, basis, complete = 0, '', not warnings
    notes = list(warnings)
    def observe(stamp, source):
        nonlocal latest, basis
        if stamp > latest:
            latest, basis = stamp, source
    for cwd, stamp, source in records:
        if cwd == path or path in cwd.parents:
            observe(stamp, source)
    try:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or linked(info) or path.resolve() != path:
            raise ValueError('目录含符号链接或不可读取')
        count, reported = 0, 0
        def fail(exc):
            raise exc
        for parent, dirs, files in os.walk(path, followlinks=False, onerror=fail):
            base = Path(parent)
            keep = []
            for name in dirs:
                child = base / name
                count += 1
                if count > limit:
                    raise ValueError('文件数量超过活跃度检查上限')
                if name in TARGETS or name in ('.git', '.Trash', '.nx'):
                    continue
                s = child.lstat()
                if stat.S_ISDIR(s.st_mode) and not linked(s) and s.st_dev == info.st_dev and not tagged(child):
                    keep.append(name)
            dirs[:] = keep
            for name in files:
                count += 1
                if count > limit:
                    raise ValueError('文件数量超过活跃度检查上限')
                if name in ('.git', '.DS_Store'):
                    continue
                s = (base / name).lstat()
                if stat.S_ISREG(s.st_mode) and not linked(s):
                    observe(s.st_mtime_ns, '工作目录文件修改（不含依赖与构建目录）')
            if count > limit:
                raise ValueError('文件数量超过活跃度检查上限')
            if progress and count - reported >= 500:
                progress('检查工作目录活跃度', detail=f'{path.name} · {count:,} 项')
                reported = count
        gitdir = path / '.git'
        if gitdir.is_file() and not gitdir.is_symlink():
            with regular_file(gitdir) as stream:
                text = stream.read(8193).decode('utf-8', errors='strict').strip()
            if not text.startswith('gitdir: ') or len(text) > 8192:
                raise ValueError('Git 工作树元数据无法识别')
            gitdir = (path / text[8:]).resolve()
        if gitdir.is_dir() and not gitdir.is_symlink():
            for name in ('HEAD', 'index', 'logs/HEAD'):
                item = gitdir / name
                if item.exists() and not item.is_symlink():
                    observe(item.stat().st_mtime_ns, '本工作树 Git 操作记录')
    except (OSError, ValueError) as exc:
        complete = False
        notes.append(str(exc))
    if latest > now:
        notes.append('活动时间晚于扫描时间')
    return classify(latest or None, now_ns=now, basis=basis, complete=complete, warning='；'.join(notes) or None)


def annotate(report, roots, *, records=None, processes=None, progress=None):
    evidence, warnings = session_records(roots) if records is None else records
    now = time.time_ns()
    projects = report.get('projects', [])
    for index, row in enumerate(projects):
        path = Path(row['path'])
        def update(label, done=None, total=None, detail=''):
            if progress:
                progress(label, index, len(projects), detail)
        update('检查工作目录活跃度', detail=path.name)
        runtime = processes.check(path, project=path) if processes is not None else None
        row['activity'] = workspace(path, records=evidence, warnings=warnings, now_ns=now, runtime=runtime,
                                    progress=update if progress else None)
    if progress and projects:
        progress('检查工作目录活跃度', len(projects), len(projects))
    # Artifacts inherit the nearest listed workspace, not package extraction times.
    parents = sorted(projects, key=lambda r: len(Path(r['path']).parts), reverse=True)
    for row in report['entries']:
        path = Path(row['path'])
        parent = next((r for r in parents if Path(r['path']) in path.parents), None)
        row['activity'] = dict(parent['activity'], workspace=parent['path']) if parent else classify(now_ns=now)
    return report
