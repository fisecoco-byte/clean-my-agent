"""Render actual Textual screens with fictional data; never run native/file actions."""
import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop('NO_COLOR', None)
os.environ['COLORTERM'] = 'truecolor'
from clean_my_agent.session_tui import SessionApp
from clean_my_agent.storage_tui import StorageApp
from clean_my_agent.inventory import BUCKETS
from clean_my_agent.resources import row
from clean_my_agent.resource_tui import make_screen
from clean_my_agent.tui_common import ManagedApp, SHARED_CSS
from clean_my_agent.dashboard_tui import DashboardApp, FilesApp

OUT = Path(__file__).resolve().parents[1]/'docs'
class NoActions:
    def apply(self, *a, **kw): raise AssertionError('preview only')
    def stage(self, *a, **kw): raise AssertionError('preview only')
    def stage_files(self, *a, **kw): raise AssertionError('preview only')
    def batch_apply(self, *a, **kw): raise AssertionError('preview only')
    agent='all'
    def selected(self, agent): return {'codex','claude'} if agent=='all' else {agent}

def session_fixture():
    doc=dict(limit=10,agents={})
    all_rows={}
    names=['整理离线安装指南','追踪登录超时的原因','校对终端命令示例','更新数据库迁移步骤','优化会话列表筛选','构建依赖占用报告']
    for agent in ('codex','claude'):
        rows=[dict(session_id=f'019a5290-7f49-7c91-8d5e-{i+1:012x}',title=title,
                   state='archived' if i>3 else 'open', route='review' if agent=='claude' else 'delete' if i>3 else 'archive',
                   idle_days=32-i*2,bytes=8192*(i+1),latest_ns=1,hold_reason=None) for i,title in enumerate(names)]
        doc['agents'][agent]=dict(policy={'archive_days':10,'delete_days':20},errors=[],session_count=len(rows),
            groups={},histogram=dict(zip(BUCKETS,[0,0,0,0,len(rows)])))
        all_rows[agent]=rows
    return doc,all_rows

async def main():
    gib=1024**3
    groups=[]
    for agent,category,amount,eligible in [('codex','worktrees',68,0),('codex','sessions',3,0),
            ('codex','plugins',1,0),('codex','logs',.5,.25),('codex','database',.5,0),
            ('claude','sessions',1.25,0),('claude','cache',.75,.125)]:
        groups.append(dict(agent=agent,category=category,bytes=int(amount*gib),
                           eligible_bytes=int(eligible*gib),eligible_files=1 if eligible else 0))
    home=dict(groups=groups,bytes=75*gib,eligible_bytes=int(.375*gib),eligible_files=2,
              full=True,deferred_dirs=0,errors=[],missing_roots=[],roots={'codex':'/Users/demo/.codex','claude':'/Users/demo/.claude'},
              measured_at='2026-11-09T12:00:00+08:00',agent_resources={'entries':[{'agent':'codex'}]*4},
              extensions={'groups':[{'agents':['codex']}]},policy={'retention_days':{'logs':30,'cache':30}})
    app=DashboardApp(NoActions(),snapshot=home)
    async with app.run_test(size=(120,26)) as pilot:
        await pilot.press('down')
        app.save_screenshot('dashboard.svg',path=str(OUT))
    files=dict(items=[dict(source='codex',root='/Users/demo/.codex',path='log/codex-tui.log.2',
                          category='logs',size=256*1024**2,stamp=(1,2,3,4)),
                     dict(source='claude',root='/Users/demo/.claude',path='cache/changelog.md',
                          category='cache',size=8192,stamp=(1,2,3,4))],errors=[])
    app=FilesApp(NoActions(),'all',files)
    app.return_to_dashboard=True
    async with app.run_test(size=(130,32)) as pilot:
        await pilot.press('space','x')
        app.save_screenshot('dashboard-clean.svg',path=str(OUT))
    doc,rows=session_fixture()
    app=SessionApp(doc,rows,actions=NoActions())
    async with app.run_test(size=(148,35)) as pilot:
        await pilot.press('space','down','space')
        app.save_screenshot('reporter-table.svg',path=str(OUT))
        await pilot.press('x')
        app.save_screenshot('reporter-confirm.svg',path=str(OUT))
        await pilot.press('escape','d','space','x')
        app.save_screenshot('reporter-delete.svg',path=str(OUT))
        await pilot.press('escape','i')
        app.save_screenshot('reporter-distribution.svg',path=str(OUT))
    entry=dict(path='/Users/demo/.codex/worktrees/api-timeout/console/node_modules',kind='directory',status='candidate',
        target=None,reason=None,referenced_by=[],directory_bytes=int(4.1*gib),shared_bytes=int(4.1*gib)-8*1024**2,
        known_shared_bytes=0,unknown_bytes=0,reclaimable_bytes=8*1024**2,
        reclaimable_min_bytes=8*1024**2,reclaimable_max_bytes=8*1024**2)
    doc=dict(mode='measured',entries=[entry],errors=[],external_targets={})
    app=StorageApp(doc,actions=NoActions())
    async with app.run_test(size=(130,35)) as pilot:
        await pilot.pause()
        app.save_screenshot('deps-table.svg',path=str(OUT))
        await pilot.press('t')
        app.save_screenshot('deps-stage.svg',path=str(OUT))
    rows=[]
    for category,name,version,amount,reason in [
        ('plugin','team-tools','1.2.0',280,'原生孤立标记已存在；等待所属 Agent 回收'),
        ('plugin','team-tools','1.3.0',310,'安装登记或 Skill 链接仍引用；保留'),
        ('skill','review-changes','1.2.0',None,'插件内置 Skill，随父插件版本管理'),
        ('mcp','project-index','1.2.0',None,'插件内置 MCP，随父插件版本管理'),
        ('desktop-cache','Code Cache',None,140,'只读盘点；不进入常规清理')]:
        r=row('claude' if category!='desktop-cache' else 'codex',category,
              Path('/Users/demo/.claude/plugins/cache/team')/name/(version or 'cache'))
        r.update(name=name,version=version,directory_bytes=amount*1024**2 if amount else None,
                 reason=reason,source='team-market' if category!='desktop-cache' else '本机已知路径')
        rows.append(r)
    class ResourcePreview(ManagedApp):
        CSS=SHARED_CSS
        TITLE='Clean My Agent · 资源版本'
        resource_only=True
        def on_mount(self):
            self.push_screen(make_screen(dict(entries=rows,errors=[],totals_note='额外资源只读 · 内置 Skill / MCP 占用包含在父插件中，不重复计量')))
    app=ResourcePreview()
    async with app.run_test(size=(148,35)) as pilot:
        await pilot.pause()
        app.save_screenshot('resource-versions.svg',path=str(OUT))
asyncio.run(main())
