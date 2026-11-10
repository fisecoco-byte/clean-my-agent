import argparse
import json
import os
import sys
from pathlib import Path

from . import dependency_report, inventory, inventory_report, storage_view, vault, resources, extensions, policy as policies
from .policy import CLEANABLE_FILES
from .scan import LABELS, fingerprint, layout, scan
from .actions import DependencyActions, SessionActions


from .terminal import Terminal, fmt, safe
from . import __version__


def positive_int(val):
    val = int(val)
    if val < 1:
        raise argparse.ArgumentTypeError('请输入正整数')
    return val


def days_arg(val):
    val = int(val)
    if val < 1:
        raise argparse.ArgumentTypeError('至少保留最近 1 天')
    return val


def write_flags(q, desc):
    mode = q.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='仅预览（默认）')
    mode.add_argument('--apply', action='store_true', help=desc)


def profile_flag(q):
    q.add_argument('--profile', choices=list(policies.PROFILES), help='选择清理策略（默认读取配置）')


def session_flags(q, *, edit=False):
    q.add_argument('--agent', choices=['codex', 'claude', 'all'], default='all',
                   help='选择 agent（默认两者）')
    for name, label in [('archive', '归档'), ('delete', '删除')]:
        q.add_argument('--' + name + '-days', type=int, choices=policies.SESSION_DAYS,
                       help=f'{label}复核的未活跃天数' + ('，需 --set-session-defaults' if edit else '（仅本次报告）'))


