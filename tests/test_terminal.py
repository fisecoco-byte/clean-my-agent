"""Run the actual CLI on a pseudo terminal: pipes alone cannot exercise display modes."""
import errno
import io
import json
import os
import pty
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from clean_my_agent.terminal import Terminal, cells, progress_line

CLI = Path(__file__).resolve().parents[1] / 'clean-my-agent'


class ScanProgress(unittest.TestCase):
    def test_width_and_phase_local_percentage(self):
        for width in (29, 37, 45, 79):
            line = progress_line(width, 83, 2, '测量依赖目录', 3, 12, '中文项目' * 30)
            self.assertLessEqual(cells(line), width)
            self.assertIn('3/12', line)
            self.assertIn('01:23', line)
            if width >= 45:
                self.assertIn('25%', line)
                self.assertIn('█', line)
            line = progress_line(width, 83, 2, '查找工作树', None, None, '')
            self.assertNotIn('%', line)

    def test_live_stderr_and_interrupt_cleanup_in_monochrome(self):
        drawn = threading.Event()
        class Screen(io.StringIO):
            def isatty(self): return True
            def write(self, data):
                if '1/3' in data:
                    drawn.set()
                return super().write(data)
        stdout, stderr = Screen(), Screen()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr), patch.dict(os.environ, {
                'TERM':'xterm', 'COLUMNS':'80', 'NO_COLOR':'1'}):
            with self.assertRaises(KeyboardInterrupt):
                with Terminal().working('查找目录') as update:
                    update('测量依赖目录', 1, 3, 'project\x1b[2J')
                    self.assertTrue(drawn.wait(3), 'progress must be visible before the scan returns')
                    raise KeyboardInterrupt()
            with Terminal(plain=True).working('无进度') as update:
                self.assertIsNone(update)
        self.assertEqual(stdout.getvalue(), '')
        self.assertNotIn('\x1b', stderr.getvalue())
        self.assertRegex(stderr.getvalue(), r'\r +\r$')


class TerminalModes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name).resolve()
        self.env = dict(os.environ, HOME=str(home), TERM='xterm-256color', COLUMNS='80',
                        CODEX_HOME=str(home / '.codex'), CLAUDE_CONFIG_DIR=str(home / '.claude'),
                        XDG_CACHE_HOME=str(home / 'cache'), XDG_CONFIG_HOME=str(home / 'config'),
                        XDG_STATE_HOME=str(home / 'state'))
        self.env.pop('NO_COLOR', None)
        path = home / '.claude/debug/old.log'
        path.parent.mkdir(parents=True)
        path.write_text('debug')
        os.utime(path, (1, 1))

    def capture(self, *args, **env):
        read, write = pty.openpty()
        with os.fdopen(read, 'rb', buffering=0) as reader:
            with os.fdopen(write, 'wb', buffering=0) as writer:
                proc = subprocess.Popen([sys.executable, CLI, *args], env={**self.env, **env},
                                        stdout=writer, stderr=subprocess.PIPE)
            data = bytearray()
            while True:
                try:
                    block = reader.read(8192)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not block:
                    break
                data.extend(block)
            _, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err)
        return data.decode().replace('\r\n', '\n')

    def test_preview_uses_real_counts_and_preserves_filters(self):
        out = self.capture('clean', '--agent', 'claude', '--profile', 'deep', '--days', '40', '--details')
        self.assertIn('CLEAN MY AGENT', out)
        self.assertIn('1 个', out)
        self.assertIn('/debug/old.log', out)
        self.assertIn('clean --agent claude --profile deep --days 40 --apply', out)
        self.assertIn('仅预览，未移动文件', out)

    def test_narrow_monochrome_summary_fits_and_retains_values(self):
        out = self.capture('scan', COLUMNS='38', NO_COLOR='1')
        self.assertNotIn('\x1b', out)
        self.assertIn('可清理', out)
        self.assertIn('CLEAN MY AGENT', out)
        for line in out.splitlines():
            if re.match(r'^  [│┌└]', line):
                self.assertEqual(cells(line), 38)

    def test_machine_output_and_plain_modes_stay_undecorated(self):
        doc = json.loads(self.capture('scan', '--json'))
        self.assertEqual(doc['eligible_files'], 1)
        for out in (self.capture('--plain'), self.capture('scan', '--plain'), self.capture('scan', TERM='dumb')):
            self.assertNotIn('CLEAN MY AGENT', out)
            self.assertNotIn('\x1b', out)
            self.assertIn('1 个文件', out)
