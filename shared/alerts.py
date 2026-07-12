"""Ops alerts → Discord (profile cookie death, session kick).

Sync + stdlib-only so it can be called from anywhere — including the auth
capture paths, which run in executor threads where the aiohttp-based
discord_utils helper is unusable.

Debounced per alert key: at most one Discord message per `cooldown` window
while the condition persists. Call `clear_ops_alert(key)` on recovery to
re-arm the key so the next failure alerts immediately.
"""

import json
import threading
import time
import urllib.request

from shared.config import ALERT_WEBHOOK_URL
from shared.logging_config import setup_logger

logger = setup_logger("ops.alerts")

_lock = threading.Lock()
_last_sent = {}  # key -> epoch of last sent alert


def send_ops_alert(key: str, message: str, cooldown: float = 6 * 3600) -> bool:
    """Post `message` to the ops Discord channel, at most once per `cooldown`
    seconds per `key`. Fire-and-forget (background thread); never raises."""
    if not ALERT_WEBHOOK_URL:
        logger.warning("ALERT_WEBHOOK_URL/WEBHOOK_DEFAULT empty — alert dropped: %s", message)
        return False
    now = time.time()
    with _lock:
        if now - _last_sent.get(key, 0.0) < cooldown:
            return False
        _last_sent[key] = now
    threading.Thread(target=_post, args=(message,), daemon=True).start()
    logger.warning("OPS ALERT [%s]: %s", key, message)
    return True


def clear_ops_alert(key: str) -> None:
    """Re-arm `key` after recovery so the next failure alerts immediately."""
    with _lock:
        _last_sent.pop(key, None)


def _post(message: str) -> None:
    try:
        req = urllib.request.Request(
            ALERT_WEBHOOK_URL,
            data=json.dumps({"content": message}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        logger.error("Discord ops alert failed: %s", e)
