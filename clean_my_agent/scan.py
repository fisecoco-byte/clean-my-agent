"""Read file metadata only. A rule must explicitly accept a file before it can move."""
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

LABELS = {
    'cache': '缓存', 'logs': '诊断日志', 'snapshots': '终端快照',
    'archives': '已归档会话', 'tool-results': '历史工具输出',
    'sessions': '会话与项目记录', 'database': '数据库', 'worktrees': 'Git 工作树', 'codex-projects': 'Codex 项目目录',
    'dependencies': '项目依赖', 'plugins': '插件和技能', 'temporary': '未分类临时数据', 'other': '其他受保护数据',
}
QUICK = {
    'codex': {'cache', 'log', 'shell_snapshots', 'archived_sessions', 'sessions'},
    'claude': {'cache', 'debug', 'shell-snapshots', 'projects'},
}
ARCHIVE = re.compile(r'rollout-[\w.-]+\.jsonl(?:\.zst)?\Z')
DB = re.compile(r'.*\.(sqlite3?|db)(?:-(wal|shm|journal))?\Z')


def setting(name, default):
    val = Path(os.environ.get(name) or default).expanduser()
    if not val.is_absolute() or '..' in val.parts:
        raise ValueError(f'{name} 需要不含 .. 的绝对路径')
    return val


def layout():
    home = Path.home()
    cache = home / 'Library/Caches' if sys.platform == 'darwin' else setting('XDG_CACHE_HOME', home / '.cache')
    roots = {'codex': setting('CODEX_HOME', home / '.codex'),
             'claude': setting('CLAUDE_CONFIG_DIR', home / '.claude'),
             't3': setting('T3_HOME', home / '.t3'),
             'claude-cli': cache / 'claude-cli-nodejs'}
    state = setting('XDG_STATE_HOME', home / '.local/state') / 'clean-my-agent'
    paths = [*roots.values(), state]
    for i, p in enumerate(paths):
        if p == home or p in home.parents or any(q == p or q in p.parents or p in q.parents for q in paths[:i]):
            raise ValueError('数据目录不能是主目录、根目录或相互包含；请检查环境变量')
    return roots, state


def classify(source, rel):
    parts = rel.parts
    top = parts[0]
    name = parts[-1]
    if source in ('codex', 'claude', 't3') and top == 'worktrees':
        return 'worktrees'
    if source == 'claude-cli':
        if len(parts) >= 3 and any(p.startswith('mcp-logs-') for p in parts[:-1]) and name.endswith('.jsonl'):
            return 'logs'
        return 'other'
    if top == 'cache':
        if source == 'claude' and len(parts) == 2 and name in ('changelog.md', 'my-closed-issues.json'):
            return 'cache'
        if (source == 'codex' and len(parts) == 3 and name.endswith('.json')
                and parts[1] in {'codex_apps_tools', 'remote_plugin_catalog', 'bundled_plugin_exclusions',
                                 'codex_app_directory', 'codex_apps_server_info'}):
            return 'cache'
    if source == 'codex':
        if len(parts) == 2 and top == 'log' and re.fullmatch(r'.+\.log(?:\.\d+(?:\.gz)?)?', name):
            return 'logs'
        if len(parts) == 2 and top == 'shell_snapshots' and name.endswith(('.sh', '.zsh', '.bash', '.fish')):
            return 'snapshots'
        if top == 'archived_sessions' and len(parts) == 2 and ARCHIVE.fullmatch(name):
            return 'archives'
        if top == 'worktrees': return 'worktrees'
        if top == '.chatgpt-projects': return 'codex-projects'
        if top in ('tmp', '.tmp'): return 'temporary'
    if source == 'claude':
        if len(parts) == 2 and top == 'debug' and name.endswith(('.txt', '.log')):
            return 'logs'
        if len(parts) == 2 and top == 'shell-snapshots' and name.endswith(('.sh', '.zsh', '.bash', '.fish')):
            return 'snapshots'
        if len(parts) == 5 and top == 'projects' and parts[-2] == 'tool-results' and name.endswith(('.txt', '.json')):
            return 'tool-results'
    if top in ('sessions', 'projects', 'archived_sessions', 'history.jsonl', 'file-history'):
        return 'sessions'
    if DB.fullmatch(name): return 'database'
    if top in ('plugins', 'skills'): return 'plugins'
    return 'other'


