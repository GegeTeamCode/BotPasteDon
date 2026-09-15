"""Shared worker helpers: Selenium implicit-wait override, filename sanitizing, file cleanup."""

import os
import re
import signal
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Set

from shared.database import Database
from shared.logging_config import setup_logger


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="worker_")


@contextmanager
def implicit_wait_override(driver, temp_wait: int, default: int = 10):
    """Context manager to safely override and restore implicitly_wait."""
    try:
        driver.implicitly_wait(temp_wait)
        yield
    finally:
        driver.implicitly_wait(default)


def sanitize_filename(filename: str) -> str:
    """Remove path traversal characters from filenames."""
    filename = os.path.basename(filename)
    filename = re.sub(r'[^\w.\-]', '_', filename)
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:200] + ext
    return filename


def cleanup_files(files):
    for f in files:
        # Only path-like entries are deletable. ERP file-info is a dict
        # ({url, evidence_id, name}); the actual downloaded copy lives at a
        # separate local path (tracked as task_data["_downloaded_tmp"]).
        # Skipping dicts here prevents the silent TypeError that used to make
        # this a no-op and leak /tmp/erp_evidence_* forever.
        if not isinstance(f, (str, Path)):
            continue
        try:
            p = Path(f)
            if p.exists():
                p.unlink()
        except Exception:
            pass
