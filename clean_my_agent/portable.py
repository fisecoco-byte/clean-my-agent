"""Read-only Windows adapters. Unix mutation guarantees are never emulated."""
import ctypes
import os
from pathlib import Path
import stat
from functools import lru_cache

WINDOWS = os.name == 'nt'
READ_ONLY = 'Windows 原生版当前仅盘点；暂存、恢复和删除请使用 macOS / Linux 版'


def linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


class FileStandardInfo(ctypes.Structure):
    _fields_ = [('allocation',ctypes.c_int64),('end',ctypes.c_int64),('links',ctypes.c_uint32),
                ('pending',ctypes.c_ubyte),('directory',ctypes.c_ubyte)]


@lru_cache(maxsize=1)
def windows_api():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,
                               ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
    api.CreateFileW.restype = ctypes.c_void_p
    api.GetFileInformationByHandleEx.argtypes = [ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32]
    api.GetFileInformationByHandleEx.restype = ctypes.c_int
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    api.GetCompressedFileSizeW.argtypes = [ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_uint32)]
    api.GetCompressedFileSizeW.restype = ctypes.c_uint32
    return api


def allocated(path, info=None):
    info = info or Path(path).lstat()
    if hasattr(info, 'st_blocks'):
        return info.st_blocks * 512
    if linked(info) or not stat.S_ISREG(info.st_mode):
        return 0
    api = windows_api()
    name = str(Path(path).absolute())
    if not name.startswith('\\\\?\\'):
        name = '\\\\?\\UNC\\'+name[2:] if name.startswith('\\\\') else '\\\\?\\'+name
    # Metadata access, shared reads/writes/deletes, OPEN_EXISTING, no link following.
    handle = api.CreateFileW(name, 0x80, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        data = FileStandardInfo()
        if not api.GetFileInformationByHandleEx(handle, 1, ctypes.byref(data), ctypes.sizeof(data)):
            raise ctypes.WinError(ctypes.get_last_error())
        amount = data.allocation
        if getattr(info,'st_file_attributes',0) & (0x800 | 0x200):  # Compressed or sparse.
            high = ctypes.c_uint32()
            ctypes.set_last_error(0)
            low = api.GetCompressedFileSizeW(name, ctypes.byref(high))
            error = ctypes.get_last_error()
            if low == 0xffffffff and error:
                raise ctypes.WinError(error)
            amount = (high.value << 32) | low
    finally:
        api.CloseHandle(handle)
    after = Path(path).lstat()
    if (not os.path.samestat(info, after) or info.st_size != after.st_size
            or info.st_mtime_ns != after.st_mtime_ns or amount < 0):
        raise ValueError('测量期间文件已替换')
    return amount


def walk(root, errors):
    root = Path(root)
    try:
        info = root.lstat()
        if linked(info) or root.resolve() != root or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f'跳过链接或非目录：{root}')
        pending = [root]
        while pending:
            parent = pending.pop()
            if linked(parent.lstat()) or parent.resolve() != parent:
                raise ValueError(f'目录已变化：{parent}')
            dirs, files = [], []
            with os.scandir(parent) as entries:
                for entry in entries:
                    # DirEntry.stat on Windows may omit device / inode values.
                    # Compare identities from lstat on both sides of the check.
                    s = Path(entry.path).lstat()
                    if linked(s) or s.st_dev != info.st_dev:
                        continue
                    if stat.S_ISDIR(s.st_mode):
                        dirs.append(entry.name)
                    elif stat.S_ISREG(s.st_mode):
                        files.append(entry.name)
            yield parent, dirs, files, parent
            pending.extend(parent / name for name in reversed(dirs))
    except FileNotFoundError:
        if root.exists():
            errors.append(f'读取期间目录变化：{root}')
    except (OSError, ValueError) as exc:
        errors.append(str(exc))


def stat_at(name, parent):
    return (Path(parent) / name).lstat() if isinstance(parent, Path) else os.stat(name, dir_fd=parent, follow_symlinks=False)


def open_read(path, parent=None):
    if not WINDOWS:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    target = Path(parent) / path if parent is not None else Path(path)
    before = target.lstat()
    if linked(before) or target.resolve() != target or not stat.S_ISREG(before.st_mode):
        raise ValueError('跳过链接或非普通文件')
    fd = os.open(target, os.O_RDONLY | os.O_BINARY)
    if not os.path.samestat(before, os.fstat(fd)):
        os.close(fd)
        raise ValueError('读取期间文件已替换')
    return fd


