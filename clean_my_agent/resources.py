"""Agent resource inventory and shared extension report helpers."""
import os
from pathlib import Path
import stat
import sys

from .scan import fingerprint
from .storage import measure
from .terminal import fmt, safe
from .portable import allocated

LABELS = {'agent-version': 'Agent 安装版本', 'desktop-cache': '桌面缓存', 'logs': '旧日志',
          'update-staging': '更新残留', 'crash-reports': '崩溃报告', 'runtime': '运行环境',
          'marketplace-staging': '插件市场暂存', 'plugin': 'Plugin', 'skill': 'Skill', 'mcp': 'MCP'}


def children(path, errors):
    try:
        if path.resolve(strict=True) != path or not path.is_dir():
            errors.append(f'跳过含链接或非目录的资源根：{path}')
            return []
        return sorted(path.iterdir())
    except FileNotFoundError:
        return []
    except (OSError, RuntimeError):
        errors.append(f'无法读取资源目录：{path}')
        return []


def row(agent, category, path, **values):
    return dict(agent=agent, category=category, path=str(path), name=path.name,
                version=None, status='retained', reason='只读盘点；不进入常规清理',
                can_stage=False, directory_bytes=None, shared_bytes=None, reclaimable_bytes=None,
                referenced_by=[], **values)


def inspect(roots, selected, *, full=True, progress=None, policy=None):
    from .agent_resources import inspect as inspect_resources
    return inspect_resources(roots, selected, full=full, progress=progress, policy=policy)


def measure_rows(rows, roots, *, full, progress, errors):
    measured = {}
    for n, r in enumerate(rows):
        path = Path(r['path'])
        r['included_in_primary'] = any(path == base or base in path.parents for base in roots.values())
        r['measurement'] = 'not-requested'
        if not r.get('measure', True):
            r['measurement'] = 'component-of-parent' if r.get('parent_path') else 'configuration-only'
            continue
        if progress:
            progress('盘点 Agent 资源', n, len(rows), r['name'])
        try:
            info = path.lstat()
            r['stamp'] = fingerprint(info)
            if path.is_symlink() or path.resolve(strict=True) != path:
                r.update(measurement='link', reason='链接仅盘点，不跟随或清理', directory_bytes=allocated(path, info))
                continue
            if not full:
                continue
            key = (info.st_dev, info.st_ino)
            if key in measured:
                r.update(measurement='alias', alias_of=measured[key])
                continue
            measured[key] = str(path)
            if stat.S_ISDIR(info.st_mode):
                sizes, _, _ = measure(path, on_entries=(lambda count: progress(
                    '盘点 Agent 资源', n, len(rows), f'{r["name"]} · {count:,} 项')) if progress else None)
                r.update(sizes)
            elif stat.S_ISREG(info.st_mode):
                r['directory_bytes'] = allocated(path, info)
            else:
                r.update(measurement='unsupported', reason='特殊文件，仅列出路径')
                continue
            r['measurement'] = 'measured'
        except (OSError, ValueError, RuntimeError):
            r.update(measurement='unknown', directory_bytes=None, reason='无法完整测量；保留')
            errors.append(f'无法完整测量资源：{path}')
    if progress and rows:
        progress('盘点 Agent 资源', len(rows), len(rows))


def display(ui, doc, *, details=False, title='Agent 缓存 / 版本 / 残留'):
    ui.section(title)
    print('  ' + doc['totals_note'])
    rows = doc['entries']
    if not rows:
        print('  未发现已识别资源。')
        return
    shown = rows if details else sorted(rows, key=lambda r: -(r['directory_bytes'] or 0))[:12]
    for r in shown:
        amount = '未测量' if r['directory_bytes'] is None else fmt(r['directory_bytes'])
        print(f"  {r['agent']:7} {amount:>10}  {LABELS[r['category']]} · {safe(r['name'])}" +
              (f" · {safe(r['version'])}" if r.get('version') else ''))
        print('    ' + safe(r['path']))
        print('    ' + safe(r['reason']))
        if r.get('guidance'):
            print('    ' + safe(r['guidance']))
    if len(shown) < len(rows):
        print(f'  另有 {len(rows) - len(shown)} 项；--details 或交互资源页查看。')
