"""Ops alerts (profile cookie death, session kick) → log.

Sync + stdlib-only so it can be called from anywhere — including the auth
capture paths, which run in executor threads.

Debounced per alert key: at most one alert line per `cooldown` window while the
condition persists. Call `clear_ops_alert(key)` on recovery to re-arm the key so
the next failure alerts immediately.
"""

import threading
import time

from shared.logging_config import setup_logger

logger = setup_logger("ops.alerts")

_lock = threading.Lock()
_last_sent = {}  # key -> epoch of last emitted alert


def send_ops_alert(key: str, message: str, cooldown: float = 6 * 3600) -> bool:
    """Log `message` as an ops alert, at most once per `cooldown` seconds per
    `key`. Never raises."""
    now = time.time()
    with _lock:
        if now - _last_sent.get(key, 0.0) < cooldown:
            return False
        _last_sent[key] = now
    logger.warning("OPS ALERT [%s]: %s", key, message)
    return True


def clear_ops_alert(key: str) -> None:
    """Re-arm `key` after recovery so the next failure alerts immediately."""
    with _lock:
        _last_sent.pop(key, None)