def parser():
    p = argparse.ArgumentParser(prog='clean-my-agent', description='查看 Codex / Claude Code 的磁盘占用，预览、暂存和清理旧文件。')
    p.add_argument('--version', action='version', version='%(prog)s '+__version__)
    sub = p.add_subparsers(dest='cmd', metavar='命令')
    q = sub.add_parser('mcp', help='启动本地 MCP 服务')
    q.add_argument('--allow-stage', action='store_true', help='允许暂存与恢复（macOS / Linux）')
    q = sub.add_parser('dashboard', aliases=['home'], help='统一存储总览（默认）；进入清理、会话和暂存区')
    q.add_argument('--agent', choices=['codex', 'claude', 't3', 'all'], default='all')
    profile_flag(q)
    depth = q.add_mutually_exclusive_group()
    depth.add_argument('--quick', dest='full', action='store_false', help='快速总览，明确标出未展开目录')
    depth.add_argument('--full', dest='full', action='store_true', help='统计完整主目录（默认）；依赖共享量按需测量')
    q.set_defaults(full=True)
    q.add_argument('--json', action='store_true', help='只读输出总览 JSON，不打开面板')
    for name, desc in [('scan', '查看占用；交互终端打开统一总览'), ('clean', '预览清理；加 --apply 移入暂存区')]:
        q = sub.add_parser(name, help=desc, description=desc)
        q.add_argument('--agent', choices=['codex', 'claude', 't3', 'all'], default='all', help='限定工具（默认 all）')
        profile_flag(q)
        q.add_argument('--days', type=days_arg, metavar='N', help='覆盖本次常规文件的分类保留天数')
        q.add_argument('--include-history', action='store_true', help=argparse.SUPPRESS)
        q.add_argument('--details', action='store_true', help='列出所有候选文件及大小')
        if name == 'scan':
            q.add_argument('--table', action='store_true', help='打印紧凑表格，不进入交互浏览')
            depth = q.add_mutually_exclusive_group()
            depth.add_argument('--full', dest='full', action='store_true', help='完整主目录（默认）；交互时进入依赖页测量共享，文本 / JSON 含依赖明细')
            depth.add_argument('--quick', dest='full', action='store_false', help='快速预览；跳过工作树大小和依赖逐项测量')
            q.set_defaults(full=True)
        else:
            write_flags(q, '实际移入暂存区；暂不释放磁盘')
        q.add_argument('--json', action='store_true', help='输出 JSON，容量单位为字节')
    q = sub.add_parser('inventory', aliases=['report', 'reporter'],
                       help='会话盘点；report 或 reporter 在终端中打开交互图表')
    q.add_argument('--tui', action='store_true', help='打开会话表格；可勾选并确认调用 Codex 归档或删除（需 UI 组件）')
    session_flags(q)
    q.add_argument('--details', action='store_true', help='展开原始会话文件和工作树的清单')
    q.add_argument('--html', metavar='报告.html', help='生成离线交互报告（不覆盖已有文件）')
    q.add_argument('--limit', default=10, type=positive_int, metavar='N', help='每组最多展示 N 项（默认 10，不影响汇总）')
    q.add_argument('--full', action='store_true', help='同时统计各工作树目录占用，可能较慢')
    q.add_argument('--json', action='store_true', help='输出结构化盘点结果及管理提示')
    q = sub.add_parser('policy', help='查看策略，或显式保存会话复核阈值')
    session_flags(q, edit=True)
    q.add_argument('--set-session-defaults', action='store_true', help='将指定天数保存为所选 agent 的报告默认值')
    profile_flag(q)
    q.add_argument('--json', action='store_true', help='以 JSON 输出生效策略')
    q.add_argument('--example', action='store_true', help='打印可保存为 policy.json 的示例配置')
    q = sub.add_parser('deps', help='盘点 Agent 工作树中的多语言依赖、构建产物和工具缓存')
    q.add_argument('path', nargs='?', metavar='产物路径', help='只处理盘点返回的指定目录')
    q.add_argument('--agent', choices=['codex', 'claude', 't3', 'all'], default='all')
    q.add_argument('--from-report', metavar='报告.json', help='只读打开已有的 deps 或 scan JSON 快照，不重新扫描')
    q.add_argument('--table', action='store_true', help='打印紧凑表格，不进入交互浏览')
    profile_flag(q)
    q.add_argument('--json', action='store_true', help='输出依赖占用、释放估计及工作树引用')
    write_flags(q, '将符合策略的指定产物目录移入暂存区')
    q = sub.add_parser('resources', help='盘点并暂存 Agent 缓存、旧版本与安装残留')
    q.add_argument('path', nargs='?', metavar='资源路径')
    from .agent_resources import FAMILIES
    q.add_argument('--agent', choices=['all', *FAMILIES], default='all')
    q.add_argument('--quick', action='store_true')
    q.add_argument('--table', action='store_true')
    q.add_argument('--json', action='store_true')
    profile_flag(q)
    write_flags(q, '重新核查并暂存一个资源候选')
    q = sub.add_parser('extensions', help='只读检测 Skill / MCP / Plugin 的多版本、引用与原生回收建议')
    q.add_argument('--agent', choices=['codex', 'claude', 'all'], default='all')
    q.add_argument('--quick', action='store_true', help='只识别版本和引用，不测量目录大小')
    q.add_argument('--details', action='store_true', help='打印全部资源明细')
    q.add_argument('--table', action='store_true', help='打印报告，不进入交互浏览')
    q.add_argument('--json', action='store_true', help='输出结构化版本复核报告')
    q = sub.add_parser('trash', help='列出暂存批次；指定批次可查看原路径')
    q.add_argument('batch', nargs='?', metavar='批次编号')
    for name, desc in [('restore', '还原批次，保留原路径已有文件'), ('purge', '预览永久删除指定批次；加 --apply 释放空间')]:
        q = sub.add_parser(name, help=desc, description=desc)
        q.add_argument('batch', metavar='批次编号')
        if name == 'purge':
            write_flags(q, '永久删除此批次，不可撤销')
    for cmd in dict.fromkeys([p, *sub.choices.values()]):
        cmd.add_argument('--plain', action='store_true', default=argparse.SUPPRESS,
                         help='使用没有图表、颜色和动画的纯文本界面')
    return p


def report(args, result, policy):
    rows = sorted((dict(agent=a, category=c, bytes=r[1], eligible_files=r[2], eligible_bytes=r[3])
                   for (a, c), r in result.rows.items()), key=lambda row: (row['agent'], -row['bytes']))
    total = sum(row['bytes'] for row in rows)
    eligible = sum(item['size'] for item in result.items)
    folders = sorted(result.folders.items(), key=lambda entry: (-entry[1], entry[0]))[:10]
    record = dict(mode='preview', full=getattr(args, 'full', False), days=args.days, bytes=total,
                  eligible_bytes=eligible, eligible_files=len(result.items), groups=rows,
                  errors=result.errors, missing_roots=result.missing, deferred_dirs=len(result.deferred))
    record['policy'] = policy.describe()
    record['kept'] = {reason: {'files': n, 'bytes': size} for reason, (n, size) in result.kept.items()}
    if args.details:
        record['files'] = [dict(path=os.path.join(i['root'], i['path']), bytes=i['size'], category=i['category'],
                                retention_days=policy.retention[i['category']])
                           for i in result.items]
    if record['full']:
        record['largest_groups'] = [dict(path=k, bytes=v) for k, v in folders]
    return record


