"""Resolve the same cleanup rules for previews, quarantine and protection on purge."""
import json
import os
from dataclasses import dataclass
from pathlib import Path
import stat
import tempfile
try:
    import fcntl
except ImportError:
    fcntl = None
import unicodedata
from typing import Optional

CLEANABLE_FILES = frozenset({'cache', 'logs'})
# These v1 categories can be restored from old batches, never staged or purged.
RETIRED = frozenset({'snapshots', 'archives', 'tool-results'})
CATEGORIES = ('cache', 'logs', 'dependencies')
PROFILES = {
    'conservative': (60, 90, 30),
    'standard': (30, 30, 7),
    'deep': (7, 14, 7),
}
REASONS = {'protected': '命中保护路径', 'out-of-scope': '仅盘点',
           'linked': '有其他硬链接', 'recent': '在保留期内'}
DAY_NS = 86400 * 10**9
SESSION_DAYS = (5, 10, 15, 20)
AGENTS = ('codex', 'claude')


def session_rules(raw):
    if not isinstance(raw, dict) or raw.keys() - set(AGENTS):
        raise ValueError('sessions 只接受 codex 和 claude')
    result = {}
    for agent in AGENTS:
        values = raw.get(agent, {})
        rules = {'archive_days': 10, 'delete_days': 20}
        if not isinstance(values, dict) or values.keys() - rules.keys():
            raise ValueError('会话策略只接受 archive_days 和 delete_days')
        for key, value in values.items():
            if type(value) is not int or value not in SESSION_DAYS:
                raise ValueError('会话策略天数请选择 5、10、15 或 20')
            rules[key] = value
        result[agent] = rules
    return result


EXAMPLE = {'sessions': session_rules({}), 'profile': 'standard', 'retention_days': {'logs': 60},
           'protect': ['codex:worktrees/keep-project', 'claude:projects/keep-project']}


def config_path():
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config').expanduser()
    if not base.is_absolute() or '..' in base.parts:
        raise ValueError('XDG_CONFIG_HOME 必须是不含 .. 的绝对路径')
    return base / 'clean-my-agent/policy.json'


def unique_keys(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError(f'重复的配置项：{k}')
        result[k] = v
    return result


def read_config(path):
    # A dangling link is an unreadable policy, not an absent one.
    try:
        info = path.lstat()
    except FileNotFoundError:
        for p in path.parents:
            if p.is_symlink() and not p.exists():
                raise ValueError(f'策略配置目录链接失效：{p}')
        return {}
    if not stat.S_ISREG(info.st_mode) and not (stat.S_ISLNK(info.st_mode) and path.is_file()):
        raise ValueError('需要可读取的普通 JSON 文件')
    with path.open(encoding='utf-8') as stream:
        data = json.load(stream, object_pairs_hook=unique_keys)
    if not isinstance(data, dict) or data.keys() - {'profile', 'retention_days', 'protect', 'sessions'}:
        raise ValueError('仅支持 profile、retention_days、protect 和 sessions 配置项')
    return data


def valid_days(value):
    if type(value) is not int or value < 1:
        raise ValueError('保留天数必须是正整数')
    return value


def components(rel):
    # Conservative matching even on case-insensitive and Unicode-normalizing volumes.
    return tuple(unicodedata.normalize('NFC', p).casefold() for p in Path(rel).parts)


def parse_protect(entries):
    if not isinstance(entries, list):
        raise ValueError('protect 必须是路径数组')
    rules = []
    for entry in entries:
        if not isinstance(entry, str):
            raise ValueError('保护路径必须是字符串')
        source, _, rel = entry.partition(':')
        if (source not in ('codex', 'claude', 'claude-cli', 't3', 'cursor', 'copilot', 'antigravity',
                          'filo', 'qoder', 'opencode', 'chrome-devtools', 'agent-browser') or not rel or Path(rel).is_absolute()
                or '..' in Path(rel).parts or any(ch in rel for ch in '*?[]\0')):
            raise ValueError(f'无效保护路径 {entry!r}；示例 codex:worktrees/demo（不支持通配符）')
        rules.append((source, components(rel)))
    return tuple(rules)


@dataclass
class Policy:
    profile: str
    retention: dict
    protect: tuple
    rules: tuple
    sessions: dict
    path: Path
    cli_profile: Optional[str]
    cli_days: Optional[int]

    def require_current(self):
        if self != load(self.cli_profile, self.cli_days):
            raise ValueError('扫描期间清理策略已改变；请按新策略重新预览')

    def protected(self, source, rel, tree=False):
        if not self.rules:
            return False
        parts = components(rel)
        return any(src == source and (parts[:len(rule)] == rule or (tree and rule[:len(parts)] == parts))
                   for src, rule in self.rules)

    def explain(self, source, rel, category, s, now_ns):
        if self.protected(source, rel):
            return 'protected'
        if category not in CLEANABLE_FILES:
            return 'out-of-scope'
        if s.st_nlink != 1:
            return 'linked'
        if s.st_mtime_ns > now_ns - self.retention[category] * DAY_NS:
            return 'recent'
        return None

    def describe(self):
        return dict(profile=self.profile, retention_days=self.retention, protect=list(self.protect),
                    config_path=str(self.path), sessions=self.sessions)


def load(profile=None, days=None):
    path = config_path()
    try:
        data = read_config(path)
        selected = data.get('profile', 'standard')
        if not isinstance(selected, str) or selected not in PROFILES:
            raise ValueError('未知策略档位')
        overrides = data.get('retention_days', {})
        if not isinstance(overrides, dict) or overrides.keys() - set(CATEGORIES) - RETIRED:
            raise ValueError('retention_days 含未识别的类别或类型错误')
        entries = data.get('protect', [])
        rules = parse_protect(entries)
        name = profile or selected
        retention = dict(zip(CATEGORIES, PROFILES[name]))
        for k, v in overrides.items():
            days_value = valid_days(v)
            if k in CATEGORIES:
                retention[k] = days_value
        if days is not None:
            retention.update({k: valid_days(days) for k in CLEANABLE_FILES})
        return Policy(name, retention, tuple(entries), rules, session_rules(data.get('sessions', {})), path, profile, days)
    except (OSError, ValueError) as exc:
        raise ValueError(f'无法使用策略 {path}：{exc}') from exc


def save_session_defaults(agent, values):
    if os.name == 'nt':
        from .portable import READ_ONLY
        raise ValueError(READ_ONLY)
    """Change only explicit review preferences. Serialize CLI edits; replace atomically."""
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.resolve() != path.parent or path.is_symlink():
        raise ValueError('配置路径含符号链接，不能自动修改，请手动编辑')
    lock = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = path.stat() if path.exists() else None
        load()  # Reject invalid unrelated settings rather than overwriting them.
        data = read_config(path)
        sessions = data.setdefault('sessions', {})
        for key in AGENTS if agent == 'all' else (agent,):
            sessions.setdefault(key, {}).update(values)
        session_rules(sessions)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.policy-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(data, out, ensure_ascii=False, indent=2)
                out.write('\n')
                out.flush()
                os.fsync(out.fileno())
            stamp = lambda s: None if s is None else (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
            if stamp(before) != stamp(path.lstat() if path.exists() or path.is_symlink() else None):
                raise ValueError('配置文件已被修改，请重新执行')
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    finally:
        os.close(lock)
