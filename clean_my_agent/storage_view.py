"""Read-only presentation of a measured dependency report; never touches an installation."""
import json
import os
from pathlib import Path
import sys

from .terminal import cells, fmt, pad, safe
from . import activity

GROUPS = ('candidate', 'retained', 'links')
LABEL = {'candidate': '候选', 'retained': '保留', 'links': '链接'}


def project(path):
    """Keep the worktree identifier; many checkouts end with the same repo name."""
    parts = Path(path).parts
    for area in ('worktrees', '.chatgpt-projects'):
        if area in parts:
            tail = parts[parts.index(area) + 1:]
            tail = tail[:-1] if tail and tail[-1] == 'node_modules' else tail
            if area == 'worktrees' and tail:
                return safe(' / '.join(tail))
            if tail:
                return safe(' / '.join(tail[-2:]))
    path = Path(path)
    return safe(path.parent.name if path.name == 'node_modules' else path.name) or '未知目标'


def size(n):
    if n is None:
        return '未知'
    return f'{n / 1024**3:,.2f} GiB' if n >= 1024**3 else fmt(n)


def row_label(row):
    return safe(row['label']) if row.get('label') else project(row['path'])


def release(row):
    if row['reclaimable_bytes'] is not None:
        return size(row['reclaimable_bytes'])
    lo, hi = row['reclaimable_min_bytes'], row['reclaimable_max_bytes']
    if lo is None or hi is None:
        return '未知'
    if size(lo) == size(hi) and lo != hi:
        return f'{lo:,}–{hi:,} B'
    return f'{size(lo)}–{size(hi)}'


def exact(row, key):
    n = row[key]
    return '未知' if n is None else f'{size(n)}  ({n:,} B)'


def groups(report):
    result = {k: [] for k in GROUPS}
    links = {}
    for row in report['entries']:
        if row['kind'] == 'symlink':
            target = row.get('target')
            bucket = links.setdefault((target, None if target else row['path']), [])
            bucket.append(row['path'])
        else:
            result['candidate' if row['status'] == 'candidate' else 'retained'].append(row)
    for (target, _), sources in links.items():
        # External reference lists may include additional registered worktrees.
        refs = set(sources) | set(report.get('external_targets', {}).get(target, []))
        for row in report['entries']:
            if row['path'] == target:
                refs.update(row['referenced_by'])
        result['links'].append(dict(path=target or '未知目标', kind='link_group', sources=sorted(sources),
                                    referenced_by=sorted(refs), external=target in report.get('external_targets', {})))
    return result


def ordered(rows, group, by_size=False, query='', activity_order=None):
    def rank(r):
        if activity_order and group != 'links':
            return activity.sort_key(r, activity_order)
        if group == 'links':
            n = len(r['sources'])
        elif by_size:
            n = r['directory_bytes']
        else:
            n = r['reclaimable_bytes'] if r['reclaimable_bytes'] is not None else r['reclaimable_min_bytes']
        return n is None, -(n or 0), r['path']
    words = query.casefold().split()
    return sorted((r for r in rows if all(w in ' '.join([r['path'], r.get('agent',''), r.get('ecosystem',''), *r['referenced_by']]).casefold()
                                           for w in words)), key=rank)


