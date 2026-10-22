"""Shared worker helpers: Selenium implicit-wait override, filename sanitizing, file cleanup,
and the CHAT_PENDING bookkeeping both workers use to re-send a failed buyer message."""

import datetime
import json
import os
import re
import signal
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import List, Set, Tuple

from shared.alerts import send_ops_alert
from shared.constants import ORDER_COMPLETED
from shared.database import Database
from shared.logging_config import setup_logger

logger = setup_logger("worker.base")


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


def read_message() -> str:
    """Lời nhắn gửi người mua, đọc lại mỗi lần để sửa ``message.txt`` có hiệu lực ngay.

    File rỗng → "Done"; không có file → "" (bỏ bước chat).
    """
    path = Path("message.txt")
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip() or "Done"


# ── Gửi lại lời nhắn khi bước chat hỏng ──────────────────────────────────────
# Hàng đã giao đủ trên sàn nên đơn vẫn COMPLETED; dấu nằm ở error_message để
# dashboard thấy, chi tiết ở retry_data = {"chat": {...}}. Vòng hồi phục của mỗi
# worker gửi lại RIÊNG lời nhắn, giãn dần, hết lượt thì đổi sang CHAT_STUCK.

CHAT_PENDING_PREFIX = "CHAT_PENDING:"
CHAT_STUCK_PREFIX = "CHAT_STUCK:"
CHAT_MAX_TRIES = 5
# Chờ sau lượt thứ n (n = 1..4) trước khi thử lượt n+1.
CHAT_RETRY_BACKOFF_SECONDS = (60, 300, 1800, 3600)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


def load_chat_payload(order: dict) -> dict:
    try:
        data = json.loads(order.get("retry_data") or "{}")
    except (TypeError, ValueError):
        return {}
    chat = data.get("chat") if isinstance(data, dict) else None
    return dict(chat) if isinstance(chat, dict) else {}


def mark_chat_pending(db: Database, platform: str, order_id: str, payload: dict,
                      error: str, tries: int = 1) -> bool:
    """Ghi lượt chat thất bại thứ ``tries``. Trả False khi đã hết lượt (CHAT_STUCK)."""
    payload = dict(payload)
    payload["tries"] = tries
    payload["last_error"] = str(error)[:300]
    payload.pop("next_retry_at", None)
    if tries >= CHAT_MAX_TRIES:
        db.update_order_status(
            order_id, ORDER_COMPLETED,
            error_message=f"{CHAT_STUCK_PREFIX}{tries} luot: {str(error)[:240]}",
            retry_data=json.dumps({"chat": payload}, ensure_ascii=False))
        send_ops_alert(
            f"chat-stuck:{platform}:{order_id}",
            f"⚠️ [{platform}] Đơn {order_id} đã giao nhưng KHÔNG gửi được lời nhắn cho người "
            f"mua sau {tries} lượt ({str(error)[:160]}) — nhắn tay trên sàn.",
            cooldown=24 * 3600)
        return False
    delay = CHAT_RETRY_BACKOFF_SECONDS[min(tries, len(CHAT_RETRY_BACKOFF_SECONDS)) - 1]
    payload["next_retry_at"] = (_utcnow() + datetime.timedelta(seconds=delay)).isoformat()
    db.update_order_status(
        order_id, ORDER_COMPLETED,
        error_message=f"{CHAT_PENDING_PREFIX}luot {tries}: {str(error)[:240]}",
        retry_data=json.dumps({"chat": payload}, ensure_ascii=False))
    return True


def due_chat_retries(db: Database, platform: str) -> List[Tuple[dict, dict]]:
    """[(order, payload)] của các đơn CHAT_PENDING đã tới hạn gửi lại."""
    now = _utcnow()
    due = []
    for order in db.get_orders_by_error_prefix(platform, ORDER_COMPLETED, CHAT_PENDING_PREFIX):
        payload = load_chat_payload(order)
        nxt = payload.get("next_retry_at")
        if nxt:
            try:
                if datetime.datetime.fromisoformat(nxt) > now:
                    continue
            except ValueError:
                pass
        due.append((order, payload))
    return due


def clear_chat_pending(db: Database, order_id: str) -> None:
    db.update_order_status(order_id, ORDER_COMPLETED, error_message="", retry_data=None)


def complete_order(db: Database, order_id: str) -> None:
    """Đặt COMPLETED; xoá dấu CHAT_* còn sót từ lần giao trước (ERP gửi lại task),
    kẻo vòng hồi phục gửi trùng lời nhắn."""
    existing = db.get_order(order_id) or {}
    if (existing.get("error_message") or "").startswith((CHAT_PENDING_PREFIX, CHAT_STUCK_PREFIX)):
        clear_chat_pending(db, order_id)
    else:
        db.update_order_status(order_id, ORDER_COMPLETED)
