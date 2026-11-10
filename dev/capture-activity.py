"""Render activity screens from fictional metadata; no scans or file actions."""
import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop('NO_COLOR', None)
os.environ['COLORTERM'] = 'truecolor'
from clean_my_agent import activity
from clean_my_agent.policy import DAY_NS
from clean_my_agent.storage_tui import StorageApp


async def main():
    now = 1_794_312_000_000_000_000
    projects = []
    for name, days, gib, agent in [('docs-refresh',28,2.7,'codex'), ('auth-fix',18,4.1,'codex'),
            ('search-prototype',12,1.8,'claude'), ('api-tests',7,.6,'codex'),
            ('billing-ui',1,3.2,'codex'), ('legacy-import',None,1.3,'claude')]:
        projects.append(dict(path=f'/Users/demo/.{agent}/worktrees/{name}/app', agent=agent,
            directory_bytes=int(gib*1024**3), artifacts=3,
            activity=activity.classify(now-days*DAY_NS if days is not None else None, now_ns=now,
                                       basis='会话索引 / 工作目录文件修改')))
    report=dict(mode='measured', entries=[], projects=projects, errors=[], external_targets={})
    app=StorageApp(report,start_worktrees=True)
    async with app.run_test(size=(110,24)) as pilot:
        await pilot.pause()
        app.save_screenshot('activity-workspaces.svg',path=str(Path(__file__).resolve().parents[1]/'docs'))


asyncio.run(main())
