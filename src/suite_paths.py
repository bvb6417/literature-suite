from pathlib import Path
import os
import sys

RESOURCE_DIR = Path(__file__).resolve().parent
# literature_suite.py sets both variables; the fallbacks keep the modules
# importable straight from a source checkout (src/ beside the entry file).
APP_DIR = Path(os.environ.get('LITERATURE_SUITE_HOME') or RESOURCE_DIR.parent).resolve()
ENTRY_PATH = Path(os.environ.get('LITERATURE_SUITE_ENTRY') or APP_DIR / 'literature_suite.py').resolve()


def absolute_path(value, base=APP_DIR):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(base) / path).resolve()


def entry_command():
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    return [str(executable), '-B', str(ENTRY_PATH)]
