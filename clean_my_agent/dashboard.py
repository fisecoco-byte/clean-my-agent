"""Unified entry point. Inventory on entry; all writes require a reviewed action."""
from datetime import datetime
import os
from pathlib import Path
import sys

from . import dependency_report, extensions, inventory, policy as policies, resources, vault
from .actions import DependencyActions, SessionActions, ResourceActions
from .scan import agent as agent_of, layout, scan


class DashboardService:
    def __init__(self, *, agent='all', profile=None, days=None, full=True):
        self.roots, self.state = layout()
        self.agent, self.profile, self.days, self.full = agent, profile, days, full
        self.policy = policies.load(profile, days)

    def selected(self, agent):
        return {'codex', 'claude', 't3'} if agent == 'all' else {agent}

    def snapshot(self, progress=None):
        self.policy = policies.load(self.profile, self.days)
        result = scan(self.roots, self.selected(self.agent), self.policy, full=self.full, progress=progress)
        groups = [dict(agent=a, category=c, bytes=r[1], eligible_files=r[2], eligible_bytes=r[3])
                  for (a,c),r in result.rows.items()]
        extra = resources.inspect(self.roots, self.selected(self.agent), full=False, progress=progress)
        ext = extensions.inspect(self.roots, self.selected(self.agent), full=False, progress=progress)
        return dict(groups=groups, bytes=sum(r['bytes'] for r in groups),
                    eligible_bytes=sum(r['eligible_bytes'] for r in groups),
                    eligible_files=len(result.items), full=self.full, deferred_dirs=len(result.deferred),
                    roots={k:str(p) for k,p in self.roots.items() if agent_of(k) in self.selected(self.agent)},
                    errors=result.errors + extra['errors'] + ext['errors'], missing_roots=result.missing,
                    agent_resources=extra, extensions=ext, policy=self.policy.describe(),
                    read_only=os.name == 'nt',
                    bytes_scope=('Windows 文件分配量，不含文件系统元数据；共享与释放未知；当前仅盘点' if os.name == 'nt' else
                                 '已选 Agent 主目录计块；额外目录另列，不把重叠或共享空间重复相加'),
                    measured_at=datetime.now().astimezone().isoformat(timespec='seconds'))

    def clean_preview(self, agent, progress=None):
        self.policy.require_current()
        result = scan(self.roots, self.selected(agent), self.policy, progress=progress)
        return dict(items=result.items, errors=result.errors)

    def stage_files(self, agent, selected, progress):
        if not selected:
            raise ValueError('请先选择文件')
        fresh = self.clean_preview(agent, progress)
        if fresh['errors']:
            raise ValueError('扫描不完整，未暂存；请先处理提示')
        current = {(x['source'], x['path']):x for x in fresh['items']}
        chosen = []
        for old in selected:
            item = current.get((old['source'], old['path']))
            if item is None or tuple(item['stamp']) != tuple(old['stamp']):
                raise ValueError('文件或策略已变化，请返回后重新预览')
            chosen.append(item)
        if len({(x['source'],x['path']) for x in chosen}) != len(chosen):
            raise ValueError('重复的文件选择')
        progress('再次核对并暂存缓存 / 日志')
        key, count, size, errors = vault.save(chosen, self.roots, self.state, self.policy)
        return dict(batch=key, count=count, bytes=size, errors=errors)

    def batches(self, progress=None):
        rows = []
        for key in vault.batches(self.state):
            try:
                detail = {}
                count, size, errors = vault.process(self.state, key, self.roots, 'list', False, detail=detail)
                rows.append(dict(batch=key, count=count, bytes=size, errors=errors, **detail))
            except (OSError, ValueError) as exc:
                rows.append(dict(batch=key, count=0, bytes=0, files=[], created='', errors=[str(exc)]))
        return dict(items=list(reversed(rows)), errors=[])

    def batch_preview(self, key, operation):
        if operation not in ('restore', 'purge'):
            raise ValueError('不支持的批次操作')
        detail = {}
        count, size, errors = vault.process(self.state, key, self.roots, operation, False, detail=detail)
        return dict(batch=key, operation=operation, count=count, bytes=size, errors=errors, **detail)

    def batch_apply(self, expected, progress):
        current = self.batch_preview(expected['batch'], expected['operation'])
        if current['errors'] or current['files'] != expected['files'] or current['created'] != expected['created']:
            raise ValueError('批次或保护条件已变化，未执行；请重新核对')
        progress('还原批次' if expected['operation']=='restore' else '永久清除批次')
        count, size, errors = vault.process(self.state, expected['batch'], self.roots,
                                           expected['operation'], True)
        return dict(batch=expected['batch'], operation=expected['operation'], count=count, bytes=size, errors=errors)


def browse(ui, *, agent='all', profile=None, days=None, full=True):
    if not (ui.active and sys.stdin.isatty() and os.environ.get('TERM') not in ('', 'dumb')):
        return False
    try:
        from .dashboard_tui import DashboardApp, FilesApp, BatchesApp, load_page
        from .storage_tui import StorageApp
        from .session_tui import SessionApp
    except ImportError:
        return False
    from .resource_tui import browse as browse_resources
    service = DashboardService(agent=agent, profile=profile, days=days, full=full)
    snapshot, selected = None, agent
    notices = []
    while True:
        home = DashboardApp(service, snapshot=snapshot, agent=selected, notices=notices)
        request = home.run()
        snapshot = home.snapshot
        if not request:
            break
        route, selected = request
        notices = []
        try:
            if route == 'deps':
                data = load_page('核对项目产物', lambda p: dependency_report.inspect_agents(
                    service.roots, service.policy, agents=sorted(service.selected(selected)), progress=p))
                page = StorageApp(data, actions=DependencyActions(service.roots, service.state, service.policy), start_worktrees=True)
            elif route == 'sessions':
                data, rows = load_page('读取会话与标题', lambda p: inventory.inspect(
                    service.roots, service.policy, agent=selected, progress=p))
                page = SessionApp(data, rows, actions=SessionActions(service.roots, agent=selected, limit=10, full=False))
            elif route == 'clean':
                data = load_page('预览缓存与旧日志', lambda p: service.clean_preview(selected, p))
                page = FilesApp(service, selected, data)
            elif route == 'trash':
                page = BatchesApp(service)
            elif route == 'resources':
                data = load_page('核对 Agent 资源', lambda p: resources.inspect(service.roots,
                    service.selected(selected), full=True, policy=service.policy, progress=p))
                page = StorageApp(data, actions=ResourceActions(service.roots, service.state, service.policy))
            elif route == 'extensions':
                def fetch(progress):
                    ext = extensions.inspect(service.roots, service.selected(selected), full=True, progress=progress)
                    return ext
                data = load_page('测量资源与版本占用', fetch)
                browse_resources(ui, data)
                continue
            else:
                continue
            page.return_to_dashboard = True
            page.run()
            notices = list(page.operation_results)
            if notices:
                # Never return stale totals after a mutation or an uncertain result.
                snapshot = None
        except (OSError, ValueError, RuntimeError) as exc:
            notices = ['未完成：' + str(exc)]
    return True
