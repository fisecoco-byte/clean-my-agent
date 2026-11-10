"""Offline ascii.rest scan animation; progress is supplied only by the scanner."""
import time
from functools import lru_cache

from rich.text import Text
from textual import events
from textual.containers import Horizontal, Vertical
from textual.widgets import ProgressBar, Static

from ._loading_frames import CAT_FRAMES, CAT_HEIGHT, CAT_PALETTE, CAT_WIDTH, FPS, SCAN_FRAMES
from .terminal import safe


@lru_cache(maxsize=1)
def cat_frames():
    """Build the colored frames once; each widget receives its own Text copy."""
    result = []
    for artwork, colors in CAT_FRAMES:
        text = Text(artwork, justify='center', no_wrap=True)
        start = 0
        for end in range(1, len(colors) + 1):
            if end == len(colors) or colors[end] != colors[start]:
                if colors[start] != '\n':
                    text.stylize(CAT_PALETTE[int(colors[start])], start, end)
                start = end
        result.append(text)
    return tuple(result)


class ScanLoading(Vertical):
    DEFAULT_CSS = '''
        ScanLoading { height: 1fr; min-height: 6; align: center middle; }
        ScanLoading Static { width: 1fr; height: 1; content-align: center middle; }
        ScanLoading .scan-art { height: 15; color: #7de2bf; }
        ScanLoading .scan-count { color: #9ab4ac; margin-top: 1; }
        ScanLoading .scan-meter { height: 1; width: 1fr; align: center top; }
        ScanLoading ProgressBar { width: 36; max-width: 100%; height: 1; }
        ScanLoading.compact .scan-art { height: 1; }
        ScanLoading.compact .scan-count { margin-top: 0; }
    '''

    def __init__(self, title='正在扫描', **kwargs):
        super().__init__(**kwargs)
        self.heading = title
        self.started = None
        self.frame = 0
        self.done = self.total = None
        self.phase = '准备扫描'
        self.detail = ''

    def compose(self):
        yield Static('', classes='scan-art', markup=False)
        yield Static('', classes='scan-count', markup=False)
        with Horizontal(classes='scan-meter'):
            yield ProgressBar(show_eta=False, classes='scan-progress')

    def on_mount(self):
        self.timer = self.set_interval(1 / FPS, self.advance, pause=True)
        self.query_one(ProgressBar).display = False
        self.query_one('.scan-meter').display = False

    def start(self):
        self.started = time.monotonic()
        self.frame = 0
        self.display = True
        self.set_progress('准备扫描')
        self.timer.resume()
        self.advance()

    def stop(self):
        self.timer.pause()
        self.started = None
        self.display = False

    def on_unmount(self):
        self.timer.stop()

    def on_resize(self, event: events.Resize):
        self.set_class(event.size.height < CAT_HEIGHT + 3 or event.size.width < CAT_WIDTH, 'compact')
        if self.started is not None:
            self.draw()

    def set_progress(self, label, done=None, total=None, detail=''):
        self.phase, self.detail = safe(label), safe(detail)
        known = type(done) is int and type(total) is int and total > 0 and 0 <= done <= total
        self.done, self.total = (done, total) if known else (None, None)
        bar = self.query_one(ProgressBar)
        self.query_one('.scan-meter').display = known
        bar.display = known
        if known:
            bar.update(total=total, progress=done)
        self.draw()

    def advance(self):
        if self.started is not None:
            self.frame += 1
            self.draw()

    def draw(self):
        if self.has_class('compact'):
            text = Text(SCAN_FRAMES[self.frame % len(SCAN_FRAMES)], justify='center', no_wrap=True)
        else:
            frames = cat_frames()
            text = frames[self.frame % len(frames)].copy()
        self.query_one('.scan-art', Static).update(text)
        elapsed = int(time.monotonic() - self.started) if self.started is not None else 0
        count = f'{self.done}/{self.total} · ' if self.total is not None else ''
        self.query_one('.scan-count', Static).update(f'{count}{elapsed//60:02d}:{elapsed%60:02d}')
