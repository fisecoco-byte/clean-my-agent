"""Move to macOS Trash through Foundation; never fall back to permanent deletion."""
import os
from pathlib import Path
import subprocess
import sys


SCRIPT = '''use framework "Foundation"
on run argv
    set sourceURL to current application's NSURL's fileURLWithPath:(item 1 of argv)
    set manager to current application's NSFileManager's defaultManager()
    set {ok, destination, failure} to manager's trashItemAtURL:sourceURL resultingItemURL:(reference) |error|:(reference)
    if not ok then error (failure's localizedDescription() as text)
    return destination's |path|() as text
end run'''


def available():
    return sys.platform == 'darwin' and Path('/usr/bin/osascript').is_file()


def move(path):
    if not available():
        raise ValueError('独立仓库移入废纸篓目前仅支持 macOS')
    before = path.lstat()
    # Pass the path as an argument, never interpolate it into executable source.
    result = subprocess.run(['/usr/bin/osascript', '-l', 'AppleScript', '-e', SCRIPT, str(path)],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError(os.fsdecode(result.stderr[-2000:]).strip() or '系统废纸篓操作失败')
    destination = Path(os.fsdecode(result.stdout).removesuffix('\n'))
    if not destination.is_absolute() or destination == path or os.path.lexists(path):
        raise ValueError('未能核实废纸篓中的目录位置')
    after = destination.lstat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError('废纸篓中的目录身份无法核实')
    return destination
