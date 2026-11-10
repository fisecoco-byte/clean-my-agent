"""Terminal pie charts: apply live counts to ascii.rest's rasterized sectors."""
from bisect import bisect_right
from itertools import accumulate, groupby

from ._pie_grid import DEGREES
from .terminal import cells, pad

MARKS = ':+*#@'
STYLES = tuple(f'slice{i}' for i in range(5))


def share(n, total):
    if not n or not total:
        return '0%'
    if n == total:
        return '100%'
    pct = n / total * 100
    if pct < 0.1:
        return '<0.1%'
    if pct > 99.9:
        return '>99.9%'
    return f'{pct:.1f}%'


def pie_lines(histogram, width):
    """Keep exact counts in the legend even if a tiny sector occupies no cell."""
    total = sum(histogram.values())
    if not total:
        return [[('暂无会话', 'dim')]]
    boundaries = [360 * n for n in accumulate(histogram.values())]
    chart = []
    for row in DEGREES:
        buckets = (bisect_right(boundaries, deg * total) if deg >= 0 else -1 for deg in row)
        chart.append([((' ' if i == -1 else MARKS[i]) * sum(1 for _ in run),
                       'normal' if i == -1 else STYLES[i]) for i, run in groupby(buckets)])
    legend = []
    count_width = max(len(f'{n:,}') for n in histogram.values())
    for i, (label, n) in enumerate(histogram.items()):
        pct = share(n, total)
        row = [(MARKS[i] + '  ', STYLES[i]),
               (pad(label, 10) + ' ' + pad(f'{n:,}', count_width, True) + ' 个  ', 'normal'),
               (pad(pct, 6, True), 'bold')]
        if sum(cells(s) for s, _ in row) > width:
            row = [(MARKS[i] + ' ', STYLES[i]), (f'{label} {n:,} ', 'normal'), (pct, 'bold')]
        legend.append(row)
    if width < len(DEGREES[0]) + 3 + max(sum(cells(s) for s, _ in row) for row in legend):
        return chart + legend
    for y, entry in enumerate(legend, (len(chart) - len(legend)) // 2):
        chart[y].extend([('   ', 'normal'), *entry])
    return chart
