"""One read-only report used by scan, deps and the single-path preview."""
import stat
import os
import time
from pathlib import Path

from . import activity, deps, project_types
from .policy import DAY_NS
from .storage import DirectoryUsage, measure, workspace_usage
from .progress import parallel
from .scan import fingerprint
from .dependency_safety import ProcessSnapshot
from .terminal import fmt, safe

FIELDS = ('directory_bytes', 'shared_bytes', 'known_shared_bytes', 'unknown_bytes',
          'reclaimable_bytes', 'reclaimable_min_bytes', 'reclaimable_max_bytes')


def empty_report(full=True):
    return dict(mode='measured' if full else 'references_only', entries=[], projects=[], errors=[], external_targets={},
                title='工作树 / 多语言产物', scope='项目完整占用另列；产物子项不重复相加')


def inspect(root, policy, *, full=True, only=None, progress=None, source='codex', activity_records=None,
            activity_roots=None, annotate=True, references=None, workers=None):
    if os.name == 'nt':
        from .portable import dependencies
        report = dependencies(root, full=full, only=only, progress=progress)
        for row in report['entries'] + report.get('projects', []):
            row['agent'] = source
        return (activity.annotate(report, activity_roots or {source: root}, records=activity_records, progress=progress)
                if annotate else report)
    refs = references if references is not None else deps.References(root, nested=False, progress=progress)
    if only is not None and (not deps.valid_location(root, only) or only not in refs.paths):
        raise ValueError('请选择盘点返回的项目产物绝对路径')
    errors, rows, identities, measured = list(refs.errors), [], {}, {}
    processes = None
    cutoff = time.time_ns() - policy.retention['dependencies'] * DAY_NS
    paths = sorted([only] if only is not None else refs.paths)
    stage = '测量依赖目录' if full else '检查依赖目录'

    def inspect_path(path, on_entries):
        row = dict.fromkeys(FIELDS)
        row.update(path=str(path), kind='directory', status='unmeasured', reason=None,
                   referenced_by=[], target=None, installation=None, runtime=None,
                   agent=source, artifact=path.name, project=str(path.parent))
        identity, summary, error = None, None, None
        try:
            row['referenced_by'] = list(map(str, refs.incoming(path)))
            s = path.lstat()
            row['stamp'] = fingerprint(s)
            identity = (s.st_dev, s.st_ino)
            if stat.S_ISLNK(s.st_mode):
                target = refs.targets.get(path)
                row.update(kind='symlink', status='retained', reason='仅链接，不跟随或清理',
                           target=str(target) if target is not None else None,
                           directory_bytes=s.st_blocks * 512)
            elif not stat.S_ISDIR(s.st_mode):
                row.update(status='retained', reason='不是依赖目录')
            elif not full:
                try:
                    refs.guard(path)
                except deps.Retain as exc:
                    row.update(status='retained', reason=str(exc))
            else:
                deps.check_mounts(path)
                try:
                    _, project_path = deps.project_context(path)
                except ValueError:
                    project_path = path.parent
                installation = project_types.Evidence(path, project=project_path)
                row['project'] = str(project_path)
                if installation.detail:
                    row.update(installation.detail)
                elif path.name == 'node_modules':
                    row.update(ecosystem='JavaScript / TypeScript', artifact_category='项目依赖', rebuild='按锁文件重新安装依赖')
                usage = DirectoryUsage()
                usage.observe(s)
                def observe(relative, info):
                    installation.observe(relative, info)
                    usage.observe(info)
                sizes, newest, nested_git = measure(path, on_entries=on_entries, on_entry=observe)
                row['installation'] = installation.finish()
                row.update(sizes)
                if fingerprint(path.lstat()) != row['stamp']:
                    raise ValueError('测量期间目录已变化，请重新扫描')
                summary = (row['stamp'], usage)
                try:
                    deps.check_policy(root, path, policy, source)
                    if nested_git:
                        raise deps.Retain('包含嵌套的 Git 仓库')
                    if newest > cutoff:
                        raise deps.Retain(f"最近 {policy.retention['dependencies']} 天有改动")
                    if row['installation']['state'] != 'verified':
                        raise deps.Retain(row['installation']['reason'])
                except ValueError as exc:
                    row.update(status='retained', reason=str(exc))
                else:
                    row['status'] = 'candidate'
        except (OSError, ValueError) as exc:
            row.update(status='retained', reason='无法完整测量：' + str(exc))
            error = f'{path}: {exc}'
        return row, identity, summary, error

    for path, (row, identity, summary, error) in parallel(paths, inspect_path, progress, stage, workers=workers):
        rows.append(row)
        identities[path] = identity
        if summary is not None:
            measured[path] = summary
        if error:
            errors.append(error)
    # Concurrent completion must not change report order or error ordering.
    errors.sort()
    rows.sort(key=lambda r: r['path'])
    if full and rows:
        # Re-read all references after measurement, including external checkouts.
        refs = deps.References(root, progress=progress)
        if progress:
            progress('检查项目运行状态')
        processes = ProcessSnapshot()
        errors.extend(e for e in refs.errors if e not in errors)
        for row in rows:
            path = Path(row['path'])
            try:
                s = path.lstat()
                if (s.st_dev, s.st_ino) != identities.get(path):
                    raise ValueError('盘点期间路径被替换，请重新扫描')
                row['referenced_by'] = list(map(str, refs.incoming(path)))
            except (OSError, ValueError) as exc:
                measured.pop(path, None)
                row.update(dict.fromkeys(FIELDS))
                row.update(status='retained', reason=str(exc))
                errors.append(f'{path}: {exc}')
                continue
            if row['kind'] == 'directory':
                row['runtime'] = processes.check(path, project=Path(row['project']))
                if row['runtime']['state'] != 'idle' and row['status'] == 'candidate':
                    row.update(status='retained', reason=row['runtime']['reason'])
                try:
                    refs.guard(path)
                except deps.Retain as exc:
                    row.update(status='retained', reason=str(exc))
            elif path in refs.targets:
                row['target'] = str(refs.targets[path])
    rows.sort(key=lambda r: (r['kind'] == 'symlink', -(r['directory_bytes'] or 0), r['path']))
    external = {str(target): list(map(str, refs.incoming(target))) for source, target in refs.targets.items()
                if source in refs.paths and target not in refs.paths and (only is None or source == only)}
    projects = []
    workspaces = refs.repositories | {p for p in refs.workspaces if not any(r == p or r in p.parents for r in refs.repositories)}
    workspaces = {p for p in workspaces if p in refs.repositories or not any(r != p and r in p.parents for r in workspaces)}
    for index, repo in enumerate(sorted(workspaces)):
        entry = dict(path=str(repo), agent=source, directory_bytes=None,
                     artifacts=sum(repo == Path(r['project']) or repo in Path(r['path']).parents for r in rows))
        try:
            entry['stamp'] = fingerprint(repo.lstat())
        except OSError as exc:
            errors.append(str(exc))
        if full:
            try:
                if progress:
                    progress('统计工作树完整占用', index, len(workspaces), repo.name)
                # Whole-tree du would read every measured artifact a second time.
                # Retain the native fast path when no subtree can be reused.
                reusable = {p: value for p, value in measured.items() if repo in p.parents}
                entry['directory_bytes'] = (workspace_usage(repo, reusable, progress=progress) if reusable else deps.usage(repo))
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
        projects.append(entry)
    report = dict(mode='measured' if full else 'references_only', entries=rows, errors=errors,
                external_targets=external, projects=projects,
                title='工作树 / 多语言产物', scope='项目完整占用另列；产物是其中的子项，共享量及释放估计不能相加')
    if full and projects and processes is None:
        processes = ProcessSnapshot()
    return (activity.annotate(report, activity_roots or {source: root}, records=activity_records, processes=processes, progress=progress)
            if annotate else report)


