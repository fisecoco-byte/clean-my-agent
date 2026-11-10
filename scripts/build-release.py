"""Build on the target OS. No cross-compilation or download-on-first-run."""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
__version__ = tomllib.loads((ROOT/'Cargo.toml').read_text())['package']['version']


def copy_executable(source, destination):
    # Never overwrite a previously executed Mach-O inode: macOS can retain the
    # old signature in its executable cache and kill the replacement with SIGKILL.
    handle, pending = tempfile.mkstemp(prefix='.cma-binary-', dir=destination.parent)
    os.close(handle)
    try:
        shutil.copy2(source, pending)
        os.replace(pending, destination)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', required=True, choices=['macos-arm64','macos-x64','linux-arm64','linux-x64','windows-x64'])
    args = parser.parse_args()
    operating_system = {'Darwin':'macos','Linux':'linux','Windows':'windows'}[platform.system()]
    arch = 'arm64' if platform.machine().lower() in ('arm64','aarch64') else 'x64'
    if args.target != f'{operating_system}-{arch}':
        parser.error('Build each package on its matching OS and architecture.')
    tag = 'v'+__version__
    if os.environ.get('GITHUB_REF_TYPE') == 'tag' and os.environ.get('GITHUB_REF_NAME') != tag:
        parser.error('Git tag must match the package version in Cargo.toml.')
    work = ROOT/'build/release'/args.target
    work.mkdir(parents=True, exist_ok=True)
    cargo = os.environ.get('CARGO') or shutil.which('cargo') or str(Path.home()/'.cargo/bin/cargo')
    subprocess.run([cargo,'build','--release','--locked'],check=True,cwd=ROOT)
    executable = 'clean-my-agent.exe' if operating_system == 'windows' else 'clean-my-agent'
    (work/'binary').mkdir(exist_ok=True)
    copy_executable(ROOT/'target/release'/executable, work/'binary'/executable)
    subprocess.run([sys.executable,str(ROOT/'scripts/smoke-release.py'),str(work/'binary'/executable)],check=True,cwd=ROOT)
    folder = work/'bundle/clean-my-agent'
    folder.mkdir(parents=True,exist_ok=True)
    executable = 'clean-my-agent.exe' if operating_system == 'windows' else 'clean-my-agent'
    copy_executable(work/'binary'/executable, folder/executable)
    for name in ('install.sh','install.ps1','README.md','README_CN.md'):
        shutil.copy2(ROOT/name, folder/name)
    (folder/'assets').mkdir(exist_ok=True)
    for name in ('logo.png', 'readme-logo.png'):
        shutil.copy2(ROOT/'assets'/name, folder/'assets'/name)
    shutil.copytree(ROOT/'skills',folder/'skills',dirs_exist_ok=True)
    shutil.copytree(ROOT/'docs',folder/'docs',dirs_exist_ok=True)
    metadata = subprocess.check_output([str(work/'binary'/executable),'--build-info'],text=True)
    (folder/'BUILD_INFO.json').write_text(metadata,encoding='utf-8')
    shutil.copy2(ROOT/'Cargo.lock',folder/'Cargo.lock')
    shutil.copytree(ROOT/'assets/loading',folder/'assets/loading',dirs_exist_ok=True)
    (folder/'VERSION').write_text(tag+'\n',encoding='utf-8')
    (folder/'BUNDLE_TARGET').write_text(args.target+'\n',encoding='utf-8')
    out = ROOT/'dist'
    out.mkdir(exist_ok=True)
    prefix = 'clean-my-agent-'+tag+'-'+args.target
    if operating_system == 'windows':
        asset = out/(prefix+'.zip')
        with zipfile.ZipFile(asset,'w',zipfile.ZIP_DEFLATED) as archive:
            for p in sorted(folder.rglob('*')):
                if p.is_file(): archive.write(p,p.relative_to(folder.parent))
    else:
        asset = out/(prefix+'.tar.gz')
        with tarfile.open(asset,'w:gz') as archive:
            archive.add(folder,arcname='clean-my-agent')
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    (out/(asset.name+'.sha256')).write_text(f'{digest}  {asset.name}\n',encoding='ascii',newline='\n')
    print(asset)


if __name__ == '__main__':
    main()
