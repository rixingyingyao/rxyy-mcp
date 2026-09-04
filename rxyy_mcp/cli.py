"""pip 入口：``rxyy-mcp`` / ``rxyy-mcp hub --daemon`` / ``rxyy-mcp console``。"""
from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

_COMMANDS = {
    "hub": "hub.py",
    "console": "console/app.py",
    "watchdog": "watchdog.py",
    "server": "server.py",
}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    parser = argparse.ArgumentParser(
        prog="rxyy-mcp",
        description="rxyy mcp hub / desktop console",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="hub",
        choices=sorted(_COMMANDS),
        help="hub=MCP+控制台核心（默认）；console=桌面壳；watchdog/server=配套进程",
    )
    args, rest = parser.parse_known_args(argv)
    target = APP_DIR / _COMMANDS[args.command]
    if not target.is_file():
        raise SystemExit("找不到 %s" % target)
    sys.argv = [str(target), *rest]
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