def inspect_agents(roots, policy, *, agents=('codex', 'claude', 't3'), only=None, full=True, progress=None,
                   on_preview=None, workers=None):
    selected = [(agent, roots[agent]) for agent in agents if only is None or roots[agent] in only.parents]
    if not selected:
        raise ValueError('路径不在所选 Agent 的工作树范围内')
    references = {}
    def merge(combined, report):
        for key in ('entries', 'projects', 'errors'):
            combined[key].extend(report.get(key, []))
        combined['external_targets'].update(report['external_targets'])
    if on_preview and full:
        preview = empty_report(False)
        for agent, root in selected:
            if os.name != 'nt':
                references[agent] = deps.References(root, nested=False, progress=progress)
            merge(preview, inspect(root, policy, source=agent, only=only, full=False, progress=progress,
                                   references=references.get(agent), annotate=False, workers=workers))
        on_preview(preview)
    combined = empty_report(full)
    activity_roots = {k: v for k, v in roots.items() if k in ('codex', 'claude')}
    # Index metadata is common to all selected agents; read it once per report.
    records = activity.session_records(activity_roots)
    for agent, root in selected:
        report = inspect(root, policy, source=agent, only=only, full=full, progress=progress,
                         activity_roots=activity_roots, activity_records=records,
                         references=references.get(agent), workers=workers)
        merge(combined, report)
    return combined


