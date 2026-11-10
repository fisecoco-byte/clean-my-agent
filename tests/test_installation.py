import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == 'nt', 'Bash installer runs on Unix')
class InstallationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base/"home space's"
        self.home.mkdir()
        self.env = {**os.environ,'HOME':str(self.home),'XDG_CONFIG_HOME':str(self.home/'.config'),
                    'ZDOTDIR':str(self.home),'CMA_BIN_DIR':str(self.home/'.local/bin')}
        self.fake = self.base/'fixture'
        self.fake.write_text('#!/bin/sh\nprintf "clean-my-agent 0.1.0\\n"\n')
        self.fake.chmod(0o755)
        self.bin = self.home/'.local/bin/clean-my-agent'

    def install(self,*args):
        return subprocess.run(['bash',str(ROOT/'install.sh'),*args],env=self.env,cwd=self.base,
                              capture_output=True,text=True,timeout=15)

    def test_offline_install_idempotent_path_and_uninstall_preserve_data(self):
        profile=self.home/'.bashrc'
        profile.write_text('export USER_EXISTING_SETTING=keep\n')
        login=self.home/'.profile'
        login.write_text('export LOGIN_SETTING=keep\n')
        data=self.home/'.local/state/clean-my-agent/batch/data'
        data.parent.mkdir(parents=True)
        data.write_text('keep')
        for _ in range(2):
            result=self.install('--from',str(self.fake))
            self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(profile.read_text().count('# >>> clean-my-agent PATH >>>'),1)
        self.assertFalse((self.home/'.bash_profile').exists())
        self.assertIn('LOGIN_SETTING=keep',login.read_text())
        result=subprocess.run(['bash','-c','. "$HOME/.bashrc"; clean-my-agent --version'],
                              env=self.env,capture_output=True,text=True)
        self.assertEqual(result.stdout.strip(),'clean-my-agent 0.1.0',result.stderr)
        result=subprocess.run(['bash','-c','. "$HOME/.bashrc"; . "$HOME/.bashrc"; printf "%s" "$PATH"'],
                              env=self.env,capture_output=True,text=True)
        self.assertEqual(result.stdout.split(os.pathsep).count(str(self.bin.parent)),1)
        result=self.install('--uninstall')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(self.bin.exists())
        self.assertIn('USER_EXISTING_SETTING=keep',profile.read_text())
        self.assertNotIn('clean-my-agent',profile.read_text())
        self.assertEqual(data.read_text(),'keep')

    def test_refuses_unowned_command_and_failed_upgrade_keeps_installed_binary(self):
        self.bin.parent.mkdir(parents=True)
        self.bin.write_text('unmanaged')
        result=self.install('--from',str(self.fake),'--no-path')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.bin.read_text(),'unmanaged')
        self.bin.unlink()
        result=self.install('--from',str(self.fake),'--no-path')
        self.assertEqual(result.returncode,0,result.stderr)
        installed=self.bin.read_bytes()
        self.fake.write_text('#!/bin/sh\nexit 1\n')
        result=self.install('--from',str(self.fake),'--no-path')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.bin.read_bytes(),installed)
        self.assertFalse((self.home/'.bashrc').exists())

    def test_failed_checksum_never_installs_or_runs_payload(self):
        mockbin=self.base/'tools'
        mockbin.mkdir()
        fakecurl=mockbin/'curl'
        fakecurl.write_text('''#!/bin/bash
url=""; out=""
while [[ $# -gt 0 ]]; do
  case "$1" in -o) out="$2"; shift 2;; https:*) url="$1"; shift;; *) shift;; esac
done
case "$url" in
  */SHA256SUMS) printf '%064d  clean-my-agent-v0.1.0-%s-%s.tar.gz\\n' 0 "$TEST_PLATFORM" "$TEST_ARCH" > "$out";;
  *) printf 'invalid archive' > "$out";;
esac
''')
        fakecurl.chmod(0o755)
        import platform
        self.env.update(PATH=str(mockbin)+os.pathsep+os.environ['PATH'],
                        TEST_PLATFORM='macos' if platform.system()=='Darwin' else 'linux',
                        TEST_ARCH='arm64' if platform.machine() in ('arm64','aarch64') else 'x64')
        result=self.install('--version','v0.1.0')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('SHA256',result.stderr)
        self.assertFalse(self.bin.exists())


class PortableTests(unittest.TestCase):
    def test_readonly_walk_pruning_hardlinks_and_unknown_reclaim(self):
        from clean_my_agent import portable, policy
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            p=root/'log/old.log'
            p.parent.mkdir()
            p.write_bytes(b'x'*4096)
            os.utime(p,(1,1))
            os.link(p,p.parent/'hard.log')
            other=root/'worktrees/demo/node_modules'
            other.mkdir(parents=True)
            (other/'pkg').write_text('dependency')
            rules=object()  # This adapter never authorizes a cleanup candidate.
            doc=portable.scan({'codex':root},{'codex'},rules,full=True)
            self.assertFalse(doc.items)
            self.assertEqual(doc.rows['codex','logs'][1],portable.allocated(p))
            quick=portable.scan({'codex':root},{'codex'},rules,full=False)
            self.assertIn(str(root/'worktrees'),quick.deferred)
            sizes,_,_=portable.measure(other)
            self.assertIsNone(sizes['reclaimable_bytes'])
            self.assertIsNone(sizes['shared_bytes'])
            report=portable.dependencies(root)
            self.assertEqual(report['entries'][0]['status'],'retained')
