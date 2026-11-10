"""Detect extension versions without executing skills, MCP servers or plugin code.

Installed extensions are not ordinary disposable caches. Suggestions never enter
the quarantine candidate set, even when an owner marks an old version orphaned.
"""
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .dependency_safety import regular_text
from .resources import children, measure_rows, row

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


def metadata(path, errors, *, toml=False):
    if not path.exists() and not path.is_symlink():
        return None
    try:
        text, _ = regular_text(path)
        if toml and tomllib is None:
            raise ValueError('TOML needs Python 3.11 or tomli')
        data = tomllib.loads(text) if toml else json.loads(text)
        if not isinstance(data, dict):
            raise ValueError('not an object')
        return data
    except (OSError, ValueError, UnicodeError, RuntimeError):
        errors.append(f'配置无法解析，相关版本保留（TOML 需 Python 3.11 或 tomli）：{path}')
        return None


def small_string(value):
    return value if isinstance(value, str) and len(value) <= 512 else None


def inspect(roots, selected, *, full=True, progress=None):
    rows, errors = [], []
    plugins, skills, configs, path_references = [], [], [], []
    # Look across both hosts for references even when only one is displayed.
    for agent in ('codex', 'claude'):
        root = roots[agent]
        registry_path = root / 'plugins/installed_plugins.json'
        registry = metadata(registry_path, errors)
        installed = registry.get('plugins', {}) if registry else {}
        if not isinstance(installed, dict):
            errors.append(f'插件登记格式未知：{registry_path}')
            installed = {}
        cfg = root / 'config.toml' if agent == 'codex' else Path.home() / '.claude.json'
        data = metadata(cfg, errors, toml=agent == 'codex')
        if data:
            configs.append((agent, cfg, data.get('mcp_servers' if agent == 'codex' else 'mcpServers', {})))
            # Claude records project-scoped MCP servers here as well.
            if agent == 'claude' and isinstance(data.get('projects'), dict):
                for project, settings in data['projects'].items():
                    if isinstance(settings, dict):
                        configs.append((agent, cfg, settings.get('mcpServers', {})))
            skill_settings = data.get('skills', {})
            for entry in skill_settings.get('config', []) if isinstance(skill_settings, dict) else []:
                if isinstance(entry, dict) and isinstance(entry.get('path'), str):
                    path_references.append((entry['path'], str(cfg) + ':skills'))
        for market in children(root / 'plugins/cache', errors):
            if market.is_symlink() or not market.is_dir():
                continue
            for component in children(market, errors):
                if component.is_symlink() or not component.is_dir():
                    continue
                plugin_id = component.name + '@' + market.name
                records = installed.get(plugin_id, [])
                if not isinstance(records, list):
                    records = []
                for version in children(component, errors):
                    if version.name.startswith('.') or not version.is_dir():
                        continue
                    manifest = None
                    if not version.is_symlink():
                        for folder in ('.codex-plugin', '.claude-plugin'):
                            manifest = metadata(version / folder / 'plugin.json', errors)
                            if manifest is not None:
                                break
                    r = row(agent, 'plugin', version)
                    r.update(name=component.name, identity=plugin_id, source=market.name,
                             version=version.name, manifest_version=small_string((manifest or {}).get('version')),
                             reason='活动版本无法完全确认；保留', group_key='plugin:' + plugin_id)
                    r['registry_verified'] = bool(registry and isinstance(registry.get('plugins'), dict))
                    for item in records:
                        if isinstance(item, dict) and item.get('installPath') == str(version):
                            r['referenced_by'].append(str(registry_path) + ':' + str(item.get('scope', 'unknown')))
                    if version.is_symlink():
                        r['reason'] = '开发目录链接；保留，不跟随清理'
                    elif not manifest:
                        r['reason'] = '缺少可识别的插件清单；保留'
                    if agent == 'codex' and isinstance((data or {}).get('plugins'), dict):
                        if plugin_id in data['plugins']:
                            r['referenced_by'].append(str(cfg) + ':plugins.' + plugin_id + '（未指定版本）')
                    orphan = version / '.orphaned_at'
                    r['guidance'] = '在所属 Agent 的插件管理中复核；同名或版本较旧本身不构成删除依据。'
                    try:
                        if agent == 'claude' and orphan.is_file() and not orphan.is_symlink():
                            r['orphaned_days'] = max(0, int((time.time() - orphan.stat().st_mtime) / 86400))
                            r['guidance'] = '检测到原生孤立标记；由 Claude Code 管理旧版本回收，不能用卸载整个插件代替。'
                    except OSError:
                        errors.append(f'无法读取原生孤立标记：{orphan}')
                    rows.append(r)
                    plugins.append((r, manifest or {}))
                    if not version.is_symlink():
                        skills.extend((agent, p, r) for p in skill_paths(version / 'skills', errors))
                        servers = (manifest or {}).get('mcpServers')
                        if isinstance(servers, dict):
                            bundle_mcp(rows, r, servers)
                        mcp = metadata(version / '.mcp.json', errors)
                        if mcp:
                            bundle_mcp(rows, r, mcp.get('mcpServers', mcp))
                        if isinstance(servers, str):
                            p = version / servers
                            if '..' not in p.parts and version in p.parents:
                                extra = metadata(p, errors)
                                if extra:
                                    bundle_mcp(rows, r, extra.get('mcpServers', extra))
        skills.extend((agent, p, None) for p in skill_paths(root / 'skills', errors))
    # Shared Codex skills are also used outside CODEX_HOME.
    skills.extend(('codex', p, None) for p in skill_paths(Path.home() / '.agents/skills', errors))
    for agent, path, parent in skills:
        r = row(agent, 'skill', path.parent)
        r.update(identity=path.parent.name, source=parent['identity'] if parent else '本地 Skill',
                 parent_path=parent['path'] if parent else None, measure=not bool(parent),
                 reason='本地 Skill 可能包含自定义修改；只做重复版本复核',
                 guidance='先核对来源与本地修改；链接不代表重复安装，不按目录名直接删除。')
        try:
            # Metadata only; the body is never executed or treated as instructions.
            real = path.resolve(strict=True)
            text, _ = regular_text(real, 1024 * 1024)
            header = text.split('---', 2)[1] if text.startswith('---\n') and text.count('---') >= 2 else ''
            for field in ('name', 'version'):
                hit = re.search(r'^' + field + r':\s*[\x22\x27]?([^\n\x22\x27]+)', header, re.M)
                if hit:
                    r[field] = hit[1].strip()[:512]
            r['content_fingerprint'] = hashlib.sha256(text.encode()).hexdigest()
            r['resolved_path'] = str(real.parent)
            if parent:
                r.update(version=r['version'] or parent['version'],
                         group_key='skill:' + parent['identity'] + ':' + r['name'],
                         reason='插件内置 Skill，随父插件版本管理；不单独删除')
            else:
                r['group_key'] = 'skill:local:' + r['name']
            for plugin, _ in plugins:
                target = Path(plugin['path'])
                if parent is None and (real.parent == target or target in real.parents):
                    plugin['referenced_by'].append(str(path.parent))
        except (OSError, ValueError, UnicodeError, RuntimeError):
            r.update(reason='Skill 元数据无法读取；保留', group_key='skill:unknown:' + str(path.parent))
        rows.append(r)
    for agent, cfg, servers in configs:
        if isinstance(servers, dict):
            for name, spec in servers.items():
                if isinstance(spec, dict):
                    configured_mcp(rows, agent, cfg, name, spec, errors)
                    args = spec.get('args', [])
                    for value in [spec.get('command'), *(args if isinstance(args, list) else [])]:
                        if isinstance(value, str) and value.startswith('/'):
                            path_references.append((value, str(cfg) + ':mcp:' + str(name)))
    for value, source in path_references:
        try:
            path = Path(value).resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        for plugin, _ in plugins:
            base = Path(plugin['path'])
            if path == base or base in path.parents:
                plugin['referenced_by'].append(source)
    groups = defaultdict(list)
    for r in rows:
        groups[r.get('group_key', r['path'])].append(r)
    summary = []
    for key, members in groups.items():
        versions = sorted({r['version'] for r in members if r.get('version')})
        physical = {r.get('resolved_path', r['path']) for r in members}
        if len(versions) > 1 or (len(physical) > 1 and len(members) > 1):
            category = members[0]['category']
            for r in members:
                r['multiple_versions'] = len(versions) > 1
                r['duplicate_group'] = key
            summary.append(dict(identity=key, category=category, versions=versions,
                                paths=sorted(physical), agents=sorted({r['agent'] for r in members}),
                                reason='多个已声明版本' if len(versions) > 1 else '同名多处安装；来源或版本可能不同'))
    for r, _ in plugins:
        if r['referenced_by']:
            r['reason'] = '安装登记、配置或 Skill 链接仍引用；保留'
        elif r.get('orphaned_days') is not None and r.get('registry_verified'):
            r['status'] = 'owner-managed'
            r['reason'] = '原生孤立标记已存在；等待所属 Agent 回收（标记文件年龄仅供参考）'
    # Deduplicate the same bundled MCP declared in both the manifest and .mcp.json.
    unique = {}
    for r in rows:
        if r['agent'] in selected:
            unique.setdefault((r['agent'], r['category'], r.get('group_key'), r['path'], r.get('version')), r)
    rows = list(unique.values())
    summary = [g for g in summary if any(a in selected for a in g['agents'])]
    measure_rows(rows, roots, full=full, progress=progress, errors=errors)
    return dict(mode='measured' if full else 'discovered', entries=rows, groups=summary, errors=errors,
                scope='用户级 Skill、插件缓存与已登记 MCP；未穷举系统配置、任意项目和 Python/uv 缓存',
                totals_note='只读版本复核；父插件已包含内置 Skill/MCP，不重复计量；不是清理候选。')


