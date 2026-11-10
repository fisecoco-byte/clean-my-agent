import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from clean_my_agent.report_tui import Report
from clean_my_agent.inventory import plan

CLI = Path(__file__).resolve().parents[1] / 'clean-my-agent'


class ReporterTests(unittest.TestCase):
    def test_key_changes_match_report_plan_and_keep_agents_independent(self):
        doc = {'limit': 3, 'agents': {k: {'policy': {'archive_days': 10, 'delete_days': 20}}
                                      for k in ('codex', 'claude')}}
        rows = {k: [{'route': r, 'idle_days': age, 'bytes': 4096}
                    for r, age in zip(routes, (20, 12, 8, 5))]
                for k, routes in [('codex', ('archive', 'archive', 'delete', None)), ('claude', ('review',)*4)]}
        model = Report(doc, rows)
        for agent_key, agent in [('1', 'codex'), ('2', 'claude')]:
            model.input(agent_key, 9, 3)
            for _ in range(4):
                model.input('a', 9, 3)
                for _ in range(4):
                    model.input('d', 9, 3)
                    self.assertEqual(model.results, plan(rows[agent], **model.rules[agent], limit=3))
        model.input('D', 9, 3)
        self.assertEqual(model.rules['claude']['delete_days'], 15)
        model.input('TAB', 9, 3)
        self.assertEqual(model.agent, 'codex')
        self.assertEqual(model.rules['codex']['delete_days'], 20)
        self.assertEqual(doc['agents']['claude']['policy']['delete_days'], 20)

    def test_real_terminal_resize_navigation_and_restoration(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            env = dict(os.environ, HOME=str(home), TERM='xterm-256color', LC_ALL='en_US.UTF-8',
                       CODEX_HOME=str(home/'.codex'), CLAUDE_CONFIG_DIR=str(home/'.claude'),
                       XDG_CONFIG_HOME=str(home/'config'), XDG_CACHE_HOME=str(home/'cache'),
                       XDG_STATE_HOME=str(home/'state'))
            env.pop('LINES', None)
            env.pop('COLUMNS', None)
            f = home/'.claude/projects/demo/01234567-89ab-cdef-9012-000000000001.jsonl'
            f.parent.mkdir(parents=True)
            f.write_text('must stay intact')
            os.utime(f, (1, 1))
            master, slave = pty.openpty()
            self.addCleanup(os.close, master)
            self.addCleanup(os.close, slave)
            def resize(rows, cols):
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            resize(24, 80)
            saved = termios.tcgetattr(slave)
            p = subprocess.Popen([sys.executable, CLI, 'reporter'], stdin=slave, stdout=slave, stderr=slave, env=env)
            try:
                def until(marker):
                    buf = b''
                    deadline = time.monotonic() + 5
                    while marker.encode() not in buf and time.monotonic() < deadline:
                        if select.select([master], [], [], 0.1)[0]:
                            buf += os.read(master, 65536)
                        if p.poll() is not None:
                            break
                    self.assertIn(marker.encode(), buf, buf[-300:])
                    return buf
                modern = b'SESSIONS' in until('q 退出')
                os.write(master, b'2d')
                until('人工复核')
                resize(12, 28)
                os.kill(p.pid, signal.SIGWINCH)
                until('窗口太小')
                resize(26, 70)
                os.kill(p.pid, signal.SIGWINCH)
                until('q 退出')
                os.write(master, b'i')
                until('会话文件')
                if modern:
                    os.write(master, b'\x1b')
                    until('操作清单')
                os.write(master, b'?')
                until('点击天数按钮' if modern else '关闭帮助')
                os.write(master, b'qqq' if modern else b'q')
                until('\x1b[?1049l')  # Drain the PTY during teardown, as a terminal would.
                self.assertEqual(p.wait(timeout=5), 0)
                restored = termios.tcgetattr(slave)
                # On Darwin a transition back to canonical mode can set the kernel's
                # pending-input indicator even when every user setting was restored.
                for settings in (restored, saved):
                    settings[3] &= ~getattr(termios, 'PENDIN', 0)
                self.assertEqual(restored, saved)
                self.assertEqual(f.read_text(), 'must stay intact')
                self.assertFalse((home/'config').exists())
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait()
            # Pipelines always terminate and the machine format contains no curses output.
            for args in [('reporter','--plain'), ('report','--json'), ('inventory','--tui')]:
                out = subprocess.check_output([sys.executable, CLI, *args], stdin=subprocess.DEVNULL, env=env, timeout=5)
                self.assertNotIn(b'\x1b', out)
                if '--json' in args:
                    self.assertEqual(json.loads(out)['agents']['claude']['session_count'], 1)
