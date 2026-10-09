"""One source of truth for shared GUI / CLI defaults."""
from pathlib import Path
from suite_paths import APP_DIR


def search_limit(config):
    value=int((config.get('literature_search') or {}).get('limit',20))
    if not 1<=value<=100:
        raise ValueError('全局检索上限应为 1–100')
    return value


def pdf_directory(config,base=None):
    value=((config.get('paths') or {}).get('pdf_dir')
           or (config.get('metadata') or {}).get('pdf_dir')
           or (config.get('download') or {}).get('output_dir') or 'downloads')
    path=Path(value).expanduser()
    return (path if path.is_absolute() else Path(base or APP_DIR)/path).resolve()
