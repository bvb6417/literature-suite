#!/usr/bin/env python3
"""文献工作台：不带参数打开 GUI，带子命令调用 CLI。

Commands: keyword / search / doi / expand / enrich / download / check.
Run ``python literature_suite.py --help`` for the full list.

Application modules live in ``src/``. config.local.json and all user data
(downloads, cookies, browser profile, metadata library) stay beside this file.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
ENTRY_PATH = Path(sys.executable).resolve() if getattr(sys, 'frozen', False) else Path(__file__).resolve()
SOURCE_DIR = Path(getattr(sys, '_MEIPASS', APP_DIR)) / 'src'

os.environ['LITERATURE_SUITE_HOME'] = str(APP_DIR)
os.environ['LITERATURE_SUITE_ENTRY'] = str(ENTRY_PATH)
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        from suite_gui import launch
        return launch()
    from suite_cli import main as cli_main
    return cli_main(args)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
