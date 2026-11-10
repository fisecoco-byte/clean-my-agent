"""Read-only session inventory and review suggestions from local metadata."""
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from contextlib import closing
from datetime import datetime, timezone

from .deps import usage
from .policy import AGENTS, DAY_NS, SESSION_DAYS
from .progress import counted
from .scan import ARCHIVE, disk_bytes
from .session_titles import claude_index, claude_title, codex_titles, label
from .portable import stat_at, allocated

MANAGEMENT = {'archive': 'codex archive <SESSION_ID>', 'delete': 'codex delete <SESSION_ID>',
              'worktrees': '先保留要用的改动，再在 Codex 桌面端归档关联聊天；在设置的 Worktrees 页面管理自动回收。'}
CLAUDE_GUIDE = '用 claude --resume <SESSION_ID> 核对普通本地会话。无法识别原生归档状态，两类建议都需人工复核。'
SECTIONS = {'sessions': '会话文件', 'archived_sessions': '已归档会话文件', 'worktrees': '工作树目录项'}
UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
UUID_SUFFIX = re.compile(r'(' + UUID + r')\.jsonl(?:\.zst)?\Z', re.I)
BUCKETS = ('0–4 天', '5–9 天', '10–14 天', '15–19 天', '20 天以上')


def walk(base, errors):
    if os.name == 'nt':
        from .portable import walk as windows_walk
        yield from windows_walk(base, errors)
        return
    try:
        s = base.lstat()
        if not stat.S_ISDIR(s.st_mode):
            raise ValueError(f'{base} 不是实际目录，已跳过')
        def onerror(e):
            errors.append(str(e))
        for parent, dirs, files, fd in os.fwalk(base, follow_symlinks=False, onerror=onerror):
            allowed = []
            for d in dirs:
                entry = os.stat(d, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(entry.st_mode) and entry.st_dev == s.st_dev:
                    allowed.append(d)
            dirs[:] = allowed
            yield Path(parent), dirs, files, fd
    except FileNotFoundError as exc:
        if exc.filename != str(base) and exc.filename != base:
            errors.append(str(exc))
    except (OSError, ValueError) as exc:
        errors.append(str(exc))


def thread_index(root, *, workspace_records=None):
    """Read state and optional display names, never message columns."""
    dbs = [(int(m[1]), p) for p in root.glob('state_*.sqlite') if (m := re.fullmatch(r'state_(\d+)\.sqlite', p.name))]
    if not dbs:
        return {}, 'files'
    path = max(dbs)[1]
    if path.is_symlink() or not path.is_file():
        raise ValueError('Codex 索引路径异常')
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(path) + suffix).is_symlink():
            raise ValueError('Codex 索引附属文件路径异常')
    # When WAL sidecars do not exist, do not let SQLite create them for this report.
    wal, shm = [Path(str(path) + s) for s in ('-wal', '-shm')]
    if Path(str(path) + '-journal').exists() or wal.exists() != shm.exists():
        raise ValueError('Codex 索引正在切换状态，请重新盘点')
    no_wal = not wal.exists()
    before = path.stat()
    uri = path.as_uri() + ('?mode=ro&immutable=1' if no_wal else '?mode=ro')
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        cols = {r[1] for r in db.execute('pragma table_info(threads)')}
        if not {'id', 'archived'} <= cols:
            raise ValueError('未识别的 Codex 会话索引')
        clocks = {'updated_at_ms': 10**6, 'recency_at_ms': 10**6, 'updated_at': 10**9,
                  'recency_at': 10**9, 'archived_at': 10**9}
        keys = [k for k in clocks if k in cols]
        labels = [f'substr({k},1,4096)' if k in cols else 'NULL' for k in ('name', 'title', 'summary')]
        locations = [f'substr({k},1,8192)' if k in cols else 'NULL' for k in ('cwd', 'rollout_path')]
        fields = ['id', 'archived', 'is_pinned' if 'is_pinned' in cols else '0', *labels, *locations, *keys]
        result = {}
        for sid, archived, pinned, name, title, summary, cwd, rollout, *times in db.execute('select ' + ','.join(fields) + ' from threads'):
            if isinstance(sid, str):
                latest = max([0] + [int(t * clocks[k]) for k, t in zip(keys, times) if isinstance(t, (int, float))])
                result[sid.lower()] = (archived, bool(pinned), latest, label(name), label(title) or label(summary))
                if workspace_records is not None and isinstance(cwd, str) and Path(cwd).is_absolute():
                    if isinstance(rollout, str):
                        file = Path(rollout)
                        if any(base in file.parents for base in (root/'sessions', root/'archived_sessions')):
                            try:
                                s = file.lstat()
                                if stat.S_ISREG(s.st_mode) and file.resolve() == file:
                                    latest = max(latest, s.st_mtime_ns)
                            except FileNotFoundError:
                                pass
                    workspace_records.append((Path(cwd), latest, 'Codex 会话索引 / 会话文件'))
    if no_wal and (wal.exists() or Path(str(path) + '-journal').exists() or path.stat().st_mtime_ns != before.st_mtime_ns):
        raise ValueError('Codex 索引已更新，请重新盘点')
    return result, 'index+files'


