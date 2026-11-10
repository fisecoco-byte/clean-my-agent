import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from clean_my_agent import dependency_safety as safety, storage


class InstallationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name).resolve()
        self.path = self.repo / 'node_modules'
        self.path.mkdir()
        self.write(self.repo / 'package.json', '{}', 5)
        self.write(self.repo / 'package-lock.json', '{}', 5)
        self.file = self.write(self.path / 'package.js', 'installed', 10)
        self.marker = self.write(self.path / '.package-lock.json',
                                 '{"lockfileVersion":3,"packages":{}}', 20)

    def write(self, path, text, when):
        path.write_text(text)
        os.utime(path, (when, when))
        return path

    def check(self):
        check = safety.Installation(self.path)
        storage.measure(self.path, on_entry=check.observe)
        return check.finish()

    def test_ancient_local_edit_is_retained_despite_old_retention_age(self):
        self.assertEqual(self.check()['state'], 'verified')
        os.utime(self.file, (30, 30))
        result = self.check()
        self.assertEqual(result['state'], 'modified')
        self.assertIn(str(self.file), result['reason'])

    def test_changed_manifest_missing_malformed_and_symlink_marker(self):
        os.utime(self.repo / 'package.json', (30, 30))
        self.assertEqual(self.check()['state'], 'modified')
        self.marker.unlink()
        self.assertEqual(self.check()['state'], 'unknown')
        self.marker.write_text('not json')
        self.assertEqual(self.check()['state'], 'unknown')
        self.marker.unlink()
        self.marker.symlink_to(self.repo / 'package.json')
        self.assertEqual(self.check()['state'], 'unknown')

    def test_metadata_changes_during_walk_are_unknown(self):
        check = safety.Installation(self.path)
        self.marker.write_text('{"lockfileVersion":3,"packages":{}}')
        self.assertEqual(check.finish()['state'], 'unknown')

    def test_pnpm_and_yarn_markers_and_mixed_install(self):
        self.write(self.repo / 'pnpm-lock.yaml', 'lockfileVersion: 9', 5)
        self.write(self.path / '.modules.yaml', 'layoutVersion: 5\n', 20)
        self.assertEqual(self.check()['state'], 'unknown')
        self.marker.unlink()
        self.assertEqual(self.check()['manager'], 'pnpm')
        (self.path / '.modules.yaml').unlink()
        self.write(self.repo / 'yarn.lock', '# yarn', 5)
        self.write(self.path / '.yarn-integrity', '{"topLevelPatterns":[]}', 20)
        self.assertEqual(self.check()['manager'], 'yarn')


class Processes(unittest.TestCase):
    def snapshot(self, opened, commands=b'88 node server.js\n', *, error=b'', returncode=0):
        results = [subprocess.CompletedProcess([], returncode, opened, error),
                   subprocess.CompletedProcess([], 0, commands, b'')]
        with patch.object(safety.shutil, 'which', return_value='/usr/sbin/lsof'), \
                patch.object(safety.subprocess, 'run', side_effect=results):
            return safety.ProcessSnapshot()

    def test_cwd_open_file_argv_and_sibling_prefix(self):
        root = Path('/fixture/project')
        for opened, commands in [
                (b'p88\0\nfcwd\0n/fixture/project\0\n', b'88 node\n'),
                (b'p88\0\nf4\0n/fixture/project/node_modules/pkg.js\0\n', b'88 node\n'),
                (b'p88\0\nfcwd\0n/\0\n', b'88 node /fixture/project/server.js\n')]:
            self.assertEqual(self.snapshot(opened, commands).check(root/'node_modules', project=root)['state'], 'busy')
        sibling = self.snapshot(b'p88\0\nfcwd\0n/fixture/project-two\0\n', b'88 node /fixture/project-two/app.js\n')
        self.assertEqual(sibling.check(root/'node_modules', project=root)['state'], 'idle')

    def test_warnings_unavailable_timeout_and_malformed_are_unknown(self):
        for data, err in [(b'', b''), (b'bad format\0', b''), (b'p88\0\nfcwd\0n/\0\n', b'permission denied')]:
            self.assertEqual(self.snapshot(data, error=err).check(Path('/fixture'))['state'], 'unknown')
        with patch.object(safety.shutil, 'which', return_value=None):
            self.assertEqual(safety.ProcessSnapshot().check(Path('/fixture'))['state'], 'unknown')
        with patch.object(safety.shutil, 'which', return_value='lsof'), \
                patch.object(safety.subprocess, 'run', side_effect=subprocess.TimeoutExpired('lsof', 1)):
            self.assertEqual(safety.ProcessSnapshot().check(Path('/fixture'))['state'], 'unknown')

    @unittest.skipUnless(shutil.which('lsof'), 'lsof required for live process check')
    def test_live_project_process_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve()
            child = subprocess.Popen([sys.executable, '-c',
                'import os,sys; os.chdir(sys.argv[1]); print("ready",flush=True); sys.stdin.read()', str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                result = safety.ProcessSnapshot().check(path/'node_modules', project=path)
                self.assertEqual(result['state'], 'busy', result)
            finally:
                child.communicate('', timeout=5)


if __name__ == '__main__':
    unittest.main()
