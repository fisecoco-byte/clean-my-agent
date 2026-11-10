"""Quarantine on the same filesystem with a write-ahead manifest and no-clobber restore."""
import contextlib
from datetime import datetime
try:
    import fcntl
except ImportError:
    fcntl = None
import json
import os
import re
import stat
import uuid
from pathlib import Path

from . import deps, policy as policies, project_types, agent_resources
from .policy import CLEANABLE_FILES, RETIRED
from .scan import classify, disk_bytes, fingerprint
from .dependency_safety import require_installation

D_FLAGS = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
BATCH = re.compile(r'[0-9a-f]{16}\Z')


def tree_item(item):
    return item['category'] == 'dependencies' or (item['category'] == 'agent-resources' and item.get('kind') == 'directory')


@contextlib.contextmanager
def directory(path, create=False):
    """Pin each directory by fd. Never traverse a symlink on a modifying operation."""
    if os.name == 'nt':
        from .portable import READ_ONLY
        raise ValueError(READ_ONLY)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('路径无效')
    fd = os.open('/', D_FLAGS)
    try:
        for p in path.parts[1:]:
            if create:
                try:
                    os.mkdir(p, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(p, D_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def check_private(fd):
    s = os.fstat(fd)
    if s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise ValueError('暂存目录必须归当前用户所有，且仅当前用户可访问 (0700)')


@contextlib.contextmanager
def batch_lock(state, batch):
    if not BATCH.fullmatch(batch):
        raise ValueError('无效的批次编号，请用 trash 查看')
    with directory(state) as vfd:
        check_private(vfd)
        fd = os.open(batch, D_FLAGS, dir_fd=vfd)
        try:
            check_private(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield fd
        finally:
            os.close(fd)


def unchanged(fd, name, item, moving=True):
    s = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if tree_item(item):
        if not stat.S_ISDIR(s.st_mode) or (s.st_dev, s.st_ino) != tuple(item['stamp'][:2]):
            raise ValueError('依赖目录已被替换')
        return item['size']
    same_file = (s.st_dev, s.st_ino) == tuple(item['stamp'][:2])
    if (not stat.S_ISREG(s.st_mode) or not same_file
            or (moving and (tuple(item['stamp']) != fingerprint(s) or s.st_nlink != 1))):
        raise ValueError('文件已变化或含其他硬链接')
    return disk_bytes(s)


def origin(item, roots, *, restoring=False):
    if item['category'] == 'agent-resources':
        return agent_resources.resolve(item, roots)[1]
    root = roots.get(item['source'])
    rel = Path(item['path'])
    if root is None or str(root) != item['root']:
        raise ValueError('数据根目录与预览时不一致')
    if not rel.parts or rel.is_absolute() or '..' in rel.parts:
        raise ValueError('原路径无效')
    cat = item['category']
    if cat == 'dependencies' and item['source'] in ('codex', 'claude', 't3') and deps.valid_location(
            root, root / rel, cache_tag=item.get('cache_tag') == project_types.CACHE_TAG):
        return root / rel
    if cat != classify(item['source'], rel):
        raise ValueError('原路径与清单类别不匹配')
    if cat not in CLEANABLE_FILES and not (restoring and cat in RETIRED):
        raise ValueError('此项只可盘点或还原，不属于清理范围')
    return root / rel


def save(items, roots, state, policy=None):
    policy = policy or policies.load()
    if not items:
        return None, 0, 0, []
    policy.require_current()
    prepared, errors = [], []
    for original_item in items:
        item = dict(original_item)
        try:
            path = origin(item, roots)
            if tree_item(item):
                item['tree_digest'] = project_types.tree_digest(path)
                if item['category'] == 'dependencies' and project_types.tagged(path):
                    item['cache_tag'] = project_types.CACHE_TAG
            prepared.append(item)
        except (OSError, ValueError) as exc:
            errors.append(f"{item['path']}: {exc}")
    items = prepared
    if not items:
        return None, 0, 0, errors
    key = uuid.uuid4().hex[:16]
    with directory(state, create=True) as fd:
        check_private(fd)
        os.mkdir(key, 0o700, dir_fd=fd)
    count = size = 0
    with batch_lock(state, key) as fd:
        with os.fdopen(os.open('manifest.tmp', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=fd), 'w') as stream:
            json.dump({'version': 1, 'created': datetime.now().astimezone().isoformat(timespec='seconds'), 'policy': policy.describe(), 'items': items}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename('manifest.tmp', 'manifest.json', src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
        for index, item in enumerate(items):
            try:
                path = origin(item, roots)
                guard = agent_resources.guarded(item, roots, policy) if item['category'] == 'agent-resources' else contextlib.nullcontext()
                with guard, directory(path.parent) as src:
                    if item['category'] == 'dependencies':
                        deps.eligible(roots[item['source']], path, policy, item['source'])
                        policy.require_current()
                    elif policy.protected(item['source'], Path(item['path'])):
                        raise ValueError('命中保护路径')
                    unchanged(src, path.name, item)
                    if tree_item(item) and project_types.tree_digest(path) != item['tree_digest']:
                        raise ValueError('核查期间目录内容发生变化；未暂存')
                    os.rename(path.name, str(index), src_dir_fd=src, dst_dir_fd=fd)
                count += 1
                size += item['size']
            except (OSError, ValueError) as exc:
                errors.append(f"{item['path']}: {exc}")
        discard_empty(state, key, fd)
    return (key if count else None), count, size, errors


def manifest(fd):
    with os.fdopen(os.open('manifest.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)) as stream:
        data = json.load(stream)
    if data.get('version') != 1 or not isinstance(data.get('items'), list):
        raise ValueError('批次清单格式不兼容')
    return data


def discard_empty(state, key, fd):
    # Caller holds the batch lock. Keep any unexpected file intact.
    if os.listdir(fd) == ['manifest.json']:
        os.unlink('manifest.json', dir_fd=fd)
        with directory(state) as parent:
            os.rmdir(key, dir_fd=parent)


def process(state, key, roots, action, apply, detail=None, policy=None, indices=None):
    if action == 'purge':
        policy = policy or policies.load()
    errors = []
    count = size = 0
    with batch_lock(state, key) as fd:
        data = manifest(fd)
        items = data['items']
        if detail is not None:
            detail.update(created=data.get('created', ''), policy=data.get('policy'), files=[], entries=[])
        names = os.listdir(fd)
        if indices is not None:
            if (not indices or any(type(i) is not int or not 0 <= i < len(items) for i in indices)
                    or len(set(indices)) != len(indices)):
                raise ValueError('请选择有效且不重复的暂存条目')
            if any(str(i) not in names for i in indices):
                raise ValueError('所选暂存条目已变化，请刷新')
            if any(name != 'manifest.json' and (not name.isascii() or not name.isdecimal()
                   or str(int(name)) != name or int(name) >= len(items)) for name in names):
                raise ValueError('暂存区含未知文件，未执行')
        for name in sorted(names):
            if name == 'manifest.json':
                continue
            if not name.isascii() or not name.isdecimal() or str(int(name)) != name or int(name) >= len(items):
                errors.append(f'暂存区中有未识别的文件：{name}')
                continue
            if indices is not None and int(name) not in indices:
                continue
            item = items[int(name)]
            try:
                # After an interrupted restore the same file may exist at both names.
                if action == 'purge':
                    original = origin(item, roots)  # Also check v1 batches and dry runs.
                    if item['category'] == 'dependencies':
                        refs = deps.References(roots[item['source']])
                        refs.guard(original)  # A new dangling link can still need this directory restored.
                        refs.guard(state / key / name)
                        deps.check_mounts(state / key / name)
                        _, project = deps.project_context(original)
                        project_types.require_evidence(state / key / name, original, project)
                        deps.require_idle(state / key / name, project=project)
                    if item['category'] == 'agent-resources':
                        with agent_resources.guarded(item, roots, policy, state / key / name):
                            pass
                    if tree_item(item) and item.get('tree_digest') and project_types.tree_digest(state / key / name) != item['tree_digest']:
                        raise ValueError('暂存后内容有改动；保留，可恢复')
                    if policy.protected(item['source'], item['path'], tree=tree_item(item)):
                        raise ValueError('原路径已受策略保护；保留在暂存区，可还原')
                amount = unchanged(fd, name, item, moving=(action == 'purge'))
                if detail is not None:
                    detail['files'].append((str(Path(item['root']) / item['path']), amount))
                    detail['entries'].append(dict(index=int(name), path=str(Path(item['root']) / item['path']), bytes=amount))
                if apply and action == 'restore':
                    path = origin(item, roots, restoring=True)
                    tree = tree_item(item)
                    with directory(path.parent, create=not tree) as dst:
                        if tree:
                            deps.rename_exclusive(fd, name, dst, path.name)
                            count += 1
                            size += item['size']
                            continue
                        try:
                            os.link(name, path.name, src_dir_fd=fd, dst_dir_fd=dst, follow_symlinks=False)
                        except FileExistsError:
                            orig = os.stat(path.name, dir_fd=dst, follow_symlinks=False)
                            if not os.path.samestat(orig, os.stat(name, dir_fd=fd, follow_symlinks=False)):
                                raise ValueError('原路径已有新文件，未覆盖')
                        os.unlink(name, dir_fd=fd)
                elif apply and action == 'purge':
                    guard = (agent_resources.guarded(item, roots, policy, state / key / name)
                             if item['category'] == 'agent-resources' else contextlib.nullcontext())
                    with guard:
                        if tree_item(item):
                            deps.check_mounts(state / key / name)
                            deps.empty_tree(fd, name, item['stamp'][0])
                        else:
                            os.unlink(name, dir_fd=fd)
                count += 1
                size += amount
            except (OSError, ValueError) as exc:
                errors.append(f"{item['path']}: {exc}")
        if apply:
            discard_empty(state, key, fd)
    return count, size, errors


def batches(state):
    if not state.exists():
        return []
    try:
        with directory(state) as fd:
            check_private(fd)
            return sorted((key for key in os.listdir(fd) if BATCH.fullmatch(key)),
                          key=lambda k: os.stat(k, dir_fd=fd, follow_symlinks=False).st_mtime)
    except FileNotFoundError:
        return []
