"""Explicit cleanup of ignored, rebuildable artifacts in Agent-managed checkouts."""
import ctypes
import errno
import os
import re
import stat
from collections import defaultdict
import subprocess
import time
import sys
from pathlib import Path

from .policy import DAY_NS
from .progress import counted
from .dependency_safety import require_idle
from . import project_types

AREAS = ('worktrees', '.chatgpt-projects', 'projects')
LOCKS = ('package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'yarn.lock', 'bun.lock', 'bun.lockb')


def valid_location(root, path, *, cache_tag=False):
    return (path.is_absolute() and root in path.parents and '..' not in path.parts
            and path.relative_to(root).parts[0] in AREAS and (project_types.target(path) or cache_tag))


def git(repo, *args):
    # An inherited GIT_DIR or GIT_INDEX_FILE must not redirect safety checks.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    try:
        return subprocess.run(['git', '-C', str(repo), *args], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f'Git 检查超时，保留：{repo}') from exc


def worktree_paths(repo):
    result = git(repo, 'worktree', 'list', '--porcelain', '-z')
    nul = result.returncode == 0
    if not nul:  # Stock Ubuntu 22.04 Git predates porcelain -z.
        result = git(repo, 'worktree', 'list', '--porcelain')
    if result.returncode:
        raise ValueError(f'无法核实 Git 登记的工作树：{repo}')
    paths = []
    separator = b'\0' if nul else b'\n'
    for record in result.stdout.split(separator*2):
        if not record.strip(separator):
            continue
        fields = record.split(separator)
        if not fields[0].startswith(b'worktree '):
            raise ValueError(f'无法解析 Git 工作树清单：{repo}')
        if not nul and not re.fullmatch(
                rb'worktree [^\n]+\n(?:bare|HEAD [0-9a-f]{40,64}\n(?:branch refs/[^\n]+|detached))'
                rb'(?:\nlocked(?: [^\n]*)?)?(?:\nprunable(?: [^\n]*)?)?', record):
            # Older Git emits raw path bytes. A newline in a path cannot be
            # interpreted reliably; retain dependencies rather than lose links.
            raise ValueError(f'旧版 Git 无法可靠解析工作树路径，请升级 Git：{repo}')
        raw = fields[0][9:]
        path = Path(os.fsdecode(raw))
        if not path.is_absolute():
            raise ValueError(f'Git 工作树路径不是绝对路径：{repo}')
        if b'bare' not in fields:
            paths.append(path)
    if not result.stdout:
        raise ValueError(f'Git 工作树清单为空：{repo}')
    return paths


def project_context(path):
    top = git(path.parent, 'rev-parse', '--show-toplevel')
    if top.returncode:
        raise ValueError('未找到所属 Git 仓库；保留')
    repo = Path(os.fsdecode(top.stdout.removesuffix(b'\n')))
    if not repo.is_absolute() or repo not in path.parents:
        raise ValueError('无法核实所属仓库')
    return repo, project_types.context(path, repo)


def verify(root, path):
    if not valid_location(root, path) or path.resolve(strict=True) != path or not path.is_dir():
        raise ValueError('只能选择 Agent 工作树或项目中已识别的真实生成目录')
    repo, project = project_context(path)
    if path.name == 'node_modules':
        files = git(project, 'ls-files', '-z', '--', 'package.json', *LOCKS)
        tracked = set(files.stdout.split(b'\0'))
        if (files.returncode or b'package.json' not in tracked or not (project / 'package.json').is_file()
                or not any(os.fsencode(f) in tracked and (project / f).is_file() for f in LOCKS)):
            raise ValueError('缺少 Git 跟踪的 package.json 或依赖锁文件')
    else:
        description = project_types.describe(path, project)
        for name in description['evidence']:
            if git(repo, 'ls-files', '--error-unmatch', '--', str(Path(name).relative_to(repo))).returncode:
                raise ValueError('项目清单未被 Git 跟踪；保留')
    rel = str(path.relative_to(repo))
    if git(repo, 'check-ignore', '-q', '--', rel+'/').returncode != 0:
        raise ValueError('生成目录未被 Git 忽略')
    files = git(repo, 'ls-files', '--cached', '-z', '--', rel)
    if files.returncode or files.stdout:
        raise ValueError('目录内有 Git 跟踪的文件，或无法检查 Git 索引')


class Retain(ValueError):
    """A known policy reason to retain the entire dependency tree."""


def check_policy(root, path, policy, source='codex'):
    if not valid_location(root, path):
        raise ValueError('只支持 Agent 工作树内已识别的产物绝对路径')
    if policy.protected(source, path.relative_to(root), tree=True):
        raise Retain('命中保护路径')
    verify(root, path)
    check_mounts(path)


def eligible(root, path, policy, source='codex'):
    check_policy(root, path, policy, source)
    References(root, nested=False).guard(path)
    days = policy.retention['dependencies']
    cutoff = time.time_ns() - days * DAY_NS
    _, project = project_context(path)
    installation = project_types.Evidence(path, project=project)

    def fail(exc):
        raise exc

    device = path.lstat().st_dev

    def check(s):
        if s.st_dev != device:
            raise Retain('包含其他挂载卷')
        if s.st_mtime_ns > cutoff:
            raise Retain(f'最近 {days} 天有改动')

    for base, dirs, files, fd in os.fwalk(path, follow_symlinks=False, onerror=fail):
        check(os.fstat(fd))
        if '.git' in dirs or '.git' in files:
            raise Retain('包含嵌套的 Git 仓库')
        for name in dirs + files:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            check(info)
            installation.observe((Path(base) / name).relative_to(path), info)
    evidence = installation.finish()
    if evidence['state'] != 'verified':
        raise Retain(evidence['reason'])
    # The age walk can take time. Check all reverse links again just before the move.
    References(root).guard(path)
    require_idle(path, project=project)


class References:
    """Find installations and reverse links; never walk installed packages or .git."""
    def __init__(self, root, *, nested=True, progress=None):
        self.paths, self.targets, self.errors = set(), {}, []
        self.by_identity = defaultdict(set)
        self.by_path = defaultdict(set)
        repos = set()
        self.repositories = repos
        def fail(exc):
            self.errors.append(str(exc))
        if root.resolve() != root:
            self.errors.append(f'数据根目录含符号链接：{root}')
            return
        if progress:
            progress('查找工作树' if nested else '查找依赖目录')
        for area in AREAS:
            start = root / area
            try:
                mode = start.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                fail(exc)
                continue
            if not stat.S_ISDIR(mode):
                self.errors.append(f'无法核实符号链接目录：{start}')
                continue
            for base, dirs, files in os.walk(start, onerror=fail):
                parent = Path(base)
                if '.git' in dirs or '.git' in files:
                    repos.add(parent)
                for name in dirs + files:
                    path = parent / name
                    if name != '.git' and (name in project_types.TARGETS or (name in dirs and project_types.tagged(path))):
                        self.paths.add(path)
                        if name in dirs:
                            dirs.remove(name)
                        if name == '.build' and path.is_dir() and not path.is_symlink():
                            self.paths.update(path / child for child in project_types.SWIFT_OUTPUTS if (path / child).is_dir())
                dirs[:] = [d for d in dirs if d not in ('.git', '.Trash')]
        checkouts = set()
        for repo in sorted(repos):
            if repo in checkouts:
                continue
            try:
                checkouts.update(worktree_paths(repo))
            except ValueError as exc:
                self.errors.append(str(exc))
        sources = self.paths | {p / name for p in checkouts for name in project_types.TARGETS}
        if nested:
            live = []
            for checkout in repos | checkouts:
                try:
                    directory = checkout.resolve(strict=True)
                    if not directory.is_absolute() or directory in (Path('/'), Path.home()):
                        self.errors.append(f'工作树范围过大，无法核实引用：{checkout}')
                        continue
                    (directory / '.git').lstat()
                except FileNotFoundError:  # Git retains registrations for removed checkouts.
                    continue
                except OSError as exc:
                    fail(exc)
                    continue
                live.append(str(directory))
            # Native find prunes installs before descending; -P also leaves all
            # other symlinks alone. NUL framing preserves arbitrary path names.
            # One checkout per update in an interactive scan. For unattended
            # runs keep the original batched find invocation.
            live.sort()
            batch_size = 1 if progress else 64
            batches = [live[i:i+batch_size] for i in range(0, len(live), batch_size)]
            for batch in counted(batches, progress, '复核工作树引用', lambda b: '/'.join(Path(b[0]).parts[-2:])):
                names = []
                for name in sorted(project_types.TARGETS):
                    if names: names.append('-o')
                    names.extend(('-name', name))
                result = subprocess.run(['find', '-P', *batch, '-type', 'l', '-print0', '-o',
                                         '(', *names, '-o', '-name', '.git', ')', '-prune'],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                sources.update(Path(os.fsdecode(p)) for p in result.stdout.split(b'\0') if p)
                if result.returncode:
                    self.errors.append('子项目引用检查不完整：' + os.fsdecode(result.stderr[:1500]))
        if progress:
            progress('核对依赖链接')
        for path in sorted(sources):
            try:
                if not stat.S_ISLNK(path.lstat().st_mode):
                    continue
                target, hops = link_route(path)
                self.targets[path] = target
                # An intermediate link itself can live inside an installation
                # even when the final destination lies somewhere else.
                ancestors = {p for hop in [target, *hops] for p in (hop, *hop.parents)}
                for ancestor in ancestors:
                    self.by_path[ancestor].add(path)
                    try:
                        st = ancestor.stat()
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    self.by_identity[st.st_dev, st.st_ino].add(path)
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError) as exc:
                self.errors.append(str(exc))

    def incoming(self, path):
        incoming = self.by_path.get(path, set()).copy()
        try:
            s = path.lstat()
        except FileNotFoundError:
            pass
        else:
            incoming.update(self.by_identity.get((s.st_dev, s.st_ino), ()))
        return sorted(p for p in incoming if p != path and path not in p.parents)

    def guard(self, path):
        callers = self.incoming(path)
        if callers:
            origin = '其他工作树' if any(path.parent not in p.parents for p in callers) else '本工作树子项目'
            raise Retain(f'被{origin}引用：' + '、'.join(map(str, callers)))
        if self.errors:
            raise Retain('工作树引用盘点不完整，保留目标：' + self.errors[0])


def link_route(source):
    """Resolve relative links and .. in traversal order, preserving dangling destinations."""
    current = source.parent.resolve(strict=True)
    todo = [source.name]
    hops = []
    while todo:
        part = todo.pop()
        if part == '..':
            current = current.parent
            continue
        current = current / part
        try:
            target = Path(os.readlink(current))
        except OSError as exc:
            if exc.errno in (errno.EINVAL, errno.ENOENT, errno.ENOTDIR):
                continue
            raise
        hops.append(current)
        if len(hops) > 40:
            raise OSError(errno.ELOOP, '依赖链接链过长或存在环', str(source))
        parts = target.parts
        if target.is_absolute():
            current, parts = Path(target.anchor), parts[1:]
        else:
            current = current.parent
        todo.extend(reversed(parts))
    return current, hops[1:]


def usage(path):
    if os.name == 'nt':
        from .portable import measure
        return measure(path)[0]['directory_bytes']
    p = subprocess.run(['du', '-k', '-s', '-x', str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise ValueError(f'无法完整统计 {path}')
    return int(p.stdout.split(b'\t', 1)[0]) * 1024


def rename_exclusive(srcfd, src, dstfd, dst):
    """Atomic restore of a directory without replacing even an existing empty directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    func = getattr(libc, 'renameatx_np' if sys.platform == 'darwin' else 'renameat2', None)
    if func is None:
        raise OSError(errno.ENOTSUP, '此系统不支持安全还原目录')
    flag = 4 if sys.platform == 'darwin' else 1  # RENAME_EXCL / RENAME_NOREPLACE
    if func(srcfd, os.fsencode(src), dstfd, os.fsencode(dst), flag):
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), dst)


def empty_tree(parent, name, device):
    """Never follow a symlink or descend onto another device during a batch purge."""
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if os.fstat(fd).st_dev != device:
            raise ValueError('目录包含其他挂载卷，已停止')
        with os.scandir(fd) as entries:
            for e in entries:
                if e.is_dir(follow_symlinks=False):
                    empty_tree(fd, e.name, device)
                else:
                    os.unlink(e.name, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)


def check_mounts(tree):
    # A Linux bind mount may have the same st_dev as its parent.
    if sys.platform.startswith('linux'):
        with open('/proc/self/mountinfo', 'rb') as f:
            for line in f:
                field = line.split()[4]
                raw = re.sub(rb'\\([0-7]{3})', lambda m: bytes([int(m[1], 8)]), field)
                mount = Path(os.fsdecode(raw))
                if tree == mount or tree in mount.parents:
                    raise ValueError('暂存项内有挂载点，拒绝清除')