def fingerprint(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)


def disk_bytes(s):
    return s.st_blocks * 512 if hasattr(s, 'st_blocks') else s.st_size


def agent(source):
    return 'claude' if source == 'claude-cli' else source


@dataclass
class Scan:
    rows: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0, 0, 0]))
    items: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    deferred: list = field(default_factory=list)
    folders: dict = field(default_factory=dict)
    kept: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0]))


def scan(roots, selected, policy, *, full=False, progress=None):
    if os.name == 'nt':
        from .portable import scan as windows_scan
        return windows_scan(roots, selected, policy, full=full, progress=progress)
    out = Scan()
    now_ns = time.time_ns()
    for source, root in roots.items():
        folded = []
        if agent(source) not in selected:
            continue
        if progress:
            progress('读取文件', detail=source)
        # fwalk opens children relative to the current fd, without following symlinks.
        if root.resolve(strict=False) != root:
            out.errors.append(f'跳过含符号链接的路径：{root}')
            continue
        try:
            desc = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            out.missing.append(str(root))
            continue
        except OSError as exc:
            out.errors.append(str(exc))
            continue
        try:
            rootdev = os.fstat(desc).st_dev
            for base, dirs, files, fd in os.fwalk('.', dir_fd=desc, follow_symlinks=False, onerror=lambda e: out.errors.append(str(e))):
                parent = Path(base)
                if progress and len(parent.parts) == 1:
                    progress('读取文件', detail=f'{source}/{parent.name}')
                for d in list(dirs):
                    try:
                        s = os.stat(d, dir_fd=fd, follow_symlinks=False)
                        if not stat.S_ISDIR(s.st_mode) or s.st_dev != rootdev:
                            dirs.remove(d)
                        elif source in QUICK and base == '.' and d not in QUICK[source]:
                            dirs.remove(d)
                            if full:
                                folded.append(root / d)
                            else:
                                out.deferred.append(str(root / d))
                    except OSError as e:
                        dirs.remove(d)
                        out.errors.append(str(e))
                for leaf in files:
                    rel = parent / leaf
                    try:
                        s = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                    except OSError as e:
                        out.errors.append(str(e))
                        continue
                    if not stat.S_ISREG(s.st_mode) or s.st_dev != rootdev:
                        continue
                    category = classify(source, rel)
                    row = out.rows[(agent(source), category)]
                    size = disk_bytes(s)
                    group = str(root / rel.parts[0]) if len(rel.parts) > 1 else str(root / "(根目录文件)")
                    out.folders[group] = out.folders.get(group, 0) + size
                    row[0] += 1
                    row[1] += size
                    reason = policy.explain(source, rel, category, s, now_ns)
                    if reason:
                        out.kept[reason][0] += 1
                        out.kept[reason][1] += size
                    else:
                        row[2] += 1
                        row[3] += size
                        out.items.append(dict(source=source, root=str(root), path=str(rel),
                                              category=category, size=size, stamp=fingerprint(s)))
        finally:
            os.close(desc)
        if folded:
            # Keep one du process so hard links shared across top-level folders
            # retain the same accounting. Count each result as du emits it.
            by_name = {os.fsencode(p): p for p in folded}
            done = 0
            cmd = ['du', '-k', '-s', '-x', *map(str, folded)]
            env = None
            if progress:
                progress('统计目录占用', done, len(folded), source)
                # Flush after each directory even when du writes into a pipe.
                if sys.platform == 'darwin':
                    env = {**os.environ, 'STDBUF1': 'L'}
                elif shutil.which('stdbuf'):
                    cmd = ['stdbuf', '-oL', *cmd]
            with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
                try:
                    for line in proc.stdout:
                        total, _, name = line.rstrip(b'\n').partition(b'\t')
                        path = by_name.get(name)
                        if total.isdigit() and path is not None:
                            size = int(total) * 1024
                            out.folders[str(path)] = size
                            category = classify(source, Path(path.name) / '_')
                            row = out.rows[(agent(source), category)]
                            row[1] += size
                            done += 1
                            if progress:
                                progress('统计目录占用', done, len(folded), f'已统计 {source}/{path.name}')
                except BaseException:
                    proc.kill()
                    raise
            if proc.returncode or set(map(str, folded)) - out.folders.keys():
                out.errors.append(f'{root} 有目录无法完整统计；结果为部分占用')
    return out
