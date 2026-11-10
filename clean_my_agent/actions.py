"""Actions behind explicit terminal confirmations; never mutate from a report alone."""
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess

from . import dependency_report, inventory, policy as policies, vault, worktrees
from .scan import fingerprint


class DependencyActions:
    def __init__(self, roots, state, policy):
        self.roots, self.state, self.policy = roots, state, policy

    def quarantine(self):
        from .quarantine_actions import QuarantineActions
        return QuarantineActions(self.roots, self.state)

    def worktree_details(self, selected, progress):
        return worktrees.inspect(self.roots,self.state,self.policy,selected,progress)

    def delete_worktree(self, expected, progress):
        return worktrees.remove(self.roots,self.state,self.policy,expected,progress)

    def stage_many(self, selected, progress):
        if not selected or len({r['path'] for r in selected}) != len(selected):
            raise ValueError('请选择不重复的产物候选')
        self.policy.require_current()
        items = []
        for n, old in enumerate(selected):
            path = Path(old['path']); source = old.get('agent','codex')
            if old['status'] != 'candidate' or old['kind'] != 'directory':
                raise ValueError('选择中含不可暂存的产物')
            if any(path != Path(other['path']) and path in Path(other['path']).parents for other in selected):
                raise ValueError('不能同时选择父目录与其子产物')
            progress('重新核对所选产物', n, len(selected), str(path))
            report = dependency_report.inspect(self.roots[source],self.policy,source=source,only=path,progress=progress)
            row = report['entries'][0]
            if report['errors'] or row['status'] != 'candidate' or tuple(row['stamp']) != tuple(old['stamp']):
                raise ValueError('所选产物已变化，未暂存：'+str(path)+' · '+(row.get('reason') or '请刷新'))
            items.append(dict(source=source,root=str(self.roots[source]),path=str(path.relative_to(self.roots[source])),
                              category='dependencies',size=row['directory_bytes'],stamp=row['stamp'],project=row['project']))
        key,count,_,errors = vault.save(items,self.roots,self.state,self.policy)
        moved = []
        if key:
            with vault.batch_lock(self.state,key) as fd:
                data = vault.manifest(fd)
                names = set(os.listdir(fd))
                moved = [str(Path(item['root'])/item['path']) for i,item in enumerate(data['items']) if str(i) in names]
        return dict(batch=key,moved_items=count,moved_paths=moved,errors=errors)

    def stage(self, selected, progress):
        path = Path(selected['path'])
        self.policy.require_current()
        if selected['status'] != 'candidate' or selected['kind'] != 'directory':
            raise ValueError('只能暂存候选中的真实依赖目录')
        if tuple(selected.get('stamp', ())) != fingerprint(path.lstat()):
            raise ValueError('目录已发生变化，请退出面板重新扫描')
        source = selected.get('agent', 'codex')
        report = dependency_report.inspect(self.roots[source], self.policy, source=source, only=path, progress=progress)
        row = report['entries'][0]
        result = dict(row=row, batch=None, moved_items=0, errors=report['errors'][:])
        if report['errors'] or row['status'] != 'candidate':
            result['errors'].append(row.get('reason') or '引用盘点不完整，未暂存')
            return result
        if tuple(row['stamp']) != tuple(selected['stamp']):
            raise ValueError('目录在核查期间发生变化，请重新扫描')
        item = dict(source=source, root=str(self.roots[source]),
                    path=str(path.relative_to(self.roots[source])), category='dependencies',
                    size=row['directory_bytes'], stamp=tuple(row['stamp']), project=row['project'])
        progress('最终核对引用并暂存', detail=str(path))
        batch, count, _, errors = vault.save([item], self.roots, self.state, self.policy)
        result.update(batch=batch, moved_items=count)
        result['errors'].extend(errors)
        return result


class ResourceActions:
    def __init__(self, roots, state, policy):
        self.roots, self.state, self.policy = roots, state, policy

    def quarantine(self):
        from .quarantine_actions import QuarantineActions
        return QuarantineActions(self.roots, self.state)

    def stage(self, selected, progress):
        from . import agent_resources
        path = Path(selected['path'])
        self.policy.require_current()
        if selected['status'] != 'candidate' or not selected.get('can_stage'):
            raise ValueError('请选择符合策略的资源候选')
        fresh = agent_resources.inspect(self.roots, {selected['agent']}, only=path,
                                        policy=self.policy, progress=progress)
        row = fresh['entries'][0]
        result = dict(row=row, batch=None, moved_items=0, errors=fresh['errors'][:])
        if row['status'] != 'candidate' or tuple(row['stamp']) != tuple(selected['stamp']):
            result['errors'].append(row.get('reason') or '资源已变化')
            return result
        base = self.roots.get(row['agent'], Path.home())
        if base not in path.parents:
            base = Path.home()
        item = dict(source=row['agent'], root=str(base), path=str(path.relative_to(base)),
                    category='agent-resources', kind=row['kind'], size=row['directory_bytes'], stamp=row['stamp'])
        progress('再次核对资源并暂存', detail=str(path))
        key, count, _, errors = vault.save([item], self.roots, self.state, self.policy)
        result.update(batch=key, moved_items=count)
        result['errors'].extend(errors)
        return result