def skill_paths(root, errors):
    found = []
    for path in children(root, errors):
        if (path / 'SKILL.md').is_file():
            found.append(path / 'SKILL.md')
        elif path.is_dir() and not path.is_symlink() and not path.name.startswith('.'):
            # One nested version level; never traverse plugin code or dependencies.
            for version in children(path, errors):
                if (version / 'SKILL.md').is_file():
                    found.append(version / 'SKILL.md')
    return found


def bundle_mcp(rows, parent, servers):
    if not isinstance(servers, dict):
        return
    for name in servers:
        r = row(parent['agent'], 'mcp', Path(parent['path']))
        r.update(name=str(name), version=parent['version'], source=parent['identity'],
                 group_key='mcp:' + parent['identity'] + ':' + str(name),
                 parent_path=parent['path'], measure=False,
                 reason='插件内置 MCP，随父插件版本管理；不单独删除')
        rows.append(r)


PACKAGE = re.compile(r'(?P<name>@[a-z0-9_.-]+/[a-z0-9_.-]+|[a-z0-9][a-z0-9_.-]*)(?:@(?P<version>[a-zA-Z0-9.*^~+_-][a-zA-Z0-9.*^~+_-]*))?\Z')


def command_package(args):
    """Only known npx option syntax. Never guess that an unknown flag's value is a package."""
    remaining = iter(args)
    for arg in remaining:
        if not isinstance(arg, str):
            return None
        if arg in ('-y', '--yes', '--no', '--no-install', '--', 'exec'):
            continue
        if arg in ('--registry', '--cache', '--userconfig', '--prefix'):
            next(remaining, None)
            continue
        if arg in ('-p', '--package'):
            value = next(remaining, None)
            return PACKAGE.fullmatch(value) if isinstance(value, str) else None
        if arg.startswith('--package='):
            return PACKAGE.fullmatch(arg.split('=', 1)[1])
        if arg.startswith('-'):
            return None
        return PACKAGE.fullmatch(arg)
    return None