def display(ui, report):
    rows = report['entries']
    ui.section('工作树与多语言产物')
    for project in report.get('projects', []):
        print(f"  {safe(project['agent'])}  {fmt(project['directory_bytes']) if project['directory_bytes'] is not None else '未测量'}  {activity.badge(project)}  {safe(project['path'])}")
    if not rows:
        print('  未发现依赖目录。' if not report['errors'] else '  引用盘点不完整。')
        return
    def painted(n):
        if n is None:
            return '未知'
        if n >= 1024**3:
            return f'{n / 1024**3:,.2f} GiB'
        return fmt(n)
    quick = report['mode'] == 'references_only'
    if quick:
        print('  快速扫描仅检查根目录引用；完整引用和空间计量请用 scan --full 或 deps 查看。')
    else:
        print('  预计释放指单独永久删除该目录的数据块；暂存释放 0 B。各行不可相加。')
    for row in rows:
        # Non-destructive quick view only prints actual sharing relationships.
        if quick and row['kind'] != 'symlink' and not row['referenced_by']:
            continue
        path = safe(row['path'])
        if row['kind'] == 'symlink':
            print(f"  [软链接] {path} → {safe(row['target'] or '未知目标')}（不计目标大小）")
            continue
        status = {'candidate':'符合策略', 'retained':'保留', 'unmeasured':'未测量'}[row['status']]
        print(f'  [{status}] {path}')
        if not quick:
            shared = painted(row['shared_bytes'])
            if row['shared_bytes'] is None and row['known_shared_bytes']:
                shared += f"（已确认 {fmt(row['known_shared_bytes'])}）"
            freed = painted(row['reclaimable_bytes'])
            low, high = row['reclaimable_min_bytes'], row['reclaimable_max_bytes']
            if row['reclaimable_bytes'] is None and low is not None and high is not None:
                ends = ((painted(low), painted(high)) if painted(low) != painted(high)
                        else (f'{low:,} B', f'{high:,} B'))
                freed = ' 至 '.join(ends) + ('（部分块未知）' if row['unknown_bytes'] else '（范围）')
            print(f"    目录 {painted(row['directory_bytes'])}  ·  共享 {shared}  ·  预计释放 {freed}")
        if row['referenced_by']:
            origin = ('其他工作树' if any(Path(row['path']).parent not in Path(ref).parents
                                         for ref in row['referenced_by']) else '本工作树子项目')
            print(f'    被{origin}引用，禁止清理：')
            for ref in row['referenced_by']:
                print('      ' + safe(ref))
        elif row['reason']:
            print('    ' + safe(row['reason']))
        for check in ('installation', 'runtime'):
            if row.get(check):
                print('    ' + ('安装检查：' if check == 'installation' else '运行检查：') + safe(row[check]['reason']))
    for target, aliases in report['external_targets'].items():
        print(f'  [范围外目标] {safe(target)} · {len(aliases)} 个依赖链接引用，不计入大小或候选。')
    if not quick:
        n = sum(r['status'] == 'candidate' for r in rows)
        print(f'符合策略 {n} 个目录；其余保留。')
