"""Report completed items without a second walk to estimate the total."""


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