class SessionActions:
    writes_enabled = os.name != 'nt'
    def __init__(self, roots, *, agent='all', limit=10, full=False):
        self.roots, self.agent, self.limit, self.full = roots, agent, limit, full

    def refresh(self, rules, progress=None):
        doc, rows = inventory.inspect(self.roots, policies.load(), agent=self.agent,
                                      limit=self.limit, full=self.full, progress=progress)
        for agent, data in doc['agents'].items():
            data['policy'] = dict(rules.get(agent, data['policy']))
            data['recommendations'] = inventory.plan(rows[agent], **data['policy'], limit=self.limit)
        return doc, rows

    def apply(self, operation, selected, rules, progress):
        if not self.writes_enabled:
            from .portable import READ_ONLY
            raise ValueError(READ_ONLY)
        if operation not in ('archive', 'delete') or not selected:
            raise ValueError('请选择归档或删除候选')
        threshold = rules['codex'][operation + '_days']
        if threshold not in policies.SESSION_DAYS:
            raise ValueError('未识别的复核天数')
        ids = [r['session_id'] for r in selected]
        if len(ids) != len(set(ids)) or any(not re.fullmatch(inventory.UUID, s) for s in ids):
            raise ValueError('请选择完整且不重复的会话 UUID')
        program = shutil.which('codex')
        if not program:
            raise ValueError('未找到 Codex CLI，请安装后加入 PATH')
        root = self.roots['codex']
        env = {**os.environ, 'CODEX_HOME': str(root)}
        progress('检查 Codex 原生命令')
        help_result = subprocess.run([program, operation, '--help'], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors='replace', env=env, cwd=root, timeout=15)
        if (help_result.returncode or 'Usage:' not in help_result.stdout
                or (operation == 'delete' and '--force' not in help_result.stdout)):
            raise ValueError('当前 Codex CLI 不支持所需的原生命令，请先更新 Codex')
        results = []
        stopped = False
        for n, old in enumerate(selected):
            sid = old['session_id']
            result = dict(session_id=sid, title=old['title'], action=operation, status='skipped', message='')
            results.append(result)
            if stopped:
                result['message'] = '前一操作未能确认，未执行这一项'
                continue
            progress('核对并执行会话操作', n, len(selected), sid)
            launched = False
            try:
                doc, rows = inventory.inspect(self.roots, policies.load(), agent='codex', **rules['codex'])
                current = next((r for r in rows['codex'] if r['session_id'] == sid), None)
                if (doc['errors'] or current is None or current['route'] != operation
                        or current['idle_days'] < threshold or old.get('latest_ns') is None
                        or current['latest_ns'] != old['latest_ns'] or current['state'] != old['state']):
                    result['message'] = '会话活动、状态或保护规则已变化，未执行；请刷新复核'
                    continue
                # --force only suppresses the native deletion prompt AFTER the UI's
                # explicit DELETE confirmation. Only validated UUIDs are accepted.
                argv = [program, operation, sid] + (['--force'] if operation == 'delete' else [])
                launched = True
                try:
                    command = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                        text=True, errors='replace', env=env, cwd=root, timeout=90)
                except subprocess.TimeoutExpired:
                    result.update(status='uncertain', message='Codex 命令超时；状态未确认，请刷新核对，勿直接重试')
                    stopped = True
                    continue
                if command.returncode:
                    result.update(status='failed', message=(command.stderr or command.stdout or 'Codex 返回失败')[-2000:])
                    stopped = True
                    continue
                # Verify the observed native outcome; a successful subprocess alone
                # does not justify removing a row from the user's view.
                after, fresh = inventory.inspect(self.roots, policies.load(), agent='codex', **rules['codex'])
                changed = next((r for r in fresh['codex'] if r['session_id'] == sid), None)
                verified = (changed is not None and changed['state'] == 'archived') if operation == 'archive' else changed is None
                if operation == 'delete' and verified:
                    index, _ = inventory.thread_index(root)
                    verified = sid not in index
                if not after['errors'] and verified:
                    result.update(status='succeeded', message='已归档' if operation == 'archive' else '已永久删除')
                else:
                    result.update(status='uncertain', message='命令已返回，但本地状态尚未确认；请刷新核对，勿直接重试')
                    stopped = True
            except (OSError, ValueError, sqlite3.Error) as exc:
                result.update(status='uncertain' if launched else 'failed', message=str(exc))
                stopped = True
        progress('更新会话列表', len(selected), len(selected))
        return dict(results=results)