def plan(rows, archive_days, delete_days, limit):
    actions = {k: dict(count=0, bytes=0, entries=[]) for k in ('archive', 'delete')}
    for row in rows:  # oldest first; count all matches before applying display limit
        kind, age = row['route'], row['idle_days']
        if kind in ('delete', 'review') and age >= delete_days:
            act = actions['delete']
        elif kind in ('archive', 'review') and age >= archive_days:
            act = actions['archive']
        else:
            continue
        act['count'] += 1
        act['bytes'] += row['bytes']
        if len(act['entries']) < limit:
            act['entries'].append(row)
    return actions


def collect(agent, root, policy, rules, now, full, limit, progress=None):
    from .activity import classify
    errors = []
    groups = {k: [] for k in (SECTIONS if agent == 'codex' else ('sessions',))}
    sessions = {}
    aux = {}
    titles = {}
    def track(path, fd, section, project_index=None):
        name = path.name
        match = UUID_SUFFIX.search(name)
        s = stat_at(name, fd)
        if not stat.S_ISREG(s.st_mode):
            return
        sid = match[1].lower() if match else None
        size = allocated(path, s)
        groups[section].append(dict(path=str(path), bytes=size, session_id=sid))
        if sid is None:
            return
        if project_index is not None:
            title = claude_title(name, fd, sid, project_index.get(sid, (0, '')))
            if title:
                titles[sid] = title
        row = sessions.setdefault(sid, dict(session_id=sid, bytes=0, latest=0, states=set(), protected=False))
        row['bytes'] += size
        row['latest'] = max(row['latest'], s.st_mtime_ns)
        row['states'].add(section)
        rel = path.relative_to(root)
        row['protected'] |= policy.protected(agent, rel) or (agent == 'claude' and policy.protected(agent, rel.with_suffix(''), tree=True))
    index = {}
    source = 'files'
    try:
        if root.resolve() != root:
            raise ValueError(f'跳过含符号链接的数据目录：{root}')
        if agent == 'codex':
            if progress:
                progress('读取会话索引', detail='Codex')
            titles = codex_titles(root)
            for sec in ('sessions', 'archived_sessions'):
                if progress:
                    progress('盘点会话文件', detail=f'Codex/{sec}')
                for parent, _, files, fd in walk(root / sec, errors):
                    for name in files:
                        if ARCHIVE.fullmatch(name):
                            track(parent / name, fd, sec)
            for parent, dirs, _, _ in walk(root / 'worktrees', errors):
                for d in counted(dirs, progress if full else None, '统计工作树占用'):
                    row = dict(path=str(parent / d), bytes=None)
                    groups['worktrees'].append(row)
                    if full:
                        try:
                            row['bytes'] = usage(parent / d)
                        except (OSError, ValueError) as e:
                            errors.append(str(e))
                break
            if progress:
                progress('核对会话活动', detail='Codex')
            index, source = thread_index(root)
        else:
            if progress:
                progress('盘点会话文件', detail='Claude Code')
            base = root / 'projects'
            for parent, dirs, files, fd in walk(base, errors):
                parts = parent.relative_to(base).parts
                if len(parts) == 1:
                    project_index = claude_index(fd) if 'sessions-index.json' in files else {}
                    dirs[:] = [d for d in dirs if re.fullmatch(UUID, d, re.I)]
                    for name in files:
                        if re.fullmatch(UUID + r'\.jsonl', name, re.I):
                            track(parent / name, fd, 'sessions', project_index)
                elif len(parts) >= 2:  # Child agent/tool activity belongs to the parent, never a separate chat.
                    sid = parts[1].lower()
                    newest = max([(fd.stat() if isinstance(fd, Path) else os.fstat(fd)).st_mtime_ns] + [stat_at(n, fd).st_mtime_ns for n in files])
                    aux[sid] = max(aux.get(sid, 0), newest)
    except (ValueError, OSError, sqlite3.Error) as exc:
        errors.append(str(exc))

    rows = []
    histogram = [0] * len(BUCKETS)
    for sid, raw in sessions.items():
        archived = raw['states'] == {'archived_sessions'}
        uncertain = len(raw['states']) != 1 or (source == 'index+files' and sid not in index)
        latest = max(raw['latest'], aux.get(sid, 0))
        pinned = False
        title = titles.get(sid, '')
        if sid in index:
            idx_archived, pinned, stamp, name, fallback = index[sid]
            title = name or title or fallback
            uncertain |= idx_archived not in (0, 1) or archived != bool(idx_archived)
            latest = max(latest, stamp)
        age = max(0, (now - latest) // DAY_NS)
        histogram[sum(age >= threshold for threshold in SESSION_DAYS)] += 1
        route = 'review' if agent == 'claude' else 'delete' if archived else 'archive'
        held = ('盘点不完整' if errors else '已置顶' if pinned else '命中保护路径' if raw['protected']
                else '会话状态未核实' if uncertain else '活动时间异常' if latest > now
                else '当前运行的会话' if sid == os.environ.get('CODEX_THREAD_ID') and agent == 'codex' else None)
        if held:
            route = None
        rows.append(dict(session_id=sid, title=title or None, idle_days=age, bytes=raw['bytes'], route=route,
                         latest_ns=latest, hold_reason=held,
                         activity=classify(latest, now_ns=now, complete=not errors,
                             running=held == '当前运行的会话',
                             basis='会话索引 / 文件最近修改' if source == 'index+files' else '会话文件 / 子代理文件最近修改',
                             warning='会话盘点不完整' if errors else '活动时间异常' if latest > now else None),
                         state='unverified' if uncertain else 'unknown' if agent == 'claude' else 'archived' if archived else 'open'))
    rows.sort(key=lambda r: (-r['idle_days'], -r['bytes'], r['session_id']))
    summary = {}
    for name, items in groups.items():
        items.sort(key=lambda r: (-(r['bytes'] or 0), r['path']))
        total = None if (name == 'worktrees' and not full) or any(r['bytes'] is None for r in items) else sum(r['bytes'] for r in items)
        summary[name] = dict(count=len(items), bytes=total, entries=items[:limit])
    return dict(root=str(root), groups=summary, session_count=len(rows), session_bytes=sum(r['bytes'] for r in rows),
                held=sum(r['route'] is None for r in rows), activity_source=source,
                histogram=dict(zip(BUCKETS, histogram)),
                activity_levels={k: sum(r['activity']['level'] == k for r in rows)
                                 for k in ('high','medium','low','very_low','unknown')},
                policy=rules, recommendations=plan(rows, **rules, limit=limit),
                errors=errors, guidance=MANAGEMENT if agent == 'codex' else {'review': CLAUDE_GUIDE}), rows


def inspect(roots, policy, *, agent='all', archive_days=None, delete_days=None, full=False, limit=10, now_ns=None,
            progress=None):
    now = time.time_ns() if now_ns is None else now_ns
    doc = dict(mode='inventory', generated_at=datetime.fromtimestamp(now / 10**9, timezone.utc).isoformat(timespec='seconds'),
               full=full, limit=limit, agents={}, errors=[])
    all_rows = {}
    for key in AGENTS if agent == 'all' else (agent,):
        rules = dict(policy.sessions[key])
        if archive_days is not None:
            rules['archive_days'] = archive_days
        if delete_days is not None:
            rules['delete_days'] = delete_days
        data, rows = collect(key, roots[key], policy, rules, now, full, limit, progress)
        doc['agents'][key] = data
        doc['errors'].extend(data['errors'])
        all_rows[key] = rows
    if 'codex' in doc['agents']:  # Original Codex-only JSON fields remain available for old consumers.
        doc.update({k: doc['agents']['codex'][k] for k in ('root', 'groups', 'guidance')})
    return doc, all_rows