def measure(path, *, on_entries=None, on_entry=None):
    errors, seen, total, newest, count = [], set(), 0, path.lstat().st_mtime_ns, 0
    nested = False
    for parent, dirs, files, _ in walk(path, errors):
        nested |= '.git' in dirs or '.git' in files
        for name in dirs + files:
            p = parent / name
            s = p.lstat()
            newest = max(newest, s.st_mtime_ns)
            if on_entry:
                on_entry(str(p.relative_to(path)), s)
            key = s.st_dev, s.st_ino
            if key not in seen:
                total += allocated(p, s)
                seen.add(key)
            count += 1
        if on_entries:
            on_entries(count)
    if errors:
        raise ValueError('; '.join(errors))
    return dict(directory_bytes=total, shared_bytes=None, known_shared_bytes=0,
                unknown_bytes=total, reclaimable_bytes=None,
                reclaimable_min_bytes=None, reclaimable_max_bytes=None), newest, nested


def scan(roots, selected, policy, *, full=False, progress=None):
    from .scan import Scan, QUICK, agent, classify
    out = Scan()
    for source, root in roots.items():
        if agent(source) not in selected:
            continue
        if not root.exists():
            out.missing.append(str(root))
            continue
        seen = set()
        for parent, dirs, files, _ in walk(root, out.errors):
            if not full and parent == root and source in QUICK:
                out.deferred.extend(str(parent/d) for d in dirs if d not in QUICK[source])
                dirs[:] = [d for d in dirs if d in QUICK[source]]
            if progress:
                progress('统计文件占用', detail=str(parent))
            for name in files:
                path = parent / name
                try:
                    s = path.lstat()
                    if linked(s) or (s.st_dev, s.st_ino) in seen:
                        continue
                    seen.add((s.st_dev, s.st_ino))
                    size = allocated(path, s)
                    rel = path.relative_to(root)
                    category = classify(source, rel)
                    row = out.rows[(agent(source), category)]
                    row[0] += 1
                    row[1] += size
                    group = str(root / rel.parts[0]) if len(rel.parts)>1 else str(root/'(根目录文件)')
                    out.folders[group] = out.folders.get(group, 0) + size
                except (OSError, ValueError) as exc:
                    out.errors.append(str(exc))
    return out


def dependencies(root, *, full=True, only=None, progress=None):
    from .project_types import INDICATORS, SWIFT_OUTPUTS, target
    errors, rows, projects = [], [], []
    repositories, manifests = set(), set()
    for area in ('worktrees', '.chatgpt-projects', 'projects'):
        for parent, dirs, names, _ in walk(root / area, errors):
            if '.git' in dirs or '.git' in names:
                repositories.add(parent)
            if any(name in INDICATORS or name.endswith(('.csproj', '.fsproj', '.vbproj')) for name in names):
                manifests.add(parent)
            found = [parent/name for name in dirs if target(parent/name)]
            dirs[:] = [d for d in dirs if d != '.git' and parent/d not in found]
            for build in list(found):
                if build.name == '.build':
                    found.extend(build/n for n in SWIFT_OUTPUTS if (build/n).is_dir() and not linked((build/n).lstat()))
            for path in found:
                if only is not None and path != only:
                    continue
                sizes = dict.fromkeys(('directory_bytes','shared_bytes','known_shared_bytes','unknown_bytes',
                                      'reclaimable_bytes','reclaimable_min_bytes','reclaimable_max_bytes'))
                if full:
                    try:
                        sizes, _, _ = measure(path, on_entries=(lambda n: progress('测量产物', detail=f'{n} 项')) if progress else None)
                    except (OSError, ValueError) as exc:
                        errors.append(str(exc))
                rows.append(dict(path=str(path), kind='directory', status='retained', reason=READ_ONLY,
                                 project=str(path.parent), artifact=path.name, referenced_by=[], target=None,
                                 installation=None, runtime=None, **sizes))
    if only is not None and not rows:
        raise ValueError('请选择盘点返回的项目产物绝对路径')
    workspaces = repositories | {p for p in manifests if not any(r == p or r in p.parents for r in repositories)}
    projects = [dict(path=str(p), directory_bytes=None, artifacts=0) for p in sorted(workspaces)
                if p in repositories or not any(r != p and r in p.parents for r in workspaces)]
    for project in projects:
        path = Path(project['path'])
        project['artifacts'] = sum(path in Path(row['path']).parents for row in rows)
        if full:
            try:
                project['directory_bytes'] = measure(path)[0]['directory_bytes']
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
    return dict(mode='measured' if full else 'references_only', entries=rows, errors=errors,
                projects=projects, external_targets={}, title='工作树 / 多语言产物',
                scope='Windows 文件分配量；不含文件系统元数据，共享、释放与引用核查未支持')