def print_scan(args, result, policy, ui, record):
    total, eligible, rows = record['bytes'], record['eligible_bytes'], record['groups']
    if not args.json and ui.active:
        ui.overview(args, record)
        if record.get('agent_resources'):
            ui.note('以上为原有 Agent 主目录计块；额外资源另列，重叠明细不重复相加。')
    elif not args.json:
        print('完整扫描（主目录计块，含共享；额外资源另列）' if record['full'] else '快速扫描', f'· 已统计 {fmt(total)} · 策略 {policy.profile}')
        if rows:
            print(f'{"工具":8} {"占用":>9}  {"可清理":>9}   类别')
        for r in rows:
            c = r['category']
            suffix = '' if c in CLEANABLE_FILES else '（仅盘点）'
            if c in policy.retention:
                suffix = f'（保留 {policy.retention[c]} 天）' + suffix
            print(f"{r['agent']:8} {fmt(r['bytes']):>10}  {fmt(r['eligible_bytes']):>10}   {LABELS[c]}{suffix}")
        if result.kept:
            print('已检查文件的保留原因：' + '；'.join(
                f'{policies.REASONS[k]} {v[0]} 个' for k, v in sorted(result.kept.items())))
        if result.deferred:
            print(f'另有 {len(result.deferred)} 个目录未展开；用 scan --full 查看包含工作树、项目和插件的占用。')
        if record['full']:
            print('占用最大的目录或根目录文件组：')
            for item in record['largest_groups']:
                print(f"  {fmt(item['bytes']):>10}  {safe(item['path'])}")
        for root in result.missing:
            print(f'未找到：{safe(root)}')
        if args.details:
            for item in record['files']:
                print(f"  {fmt(item['bytes']):>10}  {safe(item['path'])}")
        print(f'可清理：{len(result.items)} 个文件，约 {fmt(eligible)}。')
        if args.agent in ('all', 'codex'):
            print('会话与工作树不进入常规清理。用 reporter 复核会话并调用 Codex 管理。')


def warn(errors):
    for e in errors[:10]:
        print(f'提示：{safe(e)}', file=sys.stderr)
    if len(errors) > 10:
        print(f'另有 {len(errors) - 10} 项未完成。', file=sys.stderr)


