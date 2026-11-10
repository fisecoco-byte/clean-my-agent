"""Read-only evidence for installed dependencies and processes using a project."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import unicodedata

from .scan import fingerprint
from .portable import open_read


def regular_text(path, limit=8 * 1024 * 1024):
    if path.resolve(strict=True) != path:
        raise ValueError('元数据路径含符号链接')
    fd = open_read(path)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('元数据不是普通文件或超过读取上限')
        data = stream.read(limit + 1)
        if len(data) > limit or fingerprint(before) != fingerprint(os.fstat(stream.fileno())):
            raise ValueError('读取期间元数据发生变化')
    return data.decode('utf-8'), fingerprint(before)


class Installation:
    """mtime evidence, not a claim of cryptographic package integrity."""
    PAIRS = (('npm', 'package-lock.json', '.package-lock.json'),
             ('npm', 'npm-shrinkwrap.json', '.package-lock.json'),
             ('pnpm', 'pnpm-lock.yaml', '.modules.yaml'),
             ('yarn', 'yarn.lock', '.yarn-integrity'),
             ('yarn', 'yarn.lock', '.yarn-state.yml'))

    def __init__(self, path, *, project=None):
        self.path = path
        self.project = project or path.parent
        self.evidence = []
        self.changed = None
        self.marker_ns = None
        self.result = dict(state='unknown', manager=None, marker=None,
                           reason='缺少可验证的安装记录（Bun 等未支持的格式保留）')
        try:
            manifest, stamp = regular_text(self.project / 'package.json')
            if not isinstance(json.loads(manifest), dict):
                raise ValueError('package.json 格式无效')
            self.evidence.append((self.project / 'package.json', stamp))
            found = []
            for manager, lock, record in self.PAIRS:
                marker = path / record
                lock_path = self.project / lock
                if not marker.exists() or not lock_path.exists():
                    continue
                text, ms = regular_text(marker)
                _, ls = regular_text(lock_path)
                if record == '.package-lock.json':
                    value = json.loads(text)
                    valid = (isinstance(value, dict) and type(value.get('lockfileVersion')) is int
                             and value['lockfileVersion'] in (2, 3) and isinstance(value.get('packages'), dict))
                elif record == '.modules.yaml':
                    valid = bool(re.search(r'^layoutVersion:\s*[1-9][0-9]*\s*$', text, re.M))
                elif record == '.yarn-integrity':
                    value = json.loads(text)
                    valid = isinstance(value, dict) and isinstance(value.get('topLevelPatterns'), list)
                else:
                    valid = bool(re.search(r'^__metadata:\s*$', text, re.M)
                                 and re.search(r'^\s+version:\s*[0-9]+\s*$', text, re.M))
                if not valid:
                    raise ValueError('安装记录格式不受支持')
                found.append((manager, marker, ms, lock_path, ls))
            if not found:
                return
            if len({m for m, *_ in found}) != 1:
                raise ValueError('存在多种包管理器安装记录，无法确定本次安装来源')
            # The oldest record is conservative when two supported records coexist.
            manager, marker, ms, _, _ = min(found, key=lambda x: x[2][3])
            self.marker_ns = ms[3]
            for _, p, ps, lock, ls in found:
                self.evidence.extend(((p, ps), (lock, ls)))
            self.result.update(state='verified', manager=manager, marker=str(marker),
                               reason='安装记录之后未发现更新的文件；未校验包内容哈希')
            for p, s in self.evidence:
                if p.parent == self.project and s[3] > self.marker_ns:
                    self.changed = str(p)
                    break
        except (OSError, ValueError, UnicodeError, RuntimeError):
            self.result.update(state='unknown', reason='安装记录、锁文件或项目清单无法完整验证')

    def observe(self, relative, info):
        if self.marker_ns is None or self.changed or self.result['state'] != 'verified':
            return
        relative = Path(relative)
        if relative.parts and relative.parts[0] == '.cache':
            return
        if not stat.S_ISDIR(info.st_mode) and info.st_mtime_ns > self.marker_ns:
            self.changed = str(self.path / relative)

    def finish(self):
        if self.result['state'] == 'verified':
            try:
                if any(fingerprint(p.lstat()) != stamp or p.is_symlink() for p, stamp in self.evidence):
                    raise ValueError('metadata changed')
            except (OSError, ValueError):
                self.result.update(state='unknown', reason='盘点期间安装记录或项目清单发生变化')
            else:
                if self.changed:
                    self.result.update(state='modified', reason='安装后有修改：' + self.changed)
        return dict(self.result)


class ProcessSnapshot:
    """Bounded current-user lsof + ps observation; incomplete evidence stays unknown.

    Command lines are kept only in memory and never included in reports.
    """
    def __init__(self):
        self.files, self.commands = [], []
        self.error = None
        program = shutil.which('lsof')
        if not program:
            self.error = '未找到 lsof，无法确认运行状态'
            return
        try:
            opened = subprocess.run([program, '-nP', '-a', '-u', str(os.getuid()), '-F0pfn'],
                                    capture_output=True, timeout=12, stdin=subprocess.DEVNULL)
            if opened.returncode or opened.stderr or not opened.stdout or len(opened.stdout) > 64 * 1024**2:
                raise ValueError('打开文件检查不完整')
            pid, field = None, None
            for raw in opened.stdout.split(b'\0'):
                raw = raw.lstrip(b'\n')
                if not raw:
                    continue
                tag, value = raw[:1], os.fsdecode(raw[1:])
                if tag == b'p' and value.isdecimal():
                    pid, field = int(value), None
                elif tag == b'f':
                    field = value
                elif tag == b'n' and pid is not None and field is not None:
                    if value.startswith('/') and pid != os.getpid():
                        # lsof escapes non-ASCII bytes and literal backslashes.
                        decoded = re.sub(rb'\\x([0-9a-fA-F]{2})|\\\\',
                            lambda m: bytes([int(m[1],16)]) if m[1] else b'\\', raw[1:])
                        self.files.append((pid, field, os.fsdecode(decoded)))
                else:
                    raise ValueError('打开文件结果格式不完整')
            commands = subprocess.run(['ps', '-ww', '-axo', 'pid=,command='], capture_output=True,
                                      timeout=5, stdin=subprocess.DEVNULL)
            if commands.returncode or commands.stderr or not commands.stdout:
                raise ValueError('进程列表检查不完整')
            for line in os.fsdecode(commands.stdout).splitlines():
                pair = line.strip().split(None, 1)
                if len(pair) != 2 or not pair[0].isdigit():
                    raise ValueError('进程列表格式不完整')
                if int(pair[0]) != os.getpid():
                    self.commands.append((int(pair[0]), pair[1]))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.error = '运行状态检查失败或超时，无法确认未被使用'

    def check(self, tree, *, project=None):
        normalize = lambda p: unicodedata.normalize('NFC', str(p)).casefold()
        trees = [normalize(tree), normalize(Path(tree).resolve())]
        if project is not None:
            project = normalize(Path(project).resolve())
        if not hasattr(self, '_normalized_files'):
            self._normalized_files = [(pid, field, normalize(Path(name).resolve())) for pid, field, name in self.files]
        for pid, field, name in self._normalized_files:
            for target in trees + ([project] if field == 'cwd' and project else []):
                if name == target or name.startswith(target + '/'):
                    return dict(state='busy', reason=f'仍被进程 {pid} 使用（工作目录或打开文件）', pid=pid)
        for pid, command in self.commands:
            target = project or normalize(tree)
            if re.search(r'(?<![\w/.-])' + re.escape(target) + r'(?=/|\s|[,:;\x22\x27]|$)', normalize(command)):
                return dict(state='busy', reason=f'进程 {pid} 的启动参数引用此路径', pid=pid)
        if self.error:
            return dict(state='unknown', reason=self.error)
        return dict(state='idle', reason='本次检查未发现进程引用（非原子快照）')


def require_idle(path, *, project=None):
    result = ProcessSnapshot().check(path, project=project)
    if result['state'] != 'idle':
        raise ValueError(result['reason'])


def require_installation(path, *, project=None):
    check = Installation(path, project=project)
    def fail(exc):
        raise exc
    device = path.lstat().st_dev
    for base, dirs, files, fd in os.fwalk(path, follow_symlinks=False, onerror=fail):
        if '.git' in dirs or '.git' in files:
            raise ValueError('包含嵌套 Git 仓库，保留')
        for name in dirs + files:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_dev != device:
                raise ValueError('包含其他挂载卷，保留')
            check.observe((Path(base) / name).relative_to(path), info)
    result = check.finish()
    if result['state'] != 'verified':
        raise ValueError(result['reason'])
