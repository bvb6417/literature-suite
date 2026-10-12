"""文献工作台。双击运行，配置及数据存放在本目录。"""
from pathlib import Path
import os
import sys

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
SOURCE_DIR = Path(getattr(sys, '_MEIPASS', APP_DIR)) / 'src'
os.environ['LITERATURE_SUITE_HOME'] = str(APP_DIR)
os.environ['LITERATURE_SUITE_ENTRY'] = str(Path(sys.executable).resolve() if getattr(sys, 'frozen', False) else Path(__file__).resolve())
sys.path.insert(0, str(SOURCE_DIR))

def main():
    if sys.argv[1:]:
        from suite_cli import main as cli_main
        return cli_main(sys.argv[1:])
    from preview_app import launch
    return launch()

if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    import faulthandler
    with (APP_DIR/'crash.log').open('a', encoding='utf-8') as crash:
        faulthandler.enable(crash)
        raise SystemExit(main())
