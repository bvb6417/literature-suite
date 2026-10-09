"""Portable on-disk locations for the downloader.

Everything the program writes (cookies, caches, the browser profile and
temporary runtime folders) lives beside the program instead of under
``%LOCALAPPDATA%`` so that the system drive is never used.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def app_dir():
    from suite_paths import APP_DIR
    return APP_DIR


def state_dir() -> Path:
    """Cookies, school sessions and small caches."""
    return app_dir() / "state"


def browser_data_dir() -> Path:
    return app_dir() / "browser-data"


def browser_profile_dir() -> Path:
    """Persistent Chrome/Edge profile shared by login, verification and downloads."""
    return browser_data_dir() / "login-profile"


def runtime_dir() -> Path:
    """Short-lived per-run folders (browser download targets, XML rendering)."""
    return app_dir() / ".tmp" / "runtime"


def legacy_state_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return root / "LiteratureDownloader"


def relocate_legacy_path(value: str | Path) -> str:
    """Map a configured path inside the old %LOCALAPPDATA% folder to state_dir()."""
    text = str(value or "").strip()
    if not text:
        return text
    path = Path(os.path.expandvars(text)).expanduser()
    try:
        relative = path.resolve().relative_to(legacy_state_dir().resolve())
    except (OSError, ValueError):
        return text
    return str(state_dir() / relative)


# Prefixes of per-run folders created by older versions (and by crashed runs).
_TEMPORARY_PREFIXES = (
    "webvpn-login-",
    "publisher-verify-",
    "elsevier-institution-",
    "elsevier-fulltext-",
)


def _remove_tree(path: Path) -> bool:
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def remove_tree_with_retry(path: Path, attempts: int = 5, delay: float = 1.0) -> bool:
    """Delete a folder that a just-exited browser may still hold open."""
    for attempt in range(attempts):
        if _remove_tree(path):
            return True
        if attempt + 1 < attempts:
            time.sleep(delay)
    return False


@contextmanager
def temporary_folder(prefix: str) -> Iterator[Path]:
    """Per-run folder under runtime_dir(), removed even if a browser lingers."""
    root = runtime_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(root)))
    try:
        yield path
    finally:
        remove_tree_with_retry(path)


def cleanup_stale_runtime(max_age_seconds: float = 3600) -> None:
    """Remove per-run folders left behind by killed or crashed runs."""
    now = time.time()
    for root in (runtime_dir(), state_dir()):
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir() or not child.name.startswith(_TEMPORARY_PREFIXES):
                continue
            try:
                age = now - child.stat().st_mtime
            except OSError:
                continue
            if age >= max_age_seconds:
                _remove_tree(child)


def migrate_legacy_state():
    pass  # New suite must not move another installation's state.
