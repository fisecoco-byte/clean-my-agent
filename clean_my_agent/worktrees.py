"""Checked deletion of registered linked worktrees through Git, never rmtree."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

from . import deps, project_types, vault
from .dependency_safety import ProcessSnapshot, regular_text
from .scan import fingerprint


def git(directory, *args, timeout=30):
    env = {k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    return subprocess.run(['git', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
                           '-C', str(directory), *args], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=timeout)


def checked_git(directory, *args):
    result = git(directory, *args)
    if result.returncode:
        raise ValueError('Git 检查失败：'+os.fsdecode(result.stderr[-1200:]).strip())
    return result.stdout


def git_path(path, option):
    raw = checked_git(path, 'rev-parse', option).removesuffix(b'\n')
    result = Path(os.fsdecode(raw))
    return (result if result.is_absolute() else path/result).resolve(strict=True)


def registered(path):
    # Shares the strict parser and legacy Git fallback used by dependency checks.
    return deps.worktree_paths(path)


def contains(parent, child):
    return parent == child or parent in child.parents


def scope(roots, source, path, policy):
    root = roots.get(source)
    if (source not in ('codex','claude','t3') or root is None or not path.is_absolute()
            or '..' in path.parts or root not in path.parents
            or len(path.relative_to(root).parts) < 2 or path.relative_to(root).parts[0] not in deps.AREAS):
        raise ValueError('工作树不在已识别的 Agent 项目范围内')
    if path.resolve(strict=True) != path or not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError('工作树路径含链接或不是目录')
    if policy.protected(source, path.relative_to(root), tree=True):
        raise ValueError('工作树或其子路径受策略保护')
    return root


def codex_references(root, path):
    """Read known workspace and thread metadata, never transcripts or credentials."""
    result = []
    state = root/'.codex-global-state.json'
    if state.exists() or state.is_symlink():
        data = json.loads(regular_text(state)[0])
        if not isinstance(data, dict):
            raise ValueError('Codex 工作区登记无法解析')
        for key in ('electron-saved-workspace-roots', 'active-workspace-roots'):
            values = data.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError('Codex 工作区登记格式未知')
            for value in values:
                if not Path(value).is_absolute():
                    raise ValueError('Codex 工作区登记路径不是绝对路径')
                p = Path(value).resolve()
                if contains(path, p) or (key == 'active-workspace-roots' and contains(p, path)):
                    result.append(dict(kind='workspace', path=value, blocking=True, title='Codex 工作区'))
    dbs = [(int(m[1]), p) for p in root.glob('state_*.sqlite') if (m := re.fullmatch(r'state_(\d+)\.sqlite', p.name))]
    if not dbs:
        return result
    dbpath = max(dbs)[1]
    if dbpath.is_symlink() or dbpath.resolve() != dbpath:
        raise ValueError('Codex 会话索引路径异常')
    wal, shm, journal = [Path(str(dbpath)+s) for s in ('-wal','-shm','-journal')]
    if any(p.is_symlink() for p in (wal,shm,journal)) or journal.exists() or wal.exists()!=shm.exists():
        raise ValueError('Codex 会话索引正在切换或无法安全读取')
    before = fingerprint(dbpath.lstat())
    no_wal = not wal.exists()
    uri = dbpath.as_uri()+('?mode=ro&immutable=1' if no_wal else '?mode=ro')
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
        db.execute('PRAGMA query_only=ON'); db.execute('PRAGMA trusted_schema=OFF')
        cols = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
        if not {'id','cwd','archived'} <= cols:
            raise ValueError('Codex 会话索引缺少工作目录信息；引用未知')
        label = 'substr(title,1,4096)' if 'title' in cols else 'NULL'
        pin = 'is_pinned' if 'is_pinned' in cols else '0'
        for sid, cwd, archived, pinned, title in db.execute(f'SELECT id,cwd,archived,{pin},{label} FROM threads'):
            if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                raise ValueError('Codex 会话工作目录格式未知')
            if contains(path, Path(cwd).resolve()):
                result.append(dict(kind='session', id=sid, path=cwd, title=title or sid,
                                   blocking=archived != 1 or bool(pinned)))
    if no_wal and (wal.exists() or journal.exists() or fingerprint(dbpath.lstat()) != before):
        raise ValueError('Codex 会话索引已更新，请重新核查')
    return result


def ignored_artifacts(path, root, progress):
    raw = checked_git(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z')
    entries = [os.fsdecode(p).rstrip('/') for p in raw.split(b'\0') if p]
    for n, name in enumerate(entries):
        if progress:
            progress('核对忽略文件', n, len(entries), name)
        p = path/name
        if not contains(path, p) or '..' in Path(name).parts or Path(name).is_absolute():
            raise ValueError('忽略路径无法核实')
        if p.is_symlink() and p.name in project_types.TARGETS:
            continue  # Git removes only this outgoing link, not its target.
        if not p.is_dir() or not project_types.target(p):
            raise ValueError('存在不能自动丢弃的忽略文件：'+name)
        deps.verify(root, p)
        _, project = deps.project_context(p)
        project_types.require_evidence(p, p, project)
    return entries


def inspect(roots, state, policy, selected, progress=None):
    path, source = Path(selected['path']), selected['agent']
    result = dict(path=str(path), agent=source, directory_bytes=selected.get('directory_bytes'),
                  can_delete=False, reasons=[], checks={}, referenced_by=[], ignored=[], changes=[],
                  native_command=None, token=None)
    if os.name == 'nt':
        result['reasons'].append('Windows 当前只读盘点')
        return result
    def check(name, fn):
        if progress:
            progress(name, detail=str(path))
        try:
            value = fn()
            result['checks'][name] = '通过'
            return value
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            result['checks'][name] = str(exc)
            result['reasons'].append(str(exc))
            return None
    def identity():
        root = scope(roots, source, path, policy)
        expected = selected.get('stamp')
        info = path.lstat()
        if expected and tuple(expected[:2]) != (info.st_dev,info.st_ino):
            raise ValueError('工作树已被替换，请重新盘点')
        if git_path(path, '--show-toplevel') != path:
            raise ValueError('该路径不是仓库根目录')
        pointer = path/'.git'
        if pointer.is_symlink() or not pointer.is_file():
            raise ValueError('主工作区或独立仓库保留；仅删除附加 worktree')
        common, gitdir = git_path(path,'--git-common-dir'), git_path(path,'--git-dir')
        if gitdir == common or gitdir.parent != common/'worktrees' or contains(path, common):
            raise ValueError('不是标准附加 worktree，或仓库元数据位于目标内')
        if path not in registered(path):
            raise ValueError('Git 未登记此工作树')
        if (gitdir/'locked').exists() or (gitdir/'locked').is_symlink():
            raise ValueError('Git 工作树已锁定，保留')
        for name in ('index.lock','HEAD.lock','MERGE_HEAD','CHERRY_PICK_HEAD','REVERT_HEAD','rebase-merge','rebase-apply','sequencer','BISECT_START'):
            if (gitdir/name).exists() or (gitdir/name).is_symlink():
                raise ValueError('工作树有未完成的 Git 操作：'+name)
        head = checked_git(path,'rev-parse','--verify','HEAD').strip().decode('ascii')
        branch_result = git(path,'symbolic-ref','--quiet','HEAD')
        if branch_result.returncode not in (0,1):
            raise ValueError('无法核实工作树分支')
        branch = os.fsdecode(branch_result.stdout.strip()) if not branch_result.returncode else None
        preserved = checked_git(path,'for-each-ref','--contains=HEAD','--format=%(refname)',
                                'refs/heads','refs/remotes','refs/tags').splitlines()
        if not preserved:
            raise ValueError('HEAD 提交没有被分支或标签保留；请先保存提交')
        result.update(stamp=fingerprint(info), head=head, branch=branch, common_dir=str(common), git_dir=str(gitdir))
        result['native_command'] = ['git','-C',str(common),'worktree','remove','--',str(path)]
        return root
    root = check('工作树身份', identity)
    if root is None:
        return result
    def changes():
        raw = checked_git(path,'status','--porcelain=v1','-z','--untracked-files=all','--ignore-submodules=none')
        result['changes'] = [os.fsdecode(p) for p in raw.split(b'\0') if p]
        if raw:
            raise ValueError('存在未提交改动或未跟踪文件')
        staged = checked_git(path,'ls-files','--stage','-z')
        if any(p.startswith(b'160000 ') for p in staged.split(b'\0')):
            raise ValueError('包含子模块，保留')
    check('未提交改动', changes)
    def running():
        if contains(path, Path.cwd().resolve()):
            raise ValueError('当前工具正在该工作树内运行')
        runtime = ProcessSnapshot().check(path, project=path)
        result['runtime'] = runtime
        if runtime['state'] != 'idle':
            raise ValueError(runtime['reason'])
    check('运行状态', running)
    def references():
        refs = set()
        for agent in ('codex','claude','t3'):
            index = deps.References(roots[agent], progress=progress)
            if index.errors:
                raise ValueError('工作树引用检查不完整：'+index.errors[0])
            refs.update(str(p) for p in index.incoming(path))
            refs.update(str(p) for p,target in index.targets.items()
                        if contains(target,path) and not contains(path,p))
        result['referenced_by'] = sorted(refs)
        result['agent_references'] = codex_references(roots['codex'],path)
        if refs or any(r['blocking'] for r in result['agent_references']):
            raise ValueError('仍被其他工作树、Codex 工作区或未归档会话引用')
        for key in vault.batches(state):
            detail = {}
            _,_,errors = vault.process(state,key,roots,'list',False,detail=detail)
            if errors:
                raise ValueError('暂存区引用无法完整核实')
            if any(contains(path,Path(p)) for p,_ in detail['files']):
                raise ValueError('仍有待恢复或清除的产物批次：'+key)
    check('引用关系', references)
    def ignored():
        deps.check_mounts(path)
        result['ignored'] = ignored_artifacts(path,root,progress)
        for base, dirs, names in os.walk(path, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
            if Path(base) != path and ('.git' in dirs or '.git' in names):
                raise ValueError('包含嵌套仓库，保留')
            dirs[:] = [d for d in dirs if d != '.git']
        return project_types.tree_digest(path)
    digest = check('忽略文件与目录', ignored)
    if os.name == 'nt':
        result['reasons'].append('Windows 当前只读盘点')
    if not result['reasons']:
        proof = [result[k] for k in ('path','agent','stamp','head','branch','common_dir','git_dir')]+[digest]
        result['token'] = hashlib.sha256(json.dumps(proof,ensure_ascii=True).encode()).hexdigest()
        result['can_delete'] = True
    return result


def remove(roots, state, policy, expected, progress=None):
    if not expected.get('can_delete') or not expected.get('token'):
        raise ValueError('请先核对一个可删除的工作树')
    policy.require_current()
    current = inspect(roots,state,policy,expected,progress)
    if not current['can_delete'] or current['token'] != expected['token']:
        raise ValueError('工作树状态已变化，未删除：'+('；'.join(current['reasons']) or '目录、分支或文件有变化'))
    policy.require_current()
    path = Path(current['path']); common = Path(current['common_dir'])
    if fingerprint(path.lstat()) != tuple(current['stamp']):
        raise ValueError('工作树在执行前发生变化，未删除')
    if progress:
        progress('通过 Git 删除工作树', detail=str(path))
    try:
        command = git(common,'worktree','remove','--',str(path),timeout=120)
    except subprocess.TimeoutExpired:
        return dict(status='uncertain',path=str(path),message='Git 删除超时，结果未确认；请重新盘点，勿直接重试')
    try:
        gone = not path.exists() and not path.is_symlink() and path not in registered(common)
    except (OSError,ValueError,subprocess.TimeoutExpired):
        gone = False
    if command.returncode == 0 and gone:
        return dict(status='succeeded',path=str(path),message='已通过 Git 删除工作树；分支和会话记录保留')
    return dict(status='failed' if path.exists() else 'uncertain',path=str(path),
                message=os.fsdecode(command.stderr[-2000:]).strip() or '删除结果未确认，请重新盘点')
