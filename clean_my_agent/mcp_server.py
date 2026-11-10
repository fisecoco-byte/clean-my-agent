"""Local stdio tools. The CLI remains the single implementation of cleanup policy."""
import argparse
import asyncio
from functools import wraps
import json
import os
from pathlib import Path
import signal
import sys
from typing import Annotated, Any, Literal, Optional

from . import vault
from .scan import layout
from .storage_view import ordered

Agent = Literal['all', 'codex', 'claude']
StorageAgent = Literal['all', 'codex', 'claude', 't3']
Age = Literal[5, 10, 15, 20]


async def _cli(*args):
    # -I prevents a client project's working directory from shadowing the installed package.
    command = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, '-I', '-m', 'clean_my_agent']
    proc = await asyncio.create_subprocess_exec(
        *command, *args, '--json',
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        try:
            if os.name == 'nt':
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    if not stdout or proc.returncode not in (0, 1):
        raise ValueError(stderr.decode(errors='replace').strip() or f'CLI exit {proc.returncode}')
    data = json.loads(stdout)
    # A partial write may already have a batch ID. Do not discard that result.
    if proc.returncode and not data.get('errors'):
        raise ValueError(stderr.decode(errors='replace').strip() or '命令未完成')
    return data


def _path(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('请使用盘点返回的产物绝对路径')
    return str(path)


def _list_batches(batch, limit):
    roots, state = layout()
    keys = [batch] if batch is not None else vault.batches(state)[::-1]
    rows = []
    for key in keys[:limit]:
        detail = {}
        n, size, errors = vault.process(state, key, roots, 'list', False, detail=detail)
        row = dict(batch=key, created=detail['created'], items=n, manifest_bytes=size, errors=errors)
        if batch is not None:
            row['files'] = [dict(path=p, bytes=b) for p, b in detail['files'][:limit]]
        rows.append(row)
    return dict(batches=rows, total_batches=len(keys))


def create_server(allow_stage=False):
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations
    from mcp.server.mcpserver.exceptions import ToolError
    from pydantic import Field

    Limit = Annotated[int, Field(ge=1, le=100)]
    Days = Annotated[int, Field(ge=1)]
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    server = MCPServer('clean-my-agent', instructions='本机存储报告；先预览。errors 非空表示存在不完整结果。暂存不释放空间。')

    def tool(hints):
        def register(fn):
            @wraps(fn)
            async def run(*args, **kwargs):
                try:
                    return await fn(*args, **kwargs)
                except (OSError, ValueError) as exc:
                    raise ToolError(str(exc)) from exc
            return server.tool(annotations=hints)(run)
        return register

    @tool(read)
    async def get_policy() -> dict[str, Any]:
        """读取当前生效的保留天数、保护路径及会话复核阈值。"""
        return await _cli('policy')

    @tool(read)
    async def scan_usage(agent: StorageAgent = 'all', full: bool = False) -> dict[str, Any]:
        """盘点各类别、候选容量及未展开目录数。完整模式较慢；依赖逐项明细见 inspect_dependencies。"""
        doc = await _cli('scan', '--agent', agent, '--full' if full else '--quick')
        deps = doc.pop('project_artifacts', doc.get('node_modules'))
        doc.pop('node_modules', None)
        if deps is not None:
            doc['dependency_summary'] = dict(mode=deps['mode'], count=len(deps['entries']),
                candidates=sum(r['status'] == 'candidate' for r in deps['entries']))
        for key in ('agent_resources', 'extensions'):
            resource = doc.get(key)
            if resource is not None:
                entries = resource.pop('entries')
                resource.update(total_entries=len(entries), entries=entries[:20], truncated=len(entries) > 20)
        return doc

    @tool(read)
    async def resource_inventory(agent: str = 'all', full: bool = False, limit: Limit = 20) -> dict[str, Any]:
        """盘点 Agent 缓存、旧安装版本、更新残留；候选不等于已授权清理。"""
        from .agent_resources import FAMILIES
        if agent not in ('all', *FAMILIES):
            raise ValueError('未知 Agent')
        doc = await _cli('resources', '--agent', agent, *([] if full else ['--quick']))
        rows = doc['entries']
        doc.update(entries=rows[:limit], total_entries=len(rows), truncated=len(rows)>limit)
        return doc

    @tool(read)
    async def extension_inventory(agent: Agent = 'all', full: bool = False, limit: Limit = 20) -> dict[str, Any]:
        """只读检测 Skill/MCP/Plugin 多版本及已知引用。原生回收建议不是删除授权；不输出 MCP 凭据。"""
        doc = await _cli('extensions', '--agent', agent, *([] if full else ['--quick']))
        rows = sorted(doc['entries'], key=lambda r: (not bool(r.get('duplicate_group')), r['name']))
        doc.update(entries=rows[:limit], total_entries=len(rows), truncated=len(rows) > limit)
        return doc

    @tool(read)
    async def inspect_dependencies(path: Optional[str] = None, candidates_only: bool = False,
                                   limit: Limit = 20) -> dict[str, Any]:
        """测量 Agent 多语言项目产物；列出共享占用、引用与保留原因，释放估计不能相加。"""
        doc = await _cli('deps', *([_path(path)] if path is not None else []))
        entries = doc['entries']
        rows = ordered([r for r in entries if not candidates_only or r['status'] == 'candidate'], 'candidate')
        return dict(mode=doc['mode'], total=len(entries), matched=len(rows), entries=rows[:limit],
                    projects=doc.get('projects', [])[:limit], total_projects=len(doc.get('projects', [])),
                    errors=doc['errors'])

    @tool(read)
    async def session_inventory(agent: Agent = 'all', archive_days: Optional[Age] = None,
                                delete_days: Optional[Age] = None, limit: Limit = 10) -> dict[str, Any]:
        """按 agent 返回会话分布、标题/摘要、ID 与复核建议。不会归档或删除会话。"""
        args = ['inventory', '--agent', agent, '--limit', str(limit)]
        for flag, value in (('--archive-days', archive_days), ('--delete-days', delete_days)):
            if value is not None:
                args.extend((flag, str(value)))
        doc = await _cli(*args)
        return {k: doc[k] for k in ('generated_at', 'agents', 'errors')}

    @tool(read)
    async def preview_clean(agent: Agent = 'all', days: Optional[Days] = None, limit: Limit = 20) -> dict[str, Any]:
        """预览已知缓存及旧日志，files 只返回前 limit 项，eligible_files 为所有匹配项。"""
        doc = await _cli('clean', '--agent', agent, '--details', *(['--days', str(days)] if days else []))
        doc['files'] = doc['files'][:limit]
        return doc

    @tool(read)
    async def list_batches(batch: Optional[str] = None, limit: Limit = 20) -> dict[str, Any]:
        """列出最近批次；指定批次返回原路径和清单容量，不代表可释放量。"""
        return await asyncio.to_thread(_list_batches, batch, limit)

    if allow_stage:
        @tool(write)
        async def stage_resource(path: str) -> dict[str, Any]:
            """重新核查并暂存一个用户已确认的 Agent 资源候选；暂存释放 0 B。"""
            return await _cli('resources', _path(path), '--apply')

        @tool(write)
        async def stage_clean(agent: Literal['codex', 'claude'], days: Optional[Days] = None) -> dict[str, Any]:
            """按确认过的 agent 和天数重新检查并暂存所有匹配的缓存及旧日志；不是仅操作预览前几行。"""
            return await _cli('clean', '--agent', agent, '--apply', *(['--days', str(days)] if days else []))

        @tool(write)
        async def stage_dependency(path: str) -> dict[str, Any]:
            """重新核实并暂存一个已确认停用的依赖候选。返回批次编号；暂存释放 0 B。"""
            return await _cli('deps', _path(path), '--apply')

        @tool(write)
        async def restore_batch(batch: str) -> dict[str, Any]:
            """按批次还原，不覆盖新文件。部分成功时查看 errors 和剩余批次。"""
            roots, state = layout()
            n, size, errors = await asyncio.to_thread(vault.process, state, batch, roots, 'restore', True)
            return dict(batch=batch, restored_items=n, manifest_bytes=size, errors=errors)
    return server


def main(argv=None):
    p = argparse.ArgumentParser(description='Clean My Agent 本地 stdio MCP；默认只读。')
    p.add_argument('--allow-stage', action='store_true', help='另外开放可恢复的暂存与还原，不开放永久清除')
    opts = p.parse_args(argv)
    if os.name == 'nt' and opts.allow_stage:
        p.error('Windows 原生版当前仅支持只读 MCP')
    try:
        server = create_server(opts.allow_stage)
    except ModuleNotFoundError as exc:
        if exc.name != 'mcp':
            raise
        p.exit(2, "需要 Python 3.10+ 并安装本项目的 [mcp] 可选依赖。\n")
    server.run(transport='stdio')


if __name__ == '__main__':
    main()
