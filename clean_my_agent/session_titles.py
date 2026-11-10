"""Optional display labels. Missing, stale or unreadable labels never affect eligibility."""
import json
import os
import re
import stat
from contextlib import contextmanager
from itertools import chain
from .portable import open_read

WINDOW = 32 * 1024
INDEX_LIMIT = 16 * 1024 * 1024
UNTITLED = '（无标题或摘要）'
# Claude appends these top-level metadata records alongside (sometimes very large) messages.
CLAUDE_FIELDS = {'custom-title': ('customTitle', 3), 'ai-title': ('aiTitle', 2), 'summary': ('summary', 1)}
MARKER = re.compile(rb'"type"\s*:\s*"(?:custom-title|ai-title|summary)"')
ESCAPES = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))')


def label(value):
    if not isinstance(value, str):
        return ''
    value = ESCAPES.sub('', value[:4096])
    value = ' '.join(''.join(c if c.isprintable() else ' ' for c in value).split())
    return value if len(value) <= 160 else value[:159] + '…'


@contextmanager
def regular_file(name, dir_fd=None):
    fd = open_read(name, dir_fd)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('not a regular file')
        yield stream


def codex_titles(root):
    result = {}
    try:
        with regular_file(root / 'session_index.jsonl') as f:
            size = os.fstat(f.fileno()).st_size
            if size > INDEX_LIMIT:
                f.seek(size - INDEX_LIMIT - 1)
                f.readline(INDEX_LIMIT + 1)
            for raw in f.read(INDEX_LIMIT).splitlines():
                if len(raw) > WINDOW:
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if isinstance(row, dict) and isinstance(row.get('id'), str):
                    title = label(row.get('thread_name'))
                    if title:
                        result[row['id'].lower()] = title
    except (OSError, ValueError):
        pass
    return result


def claude_index(dir_fd):
    result = {}
    try:
        with regular_file('sessions-index.json', dir_fd) as f:
            if os.fstat(f.fileno()).st_size > INDEX_LIMIT:
                return result
            obj = json.loads(f.read(INDEX_LIMIT))
        entries = obj.get('entries') if isinstance(obj, dict) else None
        if isinstance(entries, list):
            for row in entries:
                if isinstance(row, dict) and isinstance(row.get('sessionId'), str):
                    result[row['sessionId'].lower()] = next((
                        (rank, name) for field, rank in CLAUDE_FIELDS.values()
                        if (name := label(row.get(field)))), (0, ''))
    except (OSError, ValueError, RecursionError):
        pass
    return result


def claude_title(filename, dir_fd, sid, indexed):
    best = indexed
    try:
        with regular_file(filename, dir_fd) as f:
            size = os.fstat(f.fileno()).st_size
            if size <= 2 * WINDOW:
                lines = f.read(2 * WINDOW).splitlines()
            else:
                first = f.read(WINDOW).split(b'\n')[:-1]
                f.seek(size - WINDOW - 1)
                last = f.read(WINDOW + 1).split(b'\n')[1:]
                lines = chain(first, last)
            for raw in lines:
                if not MARKER.search(raw):
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(row, dict) or row.get('sessionId', sid) != sid:
                    continue
                kind = row.get('type')
                if isinstance(kind, str) and kind in CLAUDE_FIELDS:
                    field, rank = CLAUDE_FIELDS[kind]
                    title = label(row.get(field))
                    if title and rank >= best[0]:
                        best = rank, title
    except (ValueError, OSError):
        pass
    return best[1]