def configured_mcp(rows, agent, cfg, name, spec, errors):
    r = row(agent, 'mcp', cfg)
    r.update(name=str(name), identity=str(name), measure=False, source='MCP 配置',
             group_key=f'mcp:config:{agent}:{name}', referenced_by=[str(cfg)],
             reason='已配置的 MCP；未验证本地安装版本，不删除配置',
             guidance='远程服务不对应本地可清理版本；本地服务应先在所属 Agent 中复核。')
    if spec.get('url') or spec.get('type') in ('http', 'sse'):
        r['reason'] = '远程 MCP；无可确认的本地版本占用'
        rows.append(r)
        return
    command, args = spec.get('command'), spec.get('args', [])
    package_spec = None
    if isinstance(command, str) and Path(command).name in ('npx', 'npm', 'npx.cmd', 'npm.cmd') and isinstance(args, list):
        package_spec = command_package(args)
    rows.append(r)
    if package_spec is None:
        return
    package = package_spec['name']
    r.update(package=package, declared_version=package_spec['version'], group_key='mcp:npm:' + package,
             reason='配置声明的 npm 包版本或范围；不等于实际缓存版本')
    npm_root = Path(os.environ.get('npm_config_cache') or Path.home() / '.npm').expanduser()
    if not npm_root.is_absolute() or '..' in npm_root.parts:
        errors.append('npm 缓存路径无法验证，未展开 MCP 包缓存')
        return
    for cache in children(npm_root / '_npx', errors):
        p = cache / 'node_modules' / package
        data = metadata(p / 'package.json', errors)
        if not data or data.get('name') != package or not small_string(data.get('version')):
            continue
        cached = row(agent, 'mcp', p)
        cached.update(name=str(name), package=package, version=data['version'], source='npm npx 缓存',
                      group_key='mcp:npm:' + package, referenced_by=[str(cfg)],
                      reason='同一包的缓存版本；配置范围、其他项目和运行会话可能仍需使用，保留',
                      guidance='不按版本号直接删除 npx 缓存；本工具只盘点已登记 MCP 对应的包。')
        rows.append(cached)