def load_report(path):
    with Path(path).expanduser().open(encoding='utf-8') as f:
        doc = json.load(f)
    data = doc.get('project_artifacts', doc.get('node_modules', doc)) if isinstance(doc, dict) else None
    def strings(v):
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    def invalid():
        raise ValueError('这不是有效的 deps --json 或包含 project_artifacts 的 scan --json 报告')
    if (not isinstance(data, dict) or data.get('mode') not in ('measured','references_only')
            or not isinstance(data.get('entries'), list) or not strings(data.get('errors'))):
        invalid()
    for row in data['entries']:
        if (not isinstance(row, dict) or not isinstance(row.get('path'), str)
                or row.get('kind') not in ('directory','symlink','file')
                or row.get('status') not in ('candidate','retained','unmeasured')
                or not strings(row.get('referenced_by'))
                or any(row.get(k) is not None and not isinstance(row[k], str) for k in ('reason','target'))):
            invalid()
        for k in ('directory_bytes','shared_bytes','known_shared_bytes','unknown_bytes','reclaimable_bytes',
                  'reclaimable_min_bytes','reclaimable_max_bytes'):
            if k not in row or (row[k] is not None and (type(row[k]) is not int or row[k] < 0)):
                invalid()
        for k in ('reason','target'):
            row.setdefault(k, None)
    external = data.setdefault('external_targets', {})
    if not isinstance(external, dict) or not all(strings(v) for v in external.values()):
        invalid()
    if not isinstance(data.get('projects',[]),list):
        invalid()
    for row in data.get('projects',[]):
        if (not isinstance(row,dict) or not isinstance(row.get('path'),str)
                or row.get('agent') not in ('codex','claude','t3')
                or type(row.get('artifacts')) is not int or row['artifacts']<0
                or (row.get('directory_bytes') is not None and
                    (type(row['directory_bytes']) is not int or row['directory_bytes']<0))):
            invalid()
    return data


def crop(s, width):
    if cells(s) <= width:
        return s
    kept = ''
    for c in s:
        if cells(kept + c) >= width:
            break
        kept += c
    return kept + '…'


def summary(ui, report):
    """Compact printable view, also available without optional UI packages."""
    data = groups(report)
    nc, nr = (len(data[k]) for k in GROUPS[:2])
    nl = sum(len(r['sources']) for r in data['links'])
    ui.section(report.get('title', '项目产物'))
    if report['mode'] == 'references_only':
        held = sum(r['status']=='retained' for r in data['retained'])
        print(f'  {nr+nc} 个实体目录未测量  ·  已标记保留 {held} 个  ·  {nl} 个软链接')
    else:
        print(f'  候选 {nc}  ·  保留 {nr}  ·  {nl} 个链接指向 {len(data["links"])} 个目标')
    if report['errors']:
        ui.note('盘点不完整；部分值或引用关系无法核实。')
    if report['mode'] == 'references_only':
        for row in data['retained']:
            if row['referenced_by']:
                ui.note(f"{project(row['path'])} · 被 {len(row['referenced_by'])} 个依赖链接引用")
        ui.note('快速模式未测量依赖占用；运行 deps 查看完整报告。')
        return
    if not (nc or nr):
        ui.note('没有实体产物或资源。')
        return
    width = ui.width - 4
    wide = width >= 66
    widths = [width - (43 if wide else 16), *([11,11] if wide else []), 13]
    labels = ['项目 / 资源', *(['目录大小','共享占用'] if wide else []), '预计释放']
    def line(values):
        return '  ' + ' '.join(pad(crop(v, n), n, i > 0) for i,(v,n) in enumerate(zip(values,widths)))
    print(line(labels))
    for key in GROUPS[:2]:
        rows = ordered(data[key], key)
        for row in rows[:5 if key == 'candidate' else 2]:
            name = ('保留 · ' if key == 'retained' else '') + row_label(row)
            print(line([name, *([size(row['directory_bytes']),size(row['shared_bytes'])] if wide else []),
                        release(row)]))
        if len(rows) > (5 if key == 'candidate' else 2):
            print('  … 其余' + LABEL[key] + '已折叠')
    ui.note('单项预计释放不可相加；暂存释放 0 B。--plain 可打印完整路径和引用。')


def browse(ui, data, *, scan=None, source=None, actions=None, loader=None):
    if not (ui.active and sys.stdin.isatty() and os.environ.get('TERM') not in ('', 'dumb')
            and (loader or (data['mode'] == 'measured' and (data['entries'] or data.get('projects'))))):
        return False
    try:
        from .storage_tui import StorageApp
    except ImportError:
        return False
    app = StorageApp(data, scan=scan, source=source, actions=actions, loader=loader)
    selected = app.run()
    for result in app.operation_results:
        for line in result.splitlines():
            print(safe(line))
    if selected:
        print(safe(selected))
    else:
        print('已关闭报告。')
    return True