def run(args):
    if args.cmd == 'mcp':
        from .mcp_server import main as mcp_main
        return mcp_main(['--allow-stage'] if args.allow_stage else [])
    if os.name == 'nt' and (getattr(args, 'apply', False) or args.cmd == 'restore'
                            or getattr(args, 'set_session_defaults', False)):
        from .portable import READ_ONLY
        raise ValueError(READ_ONLY)
    ui = Terminal(plain=getattr(args, 'plain', False) or getattr(args, 'json', False))
    if args.cmd in ('dashboard', 'home'):
        from .dashboard import DashboardService, browse
        if args.json:
            doc = DashboardService(agent=args.agent, profile=args.profile, full=args.full).snapshot()
            print(json.dumps(doc, ensure_ascii=False))
            return int(bool(doc['errors']))
        if browse(ui, agent=args.agent, profile=args.profile, full=args.full):
            return 0
        if ui.active and sys.stdin.isatty():
            print("未安装交互组件；可执行 python3 -m pip install -e '.[ui]'，以下为只读报告。")
        # Pipes, --plain, dumb terminals and base installs never wait for input.
        args.cmd = 'scan'
        args.days, args.details, args.table = None, False, True
    elif (args.cmd == 'scan' and not args.json and not args.table and not args.details
          and not getattr(args, 'plain', False)):
        from .dashboard import browse
        if browse(ui, agent=args.agent, profile=args.profile, days=args.days, full=args.full):
            return 0
    if args.cmd == 'deps' and args.from_report is not None:
        if args.apply or args.path or args.profile:
            raise ValueError('--from-report 仅用于查看已有报告，不能指定依赖路径、策略或 --apply')
        data = storage_view.load_report(args.from_report)
        if args.json:
            print(json.dumps(data, ensure_ascii=False))
        else:
            source = Path(args.from_report).name
            if args.table or not storage_view.browse(ui, data, source=source):
                print('历史快照 · ' + safe(source))
                (storage_view.summary if ui.active or args.table else dependency_report.display)(ui, data)
            warn(data['errors'])
        return int(bool(data['errors']))
    if args.cmd in ('inventory', 'report', 'reporter'):
        session_actions = SessionActions(layout()[0], agent=args.agent, limit=args.limit, full=args.full)
        interactive = args.tui or args.cmd in ('report', 'reporter')
        with ui.working('按 agent 盘点会话活动') as progress:
            data, rows = inventory.inspect(layout()[0], policies.load(), agent=args.agent, full=args.full,
                                           limit=args.limit, archive_days=args.archive_days, delete_days=args.delete_days,
                                           progress=progress)
        if args.html:
            data['html_path'] = str(inventory_report.export(args.html, data, rows))
        if args.json:
            print(json.dumps(data, ensure_ascii=False))
        else:
            shown = False
            if interactive and not getattr(args, 'plain', False) and sys.stdin.isatty() and sys.stdout.isatty():
                from . import report_tui
                shown = report_tui.show(data, rows, args.details, actions=session_actions)
            if not shown:
                inventory_report.display(ui, data, args.details)
            if args.html:
                print('离线报告：' + safe(data['html_path']))
            warn(data['errors'])
        return int(bool(data['errors']))
    if args.cmd == 'policy':
        values = {k: getattr(args, k) for k in ('archive_days', 'delete_days') if getattr(args, k) is not None}
        if args.set_session_defaults:
            if not values or args.example or args.profile:
                raise ValueError('保存默认值时请指定 --archive-days 或 --delete-days，不与 --example、--profile 混用')
            policies.save_session_defaults(args.agent, values)
        elif values:
            raise ValueError('仅预览请使用 inventory；保存默认天数请加 --set-session-defaults')
    if args.cmd == 'policy' and args.example:
        print(json.dumps(policies.EXAMPLE, ensure_ascii=False, indent=2))
        return 0
    policy = None
    if args.cmd in ('scan', 'clean', 'deps', 'resources', 'purge', 'policy'):
        policy = policies.load(getattr(args, 'profile', None), getattr(args, 'days', None))
    if args.cmd == 'policy':
        if args.json:
            print(json.dumps(policy.describe(), ensure_ascii=False, indent=2))
        elif ui.active:
            ui.policy(policy)
        else:
            print(f'当前策略：{policy.profile}　配置文件：{safe(policy.path)}')
            for cat, days in policy.retention.items():
                gate = '；仅 deps 显式选择' if cat == 'dependencies' else ''
                print(f'  {LABELS[cat]}：保留 {days} 天{gate}')
            print('个人保护路径：' + ('、'.join(map(safe, policy.protect)) or '无'))
            print('会话（含归档）、快照、历史工具输出、工作树本体均仅盘点；用 inventory 查看 Codex 管理提示。')
        if not args.json:
            for key, v in policy.sessions.items():
                if args.agent in ('all', key):
                    print(f"{key} 会话复核：归档 ≥{v['archive_days']} 天；删除 ≥{v['delete_days']} 天。")
        return 0
    roots, state = layout()
    if args.cmd == 'extensions':
        selected = {'codex', 'claude'} if args.agent == 'all' else {args.agent}
        with ui.working('识别扩展来源与版本') as progress:
            data = extensions.inspect(roots, selected, full=not args.quick, progress=progress)
        if args.json:
            print(json.dumps(data, ensure_ascii=False))
        else:
            from .resource_tui import browse
            if args.table or args.details or not browse(ui, data):
                resources.display(ui, data, details=args.details, title='Skill / MCP / Plugin · 只读版本复核')
            warn(data['errors'])
        return int(bool(data['errors']))
    exe = './clean-my-agent' if sys.argv[0].startswith('./') else 'clean-my-agent'
    apply_hint = '将 --dry-run 换为 --apply' if getattr(args, 'dry_run', False) else '添加 --apply'
    if args.cmd == 'resources':
        from . import agent_resources
        from .actions import ResourceActions
        path = Path(args.path).expanduser() if args.path else None
        if args.apply and (path is None or args.quick):
            raise ValueError('请先预览，再指定一个资源绝对路径；快速盘点不能暂存')
        selected = set(agent_resources.FAMILIES) if args.agent == 'all' else {args.agent}
        with ui.working('核对 Agent 资源') as progress:
            data = agent_resources.inspect(roots, selected, full=not args.quick, only=path, policy=policy, progress=progress)
        action = ResourceActions(roots, state, policy)
        if args.apply:
            result = action.stage(data['entries'][0], lambda *a,**kw:None)
            data.update(batch=result['batch'], moved_items=result['moved_items'])
            data['errors'].extend(result['errors'])
        if args.json:
            print(json.dumps(data, ensure_ascii=False))
        elif path is None and not args.table and storage_view.browse(ui, data, actions=None if args.dry_run else action):
            pass
        else:
            if args.table:
                storage_view.summary(ui, data)
            else:
                resources.display(ui, data, details=True)
            if data.get('batch'):
                print('已暂存；批次 '+data['batch']+'。暂存释放 0 B。')
        return int(bool(data['errors']))
    if args.cmd == 'deps':
        if args.apply and not args.path:
            raise ValueError('请先用 deps 查看候选，再指定一个产物绝对路径')
        path = Path(args.path).expanduser() if args.path else None
        selected = ('codex', 'claude', 't3') if args.agent == 'all' else (args.agent,)
        with ui.working('逐项核实依赖的共享空间和工作树引用') as progress:
            data = dependency_report.inspect_agents(roots, policy, agents=selected, only=path, progress=progress)
        if not args.json:
            if path is None and not args.table and storage_view.browse(
                    ui, data, actions=None if args.dry_run else DependencyActions(roots, state, policy)):
                warn(data['errors'])
                return int(bool(data['errors']))
            ui.header('工作树 / 多语言产物', '暂存' if args.apply else '只读')
            (storage_view.summary if path is None and (ui.active or args.table)
             else dependency_report.display)(ui, data)
        if path is not None and args.apply:
            row = data['entries'][0]
            if row['status'] != 'candidate' or data['errors']:
                data['errors'].append(row['reason'] or '引用或目录状态无法完整核实；未暂存')
            else:
                root = roots[row['agent']]
                item = dict(source=row['agent'], root=str(root), path=str(path.relative_to(root)),
                            category='dependencies', size=row['directory_bytes'], stamp=fingerprint(path.lstat()), project=row['project'])
                key, count, _, errors = vault.save([item], roots, state, policy)
                data.update(batch=key, moved_items=count)
                data['errors'].extend(errors)
                if count and not args.json:
                    print(f'已暂存依赖目录，尚未释放空间。\n恢复：{exe} restore {key}\n永久清除：{exe} purge {key} --apply')
        elif not args.json:
            print('仅预览；请先停用项目，再指定一个符合策略的路径并加 --apply 暂存。')
        if args.json:
            print(json.dumps(data, ensure_ascii=False))
        else:
            warn(data['errors'])
        return int(bool(data['errors']))
    if args.cmd in ('scan', 'clean'):
        selected = {'codex', 'claude', 't3'} if args.agent == 'all' else {args.agent}
        dep_data = None
        resource_data = extension_data = None
        with ui.working('读取文件占用') as progress:
            res = scan(roots, selected, policy, full=getattr(args, 'full', False), progress=progress)
            if args.cmd == 'scan':
                dep_data = dependency_report.inspect_agents(roots, policy, agents=sorted(selected), full=args.full, progress=progress)
                res.errors.extend(dep_data['errors'])
            if args.cmd == 'scan':
                resource_data = resources.inspect(roots, selected, full=args.full, progress=progress, policy=policy)
                extension_data = extensions.inspect(roots, selected, full=args.full, progress=progress)
                res.errors.extend(resource_data['errors'] + extension_data['errors'])
        doc = report(args, res, policy)
        if resource_data is not None:
            doc.update(agent_resources=resource_data, extensions=extension_data,
                       bytes_scope='仅原有 Agent 数据根目录；额外资源另列，重叠子项不重复加入此合计')
        if os.name == 'nt':
            doc.update(read_only=True, bytes_scope='Windows 文件分配量，不含文件系统元数据；共享、释放和引用检查未支持')
        if dep_data is not None:
            doc['project_artifacts'] = dep_data
            doc['node_modules'] = dep_data  # v0.1 JSON compatibility alias.
            if (args.full and not args.details and not args.table and not args.json
                    and storage_view.browse(ui, dep_data, scan=doc, actions=DependencyActions(roots, state, policy))):
                warn(res.errors)
                return int(bool(res.errors))
        print_scan(args, res, policy, ui, doc)
        if resource_data is not None and not args.json:
            resources.display(ui, resource_data, details=args.details)
            resources.display(ui, extension_data, details=args.details, title='Skill / MCP / Plugin · 只读版本复核')
        if dep_data is not None and not args.json:
            (storage_view.summary if (ui.active or args.table) and not args.details
             else dependency_report.display)(ui, dep_data)
        if getattr(args, 'apply', False):
            if res.errors:
                doc.update(mode='aborted', batch=None, moved_files=0, moved_bytes=0)
                if args.json:
                    print(json.dumps(doc, ensure_ascii=False))
                else:
                    warn(['扫描不完整，未进行暂存', *res.errors])
                return 1
            key, count, size, errors = vault.save(res.items, roots, state, policy)
            res.errors.extend(errors)
            doc.update(mode='quarantine', batch=key, moved_files=count, moved_bytes=size)
            if not args.json:
                print(f'已暂存 {count} 项（{fmt(size)}），尚未释放空间。')
                if key:
                    print(f'恢复：{exe} restore {key}\n永久清除：{exe} purge {key} --apply')
        elif not args.json:
            if ui.active:
                ui.section('下一步')
                ui.note('仅预览，未移动文件。')
                flags = []
                for opt in ('agent', 'profile', 'days'):
                    value = getattr(args, opt)
                    if value is not None and not (opt == 'agent' and value == 'all'):
                        flags.extend(['--' + opt, str(value)])
                if doc['errors']:
                    ui.note('请先处理扫描提示，再重新预览。')
                elif args.cmd == 'clean' and res.items and args.details:
                    ui.note('确认后暂存这些文件（暂不释放空间）：')
                    print('  ' + ' '.join([exe, 'clean', *flags, '--apply']))
                elif res.items and (args.cmd != 'clean' or not args.details):
                    print('  ' + ' '.join([exe, 'clean', *flags, '--details']))
                elif doc['deferred_dirs']:
                    print('  ' + ' '.join([exe, 'scan', *flags, '--full']))
            else:
                print('仅预览，未移动文件。' + (f' {apply_hint} 执行。' if args.cmd == 'clean' else ''))
        if args.json:
            print(json.dumps(doc, ensure_ascii=False))
        else:
            warn(res.errors)
        return int(bool(res.errors))
    if args.cmd == 'trash':
        ui.header('暂存区' + (' / 批次明细' if args.batch else ' / 批次列表'), '只读')
        keys = [args.batch] if args.batch else vault.batches(state)
        if not keys:
            print('暂存区为空。')
        failed = False
        for key in keys:
            detail = {}
            try:
                count, size, errors = vault.process(state, key, roots, 'list', False, detail=detail)
            except (OSError, ValueError) as exc:
                warn([f'{key}: {exc}'])
                failed = True
                continue
            if ui.active:
                ui.section(key)
                ui.note(f"{detail['created']}  /  {fmt(size)}  /  {count} 项")
            else:
                print(f"{key}  {detail['created']}  {fmt(size):>10}  {count} 项")
            if args.batch:
                if detail['policy']:
                    print(f"暂存时策略：{safe(detail['policy']['profile'])}")
                for path, amount in detail['files']:
                    print(f'  {fmt(amount):>10}  {safe(path)}')
            warn(errors)
            failed |= bool(errors)
        return int(failed)
    applying = args.cmd == 'restore' or args.apply
    ui.header('还原批次' if args.cmd == 'restore' else '永久清除',
              '还原' if args.cmd == 'restore' else '不可撤销' if applying else '只读')
    count, size, errors = vault.process(state, args.batch, roots, args.cmd, applying, policy=policy)
    if args.cmd == 'restore':
        print(f'已还原 {count} 项（{fmt(size)}）。')
    elif applying:
        print(f'已永久清除 {count} 项，清单大小 {fmt(size)}（不代表磁盘净释放量）。')
    else:
        print(f'此批次将永久清除 {count} 项（{fmt(size)}）。仅预览；{apply_hint} 执行。')
    warn(errors)
    return int(bool(errors))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cli = parser()
    # Keep the existing global --plain before an explicit subcommand valid.
    command_args = [value for value in argv if value != '--plain']
    if not command_args or (command_args[0].startswith('-') and command_args[0] not in ('-h','--help','--version')):
        argv = ['dashboard', *argv]
    args = cli.parse_args(argv)
    if args.cmd is None:
        args = cli.parse_args(['dashboard', *argv])
    if getattr(args, 'include_history', False):
        cli.error('--include-history 已停用：会话和历史工具输出仅盘点。请用 inventory 查看 Codex 管理方式。')
    try:
        return run(args)
    except (OSError, ValueError, KeyError, RecursionError) as e:
        print(f'未完成：{safe(e)}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('操作已中断。如已开始暂存，可用 trash 查看批次。', file=sys.stderr)
        return 130
