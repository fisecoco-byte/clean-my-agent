"""Report completed items without a second walk to estimate the total."""
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from queue import Empty, SimpleQueue
from threading import Event
import os


def counted(items, progress, label, describe=str):
    if not progress:
        yield from items
        return
    total = len(items)
    for done, item in enumerate(items):
        progress(label, done, total, describe(item))
        yield item
    if total:
        progress(label, total, total)


def parallel(items, work, progress, label, *, workers=None):
    """Bounded read-only work; all UI callbacks run on the calling thread.

    work(item, report_count) returns a result. Only the active window is queued,
    and cancellation stops scheduling work and is checked by entry callbacks.
    """
    items = list(items)
    total = len(items)
    if workers is None:
        try:
            workers = int(os.environ.get('CLEAN_MY_AGENT_SCAN_WORKERS', '1'))
        except ValueError as exc:
            raise ValueError('CLEAN_MY_AGENT_SCAN_WORKERS 需要是 1–4 的整数') from exc
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('扫描并发数需要是 1–4 的整数')
    if not total:
        return
    if workers == 1:
        for done, item in enumerate(items):
            if progress:
                progress(label, done, total)
            def count(n):
                if progress:
                    progress(label, done, total, f'{n:,} 项 · {item.parent.name}/{item.name}')
            yield item, work(item, count)
        if progress:
            progress(label, total, total)
        return
    updates, stopped = SimpleQueue(), Event()
    def run(item):
        def count(n):
            if stopped.is_set():
                raise CancelledError()
            updates.put((item, n))
        if stopped.is_set():
            raise CancelledError()
        return work(item, count)
    pool = ThreadPoolExecutor(max_workers=min(workers, total), thread_name_prefix='cma-scan')
    pending, remaining = {}, iter(items)
    done = 0
    try:
        if progress:
            progress(label, 0, total)
        for item in items[:workers]:
            next(remaining)
            pending[pool.submit(run, item)] = item
        while pending:
            ready, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
            detail = ''
            while True:
                try:
                    item, count = updates.get_nowait()
                    detail = f'{count:,} 项 · {item.parent.name}/{item.name}'
                except Empty:
                    break
            if progress:
                progress(label, done, total, detail)
            for future in ready:
                item = pending.pop(future)
                yield item, future.result()
                done += 1
                if progress:
                    progress(label, done, total)
                replacement = next(remaining, None)
                if replacement is not None:
                    pending[pool.submit(run, replacement)] = replacement
    finally:
        stopped.set()
        pool.shutdown(wait=True, cancel_futures=True)
