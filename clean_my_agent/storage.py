"""Allocated blocks, shared blocks, and the data freed by unlinking one entire tree.

Directory totals use du's inode accounting. Never follow a symlink. Private size
is a filesystem measurement, not st_size, nor a guess based on a pnpm lockfile.
"""
import ctypes
import errno
import os
import stat
import struct
import sys


class DirectoryUsage:
    """Allocated blocks reusable within one scan, deduplicating hard links.

    Keep identities only for multiply linked non-directories, not every file.
    These summaries are never a cleanup authorization or a persistent cache.
    """
    def __init__(self):
        self.bytes = 0
        self.links = {}

    def observe(self, info):
        amount = info.st_blocks * 512
        if info.st_nlink > 1 and not stat.S_ISDIR(info.st_mode):
            self.links[(info.st_dev, info.st_ino)] = amount
        else:
            self.bytes += amount

    def merge(self, other):
        self.bytes += other.bytes
        self.links.update(other.links)

    def total(self):
        # Match du -k: round once for the entire tree, not per artifact.
        amount = self.bytes + sum(self.links.values())
        return ((amount + 1023) // 1024) * 1024


def workspace_usage(path, measured, *, progress=None):
    """Count a workspace, pruning artifacts measured earlier in this report."""
    from .scan import fingerprint
    total = DirectoryUsage()
    root = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    def fail(exc):
        raise exc
    try:
        base = os.fstat(root)
        total.observe(base)
        visited = 0
        for relative, dirs, files, fd in os.fwalk('.', dir_fd=root, follow_symlinks=False, onerror=fail):
            visited += len(dirs) + len(files)
            if progress and visited >= 2048:
                progress('统计工作树完整占用', detail=path.name)
                visited = 0
            for name in dirs + files:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if info.st_dev != base.st_dev:
                    if name in dirs:
                        dirs.remove(name)
                    continue
                child = path / relative / name
                cached = measured.get(child)
                if cached and name in dirs and fingerprint(info) == cached[0] and stat.S_ISDIR(info.st_mode):
                    total.merge(cached[1])
                    dirs.remove(name)
                else:
                    total.observe(info)
        if not os.path.samestat(base, path.lstat()):
            raise ValueError('统计期间工作目录已替换')
    finally:
        os.close(root)
    return total.total()


class DarwinAttributes:
    # See getattrlist(2), sys/attr.h. The returned bitmap, not field contents,
    # says which attributes the volume actually supplies. Fixed offsets require
    # FSOPT_PACK_INVAL_ATTRS. Never dereference the final path component.
    ext = 0x8 | 0x100 | 0x200 | 0x1000
    attrs = struct.pack('=HH5I', 5, 0, 0x82000000, 0, 0, 0, ext)
    data = struct.Struct('=6IQqQQI')

    def __init__(self):
        self.get = getattr(ctypes.CDLL(None, use_errno=True), 'getattrlistat', None)
        self.buf = ctypes.create_string_buffer(128)

    def __call__(self, fd, name, s):
        if self.get is None:
            return None
        if self.get(fd, os.fsencode(name), self.attrs, self.buf, len(self.buf), 0x29):
            err = ctypes.get_errno()
            if err in (errno.ENOTSUP, errno.ENOSYS, errno.EINVAL):
                return None
            raise OSError(err, os.strerror(err), name)
        length, common, _, _, _, returned, inode, private, cid, flags, refs = self.data.unpack_from(self.buf)
        if length < self.data.size or not common & 0x02000000 or not returned & 8:
            return None
        if inode != s.st_ino:
            raise ValueError('测量期间文件已替换')
        if not 0 <= private <= s.st_blocks * 512:
            return None
        full_clone = (cid, refs) if returned & self.ext == self.ext and flags & 0x40 and cid and refs > 1 else None
        return private, full_clone


class Accounting:
    def __init__(self):
        self.allocated = self.shared = self.private = self.unknown = self.uncertain_release = 0
        self.clones = {}  # Only fully shared inodes, grouped within this tree.
        self.links = {}   # Only inodes with multiple hard links need a seen set.

    def add_inode(self, size, info, external):
        self.allocated += size
        if external:
            self.shared += size  # Another hard link survives the whole tree.
        elif info is None:
            self.unknown += size
        else:
            private, clone = info
            self.shared += size - private
            self.private += private
            if clone is None:
                # Non-private bytes may be held by snapshots, partial clones or
                # other in-tree copies. No extent-level reference count is known.
                self.uncertain_release += size - private
        if info is not None and info[1] is not None:
            cid, refs = info[1]
            group = self.clones.setdefault(cid, [0, refs, 0, False])
            if group[1] != refs:
                raise ValueError('测量期间克隆引用数变化，请重新盘点')
            group[0] += 1
            group[2] = max(group[2], size - info[0])
            group[3] |= external

    def observe(self, fd, name, s, attributes):
        size = s.st_blocks * 512
        if not size:
            return
        hard = s.st_nlink > 1 and not stat.S_ISDIR(s.st_mode)
        if hard and s.st_ino in self.links:
            rec = self.links[s.st_ino]
            if (s.st_nlink, size) != (rec[1], rec[2]):
                raise ValueError('测量期间硬链接变化，请重新盘点')
            rec[0] += 1
            return
        info = attributes(fd, name, s) if attributes else None
        if hard:
            self.links[s.st_ino] = [1, s.st_nlink, size, info]
        else:
            self.add_inode(size, info, False)

    def result(self):
        for count, nlink, size, info in self.links.values():
            if count > nlink:
                raise ValueError('测量期间硬链接变化，请重新盘点')
            self.add_inode(size, info, count < nlink)
        upper = self.private + self.unknown + self.uncertain_release
        for count, refs, size, external in self.clones.values():
            if count > refs:
                raise ValueError('测量期间克隆引用数变化，请重新盘点')
            if count == refs and not external:
                # Last full clones are inside. At most one copy, potentially
                # still held by a snapshot or a partial clone outside this tree.
                upper += size
        return dict(directory_bytes=self.allocated, known_shared_bytes=self.shared,
                    shared_bytes=None if self.unknown else self.shared, unknown_bytes=self.unknown,
                    reclaimable_min_bytes=self.private, reclaimable_max_bytes=upper,
                    reclaimable_bytes=self.private if self.private == upper else None)


def measure(path, *, on_entries=None, on_entry=None):
    if os.name == 'nt':
        from .portable import measure as windows_measure
        return windows_measure(path, on_entries=on_entries, on_entry=on_entry)
    totals = Accounting()
    attrs = DarwinAttributes() if sys.platform == 'darwin' else None
    root = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    def fail(exc):
        raise exc
    try:
        base = os.fstat(root)
        newest = base.st_mtime_ns
        nested_git = False
        visited = reported = 0
        totals.observe(root, '.', base, attrs)
        for relative, dirs, files, fd in os.fwalk('.', dir_fd=root, follow_symlinks=False, onerror=fail):
            nested_git |= '.git' in dirs or '.git' in files
            for name in dirs + files:
                s = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if s.st_dev != base.st_dev:
                    raise ValueError('包含其他挂载卷，未测量')
                newest = max(newest, s.st_mtime_ns)
                if on_entry:
                    on_entry(os.path.join(relative, name), s)
                totals.observe(fd, name, s, attrs)
                if on_entries:
                    visited += 1
                    if visited - reported >= 2048:
                        on_entries(visited)
                        reported = visited
        if not os.path.samestat(base, os.lstat(path)):
            raise ValueError('测量期间依赖目录已替换')
    finally:
        os.close(root)
    return totals.result(), newest, nested_git
