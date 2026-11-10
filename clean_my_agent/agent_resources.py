"""Exact Agent resource rules, shared by previews and quarantine revalidation."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import sys
import time

from . import deps, policy as policies
from .dependency_safety import ProcessSnapshot, regular_text
from .portable import allocated, linked, open_read
from .scan import fingerprint
from .storage import measure

FAMILIES = ('codex', 'claude', 'cursor', 'copilot', 't3', 'antigravity', 'filo', 'qoder',
            'opencode', 'chrome-devtools', 'agent-browser')
LEAVES = ('Cache', 'Code Cache', 'GPUCache', 'DawnGraphiteCache', 'DawnWebGPUCache')
VERSION = re.compile(r'(?:\d+(?:\.\d+)+[-\w.]*|local-[a-f0-9]{8,}-[-\w.]+)\Z')
PREFIXES = ('openai-bundled.staging-', 'marketplace-upgrade-', 'marketplace-add-')
FIELDS = ('directory_bytes', 'shared_bytes', 'known_shared_bytes', 'unknown_bytes',
          'reclaimable_bytes', 'reclaimable_min_bytes', 'reclaimable_max_bytes')


@dataclass(frozen=True)
class Rule:
    agent: str
    category: str
    root: Path
    mode: str = 'leaf'
    active: object = None


def children(root, errors):
    if not root.exists() and not root.is_symlink():
        return []
    try:
        if root.resolve() != root or not root.is_dir():
            raise ValueError('资源根含链接或不是目录')
        return sorted(root.iterdir())
    except (OSError, ValueError) as exc:
        errors.append(f'{root}: {exc}')
        return []


def rules(roots, errors):
    home = Path.home()
    result = []
    def add(agent, category, path, mode='leaf', active=None):
        result.append(Rule(agent, category, path, mode, active))
    daemon = roots['codex'] / 'packages/app-server-daemon'
    add('codex', 'agent-version', daemon/'releases', 'daemon', daemon/'current')
    for agent, path, launch in (
        ('claude', '.local/share/claude/versions', '.local/bin/claude'),
        ('cursor', '.local/share/cursor-agent/versions', '.local/bin/cursor-agent'),
        ('copilot', '.copilot/pkg/universal', '.local/bin/copilot')):
        add(agent, 'agent-version', home/path, 'versions', home/launch)
    add('codex', 'runtime', home/'.cache/codex-runtimes', 'runtime')
    for path in ('.tmp/bundled-marketplaces', '.tmp/marketplaces/.staging'):
        add('codex', 'marketplace-staging', roots['codex']/path, 'marketplace')
    # Existing undocumented staging locations remain inventory-only.
    for path in ('plugins/.remote-plugin-install-staging', 'plugins/.marketplace-plugin-source-staging'):
        add('codex', 'marketplace-staging', roots['codex']/path, 'review')
    add('opencode', 'desktop-cache', home/'.cache/opencode')
    add('agent-browser', 'agent-version', home/'.agent-browser/browsers', 'browser')
    add('cursor', 'logs', home/'.local/share/cursor-agent', 'logs')
    for agent, profile in (
        ('antigravity', home/'.gemini/antigravity-browser-profile'),
        ('chrome-devtools', home/'.cache/chrome-devtools-mcp/chrome-profile')):
        for leaf in (*LEAVES, 'DawnCache', 'GrShaderCache', 'GraphiteDawnCache', 'Service Worker/CacheStorage'):
            add(agent, 'desktop-cache', profile/'Default'/leaf)
        for leaf in ('GraphiteDawnCache', 'component_crx_cache', 'extensions_crx_cache'):
            add(agent, 'desktop-cache', profile/leaf)
    if sys.platform == 'darwin':
        support = home/'Library/Application Support'
        cache = home/'Library/Caches/Codex'
        for profile in ('Default', 'Default/Partitions/codex-browser-app', 'codex-browser-app'):
            for leaf in ('Cache', 'Code Cache'):
                add('codex', 'desktop-cache', cache/profile/leaf)
        add('codex', 'update-staging', home/'Library/Caches/com.openai.codex/org.sparkle-project.Sparkle/Installation', 'sparkle')
        add('codex', 'crash-reports', support/'Codex/Crashpad/pending', 'pending')
        for agent, folder in (('claude','Claude'), ('antigravity','Antigravity'), ('filo','Filo/production'), ('qoder','Qoder')):
            for leaf in LEAVES:
                add(agent, 'desktop-cache', support/folder/leaf)
        add('claude', 'desktop-cache', support/'Claude/sentry')
        for leaf in ('CachedData', 'CachedExtensionVSIXs', 'logs'):
            add('qoder', 'logs' if leaf=='logs' else 'desktop-cache', support/'Qoder'/leaf)
        for leaf in ('claude-code', 'claude-code-vm'):
            add('claude', 'agent-version', support/'Claude'/leaf, 'sdk', support/'Claude/claude-code-vm/.sdk-version')
        for folder in ('t3code-v2', 'T3 Code (Alpha)'):
            for partition in children(support/folder/'Partitions', errors):
                if partition.name.startswith('t3code-preview-'):
                    for leaf in ('Cache','Code Cache'):
                        add('t3', 'desktop-cache', partition/leaf)
    return result


def paths(rule, errors, include=None):
    if rule.mode in ('leaf', 'file', 'review'):
        return [rule.root] if rule.root.exists() or rule.root.is_symlink() or rule.root == include else []
    if rule.mode == 'logs':
        result = []
        if rule.root.is_dir() and rule.root.resolve() == rule.root:
            for base, dirs, files in os.walk(rule.root, followlinks=False, onerror=lambda e:errors.append(str(e))):
                dirs[:] = [d for d in dirs if d != 'versions' and not (Path(base)/d).is_symlink()]
                result.extend(Path(base)/name for name in files if name.endswith('.log'))
        if include is not None and rule.root in include.parents and include.name.endswith('.log') and 'versions' not in include.relative_to(rule.root).parts:
            result.append(include)
        return sorted(set(result))
    result = children(rule.root, errors)
    if include is not None and include.parent == rule.root and include not in result:
        result.append(include)
    return [p for p in result if not p.name.startswith('.')]


def version_keep(rule):
    entries = children(rule.root, [])
    if rule.mode == 'sdk':
        name = regular_text(rule.active, 4096)[0].strip()
        if not VERSION.fullmatch(name):
            raise ValueError('桌面 SDK 活动版本无法确认')
        active = rule.root/name
        if not active.exists() or active.is_symlink():
            raise ValueError('桌面 SDK 活动版本不存在')
    else:
        if not rule.active.is_symlink():
            raise ValueError('活动启动链接不存在或无法确认；所有版本保留')
        target = rule.active.resolve(strict=True)
        active = next((p for p in entries if p == target or p in target.parents), None)
        if active is None:
            raise ValueError('活动启动链接不指向已知版本')
    keep = {active}
    if rule.mode == 'daemon':
        marker = rule.root.parent/'auto-update-version'
        if marker.exists() or marker.is_symlink():
            name = regular_text(marker, 4096)[0].strip()
            if not VERSION.fullmatch(name) or not (rule.root/name).exists():
                raise ValueError('daemon 更新标记无法确认；保留')
            keep.add(rule.root/name)
    previous = [p for p in entries if p not in keep and not p.is_symlink() and VERSION.fullmatch(p.name)]
    previous.sort(key=lambda p:p.lstat().st_mtime_ns, reverse=True)
    keep.update(previous[:1])
    return keep, active


def browser_keep(rule, include=None):
    entries = [p for p in children(rule.root, []) if re.fullmatch(r'chrome-\d+(?:\.\d+)+', p.name)]
    def chrome(p):
        return any((p/name).is_file() for name in ('Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
            'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
            'chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing', 'chrome-linux64/chrome', 'chrome-win64/chrome.exe'))
    complete = [p for p in entries if chrome(p)]
    if not complete:
        raise ValueError('未识别完整 Chrome 安装；保留')
    keep = {max(complete, key=lambda p:p.name)} | (set(entries)-set(complete))
    config = rule.root.parent/'config.json'
    text = regular_text(config)[0] if config.exists() or config.is_symlink() else ''
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    pins = [os.environ.get('AGENT_BROWSER_EXECUTABLE_PATH', ''), *strings(json.loads(text) if text else {})]
    # The original path may be absent after staging, but a new pin must still
    # prevent purging its quarantined copy.
    for p in set(entries) | ({include} if include is not None else set()):
        if p.name in text or any(pin and (p == Path(pin).resolve() or p in Path(pin).resolve().parents) for pin in pins):
            keep.add(p)
    return keep


def app_build(path):
    try:
        fd = open_read(path)
        with os.fdopen(fd, 'rb') as stream:
            if os.fstat(stream.fileno()).st_size > 1024*1024:
                return None
            data = plistlib.load(stream)
            value = str(data.get('CFBundleVersion', '')) if isinstance(data, dict) else ''
        return int(value) if value.isdecimal() else None
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def newer_update(path):
    installed = [app_build(p/'Contents/Info.plist') for p in
                 (Path('/Applications/Codex.app'), Path.home()/'Applications/Codex.app')]
    installed = [v for v in installed if v is not None]
    if not installed:
        return False
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base)/d).is_symlink()]
        if len(Path(base).relative_to(path).parts) >= 6:
            dirs.clear()
        if Path(base).name == 'Contents' and Path(base).parent.name == 'Codex.app' and 'Info.plist' in files:
            staged = app_build(Path(base)/'Info.plist')
            # A user can have both system and per-user installations. An
            # update newer than either one may still be needed by that copy.
            if staged is not None and staged > min(installed):
                return True
    return False


def semantics(rule, original, physical):
    if rule.mode == 'review':
        raise ValueError('此安装暂存格式尚未被上游规则确认；仅盘点')
    if rule.mode in ('versions', 'daemon', 'sdk'):
        if not VERSION.fullmatch(original.name):
            raise ValueError('版本名称无法验证；保留')
        keep, active = version_keep(rule)
        if original in keep:
            raise ValueError('活动版本或保留的上一版本')
    elif rule.mode == 'browser':
        if not re.fullmatch(r'chrome-\d+(?:\.\d+)+', original.name) or original in browser_keep(rule, include=original):
            raise ValueError('活动、配置引用或未完成的 Chrome 安装；保留')
    elif rule.mode == 'runtime':
        active = (physical/'runtime.json').exists() and any((physical/'dependencies'/p).is_dir() for p in ('node','python'))
        leftover = re.match(r'^(tmp|temp|incomplete|partial)', original.name) or original.name.endswith(('.tmp','.incomplete','-incomplete','.partial'))
        if active or (not leftover and ((physical/'runtime.json').exists() or (physical/'dependencies').exists())):
            raise ValueError('完整或用途未确认的运行环境；保留')
    elif rule.mode == 'marketplace' and not original.name.startswith(PREFIXES):
        raise ValueError('完成的市场或非 staging 项；保留')
    elif rule.mode == 'sparkle' and newer_update(physical):
        raise ValueError('暂存更新比当前安装版本新；保留')
    if rule.mode in ('versions', 'daemon', 'sdk', 'browser', 'runtime', 'marketplace', 'sparkle') and not physical.is_dir():
        if rule.mode not in ('versions', 'sdk'):
            raise ValueError('不是预期的资源目录')
    if rule.mode in ('pending', 'file', 'logs') and not physical.is_file():
        raise ValueError('不是普通诊断文件')


def running(rule, processes, physical):
    if processes.error:
        raise ValueError(processes.error)
    names = {
        'codex': r'(?i)(?:/Codex\.app/|(?:^|[/\s])codex(?:[\s/]|$)|sparkle|crashpad_handler)',
        'claude': r'(?i)(?:/Claude\.app/|(?:^|[/\s])claude(?:[\s/]|$))',
        'cursor': r'(?i)(?:cursor-agent|/Cursor\.app/)', 'copilot': r'(?i)(?:^|[/\s])copilot(?:[\s/]|$)',
        't3': r'(?i)(?:T3 Code|t3code)', 'antigravity': r'(?i)(?:antigravity|(?:^|[/\s])gemini(?:[\s/]|$))',
        'filo': r'(?i)/Filo\.app/', 'qoder': r'(?i)(?:/Qoder\.app/|(?:^|[/\s])qoder(?:[\s/]|$))',
        'opencode': r'(?i)(?:^|[/\s])opencode(?:[\s/]|$)', 'chrome-devtools': r'chrome-devtools-mcp',
        'agent-browser': r'(?:agent-browser|[.]agent-browser/browsers/)',
    }
    for pid, command in processes.commands:
        if rule.mode not in ('versions', 'daemon') and re.search(names[rule.agent], command):
            raise ValueError(f'{rule.agent} 或更新/上传进程正在运行（{pid}）；保留')
    result = processes.check(physical, project=rule.root if rule.mode not in ('leaf','file') else None)
    if result['state'] != 'idle':
        raise ValueError(result['reason'])
    return result


def check_policy(rule, original, physical, roots, policy, newest):
    if os.name == 'nt':
        raise ValueError('Windows 原生版仅盘点')
    if original.resolve() != original or physical.resolve() != physical or linked(physical.lstat()):
        raise ValueError('路径含链接或重解析点；保留')
    base = roots.get(rule.agent)
    relative = original.relative_to(base) if base is not None and base in original.parents else original.relative_to(Path.home())
    if policy.protected(rule.agent, relative, tree=physical.is_dir()):
        raise ValueError('命中保护路径')
    semantics(rule, original, physical)
    days = policy.retention['logs' if rule.category in ('logs', 'crash-reports') else 'cache']
    if rule.mode in ('pending', 'sparkle'):
        days = max(days, 30)
    if newest > time.time_ns()-days*policies.DAY_NS:
        raise ValueError(f'最近 {days} 天有改动；保留')
    if rule.mode == 'daemon':
        lock = rule.root.parent/'install.lock'
        if not lock.is_file() or lock.is_symlink():
            raise ValueError('daemon 安装锁不可核实；保留')


def inspect(roots, selected, *, full=True, progress=None, policy=None, only=None):
    policy = policy or policies.load()
    rows, errors = [], []
    all_agents = {'codex', 'claude'}.issubset(selected) or selected == set(FAMILIES)
    processes = ProcessSnapshot() if full and os.name != 'nt' else None
    for rule in rules(roots, errors):
        if not all_agents and rule.agent not in selected:
            continue
        for path in paths(rule, errors):
            if only is not None and path != only:
                continue
            r = dict.fromkeys(FIELDS)
            r.update(agent=rule.agent, category=rule.category, path=str(path), name=path.name,
                     label=f'{rule.agent} / {path.parent.name} / {path.name}',
                     version=path.name if rule.category=='agent-version' else None,
                     status='retained', reason='未测量', can_stage=False, referenced_by=[],
                     target=None, kind='directory', installation=None, runtime=None,
                     included_in_primary=any(p in path.parents for p in roots.values()), measurement='not-requested')
            rows.append(r)
            if progress:
                progress('盘点 Agent 资源', detail=f'{rule.agent} / {path.name}')
            try:
                info = path.lstat()
                r['stamp'] = fingerprint(info)
                r['kind'] = 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
                if linked(info) or path.resolve() != path:
                    r.update(measurement='link', kind='symlink', reason='链接仅盘点，不跟随或清理')
                    continue
                if rule.mode in ('versions','daemon','sdk'):
                    try:
                        _, active = version_keep(rule)
                        if path == active:
                            r['referenced_by'] = [str(rule.active)]
                    except (OSError, ValueError):
                        pass
                if not full:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    deps.check_mounts(path)
                    sizes, newest, nested = measure(path, on_entries=(lambda n: progress(
                        '测量 Agent 资源', detail=f'{rule.agent} / {path.name} · {n:,} 项')) if progress else None)
                    r.update(sizes)
                    if nested and rule.mode not in ('marketplace',):
                        raise ValueError('包含嵌套源码仓库；保留')
                elif stat.S_ISREG(info.st_mode):
                    newest = info.st_mtime_ns
                    size = allocated(path, info)
                    r.update(directory_bytes=size, unknown_bytes=size, known_shared_bytes=0,
                             reclaimable_min_bytes=0, reclaimable_max_bytes=size)
                    if info.st_nlink != 1:
                        raise ValueError('文件存在其他硬链接；保留')
                else:
                    raise ValueError('特殊文件；保留')
                r['measurement'] = 'measured'
                check_policy(rule, path, path, roots, policy, newest)
                r['runtime'] = running(rule, processes, path)
                r.update(status='candidate', can_stage=True, reason='符合资源规则；可暂存，暂存释放 0 B')
            except (OSError, ValueError, RuntimeError) as exc:
                r.update(status='retained', reason=str(exc), can_stage=False)
    if only is not None and not rows:
        raise ValueError('路径不是已识别的 Agent 资源')
    return dict(mode='measured' if full else 'references_only', entries=rows, errors=errors,
                external_targets={}, title='Agent 缓存 / 版本 / 残留',
                totals_note='目录与共享量不能相加；暂存释放 0 B。',
                scope='固定已知路径；活动版本和状态数据保留；额外资源与主目录不重复相加')


def resolve(item, roots):
    home = Path.home()
    relative = Path(item['path'])
    allowed = {str(home), str(roots.get(item['source'], home))}
    if item['root'] not in allowed or relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('资源原路径无效')
    original = Path(item['root'])/relative
    errors = []
    for rule in rules(roots, errors):
        if rule.agent == item['source'] and original in paths(rule, errors, include=original):
            return rule, original
    raise ValueError('资源路径已不在已知规则中')


@contextmanager
def guarded(item, roots, policy, physical=None):
    """Keep the daemon installer lock through rename/unlink, not only the probe."""
    rule, original = resolve(item, roots)
    physical = physical or original
    fd = None
    try:
        if rule.mode == 'daemon':
            import fcntl
            fd = os.open(rule.root.parent/'install.lock', os.O_RDWR | os.O_NOFOLLOW)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = physical.lstat()
        if stat.S_ISDIR(info.st_mode):
            deps.check_mounts(physical)
            _, newest, nested = measure(physical)
            if nested and rule.mode != 'marketplace':
                raise ValueError('包含嵌套源码仓库；保留')
        else:
            newest = info.st_mtime_ns
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('不是独占普通文件')
        check_policy(rule, original, physical, roots, policy, newest)
        running(rule, ProcessSnapshot(), physical)
        policy.require_current()
        yield
    finally:
        if fd is not None:
            os.close(fd)
