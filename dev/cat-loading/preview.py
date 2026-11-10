"""Standalone terminal preview. No application imports and no filesystem scans."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="小猫翻书动画预览，Ctrl+C 退出")
    parser.add_argument("--seconds", type=float, default=0, help="预览秒数；默认循环播放")
    args = parser.parse_args()
    if not sys.stdout.isatty():
        parser.error("请在交互式终端中预览")
    data = json.loads(Path(__file__).with_name("frames.json").read_text())
    colors = [tuple(bytes.fromhex(color[1:])) for color in data["colors"]]
    start = time.monotonic()
    previous_size = None
    try:
        sys.stdout.write("\033[?1049h\033[?25l")
        while not args.seconds or time.monotonic() - start < args.seconds:
            index = int((time.monotonic() - start) * data["fps"]) % len(data["frames"])
            frame = data["frames"][index]
            width, height = shutil.get_terminal_size()
            left = max(0, (width - data["cols"]) // 2)
            top = max(3, (height - data["rows"] - 4) // 2)
            lines = []
            if previous_size != (width, height):
                lines.append("\033[2J\033[H\033[38;2;94;220;198m  CLEAN MY AGENT  /  PREVIEW\033[0m")
                previous_size = (width, height)
            for y, row in enumerate(frame["text"].splitlines()):
                if top + y >= height:
                    break
                lines.append(f"\033[{top + y};{left + 1}H")
                previous = None
                for x, char in enumerate(row[:max(0, width - left)]):
                    color = frame["colors"][y * data["cols"] + x]
                    if color != previous:
                        red, green, blue = colors[color]
                        lines.append(f"\033[38;2;{red};{green};{blue}m")
                        previous = color
                    lines.append(char)
            sys.stdout.write("".join(lines))
            sys.stdout.flush()
            time.sleep(1 / data["fps"])
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
