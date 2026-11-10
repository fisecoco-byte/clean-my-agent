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
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clean_my_agent import __version__


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
        parser.error('Git tag must match clean_my_agent.__version__.')
    work = ROOT/'build/release'/args.target
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile',
                    '--name','clean-my-agent','--distpath',str(work/'binary'),
                    '--workpath',str(work/'work'),'--specpath',str(work),
                    '--paths',str(ROOT),'--collect-all','textual','--collect-submodules','clean_my_agent',
                    '--collect-data','mcp','--copy-metadata','mcp',str(ROOT/'scripts/frozen_entry.py')],check=True,cwd=ROOT)
    folder = work/'bundle/clean-my-agent'
    folder.mkdir(parents=True,exist_ok=True)
    executable = 'clean-my-agent.exe' if operating_system == 'windows' else 'clean-my-agent'
    shutil.copy2(work/'binary'/executable, folder/executable)
    for name in ('install.sh','install.ps1','README.md'):
        shutil.copy2(ROOT/name, folder/name)
    shutil.copytree(ROOT/'skills',folder/'skills',dirs_exist_ok=True)
    shutil.copytree(ROOT/'docs',folder/'docs',dirs_exist_ok=True)
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
