"""G2G Worker — HTTP API server that executes G2G deliveries.

Receives tasks from Coordinator via POST /task.
Supports two modes:
  - API mode (G2G_USE_API=true): Uses REST API, no browser needed
  - Selenium mode (default): Uses Chrome automation
"""

import asyncio
import datetime
import json
import os
import re
import signal
import socket
import tempfile
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from aiohttp import web
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from shared.config import (
    CHROME_BINARY_PATH, HEADLESS_MODE, DATABASE_PATH,
    G2G_USE_API, AUTH_SERVICE_URL,
)
from shared.constants import (
    ORDER_DELIVERING, ORDER_COMPLETED, ORDER_FAILED, ORDER_RETRY_PENDING,
)
from shared.database import Database
from shared.driver_manager import get_driver
from shared.logging_config import setup_logger
from workers.base_worker import cleanup_files, _executor

logger = setup_logger("worker.g2g")

WORKER_PORT = int(os.getenv("WORKER_G2G_PORT", "8002"))

PROCESSING_TASKS: set = set()
driver = None
db: Database = None
_shutdown_event = asyncio.Event()

# API mode components
api_client = None
auth_manager = None


async def _run_sync(func, *args, timeout=180):
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(_executor, partial(func, *args)),
            timeout=timeout,
        )
    except Exception as e:
        logger.error(f"_run_sync error: {e}")
        return None


# ── HTTP API ──

async def handle_task(request: web.Request):
    try:
        task_data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    order_id = task_data.get("order_id", "unknown")
    if order_id in PROCESSING_TASKS:
        return web.json_response({"status": "already_processing"})

    asyncio.create_task(process_task(task_data))
    return web.json_response({"status": "accepted", "order_id": order_id})


async def handle_health(request: web.Request):
    return web.json_response({"status": "ok", "service": "g2g_worker"})


# ── Task Processing ──

COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://localhost:8030")


async def _notify_coordinator(order_id: str, thread_id: str, success: bool):
    """Tell coordinator to lock thread (or log failure)."""
    if not thread_id:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{COORDINATOR_URL}/complete",
                json={"order_id": order_id, "thread_id": thread_id, "success": success},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as e:
        logger.warning(f"Coordinator notify failed: {e}")


# ── Retry/backoff policy ──────────────────────────────────────────────────────

MAX_RETRY_ATTEMPTS = 100

# index = retry_count; clamp at last entry once exhausted.
RETRY_BACKOFF_SECONDS = (
    60, 60, 60, 60, 60,           # first 5: 1 min
    300, 300, 300, 300, 300,       # next 5: 5 min
    1800, 1800, 1800, 1800, 1800,  # next 5: 30 min
    3600,                          # cap: 1 hour thereafter
)

_RETRY_KEYWORDS = (
    "timeout", "timed out",
    "name resolution", "name or service not known",
    "connection reset", "connection refused", "connection aborted",
    "max retries exceeded", "temporary failure",
    "remote disconnected", "service unavailable",
    "502 ", "503 ", "504 ", "500 internal",
    "broken pipe", "eof occurred",
)

_TERMINAL_KEYWORDS = (
    "cannot perform action when order item status",
    "order item is not in delivering",
    "proof file(s) unsupported",
)


def _next_backoff_seconds(retry_count: int) -> int:
    """Return delay before next retry attempt. retry_count is 1-based."""
    idx = max(0, retry_count - 1)
    if idx >= len(RETRY_BACKOFF_SECONDS):
        return RETRY_BACKOFF_SECONDS[-1]
    return RETRY_BACKOFF_SECONDS[idx]


def _classify_error(exc: Exception) -> str:
    """Return one of: 'auth', 'network', 'terminal', 'unknown'."""
    msg = str(exc).lower()
    name = type(exc).__name__.lower()

    if "autherror" in name or "401" in msg or "403" in msg \
            or "auth service" in msg or "jwt" in msg:
        return "auth"

    for kw in _TERMINAL_KEYWORDS:
        if kw in msg:
            return "terminal"

    # Network-level exception types
    if isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror, socket.timeout)):
        return "network"

    for kw in _RETRY_KEYWORDS:
        if kw in msg:
            return "network"

    return "unknown"


def _utcnow_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat()


def _build_retry_payload(task_data: dict, category: str, retry_count: int,
                        err_msg: str) -> dict:
    """Construct the retry_data JSON blob persisted on RETRY_PENDING."""
    delay = _next_backoff_seconds(retry_count)
    next_dt = datetime.datetime.utcnow() + datetime.timedelta(seconds=delay)
    return {
        "task_data": task_data,
        "category": category,
        "retry_count": retry_count,
        "next_retry_at": next_dt.replace(microsecond=0).isoformat(),
        "last_attempt_at": _utcnow_iso(),
        "last_error": err_msg,
    }


async def process_task(task_data: dict):
    order_id = task_data["order_id"]
    thread_id = task_data.get("thread_id", "")
    PROCESSING_TASKS.add(order_id)

    try:
        logger.info(f"Processing: {order_id}, {len(task_data.get('files', []))} files")
        db.update_order_status(order_id, ORDER_DELIVERING)

        if G2G_USE_API and api_client and auth_manager:
            await handle_g2g_api(order_id, task_data)
        else:
            await handle_g2g(
                driver, task_data["order_url"],
                task_data.get("delivery_qty", "1"),
                task_data.get("files", []),
                order_id,
            )

        db.update_order_status(order_id, ORDER_COMPLETED)
        cleanup_files(task_data.get("files", []))
        logger.info(f"Completed: {order_id}")
        await _notify_coordinator(order_id, thread_id, success=True)

    except Exception as e:
        err_msg = str(e)[:500]
        category = _classify_error(e)

        # Read current retry_count to decide cap. handle_g2g_api mutates
        # task_data["skip_steps"] in-place when a per-step fails, so it
        # already carries the resume point.
        existing = db.get_order(order_id) or {}
        prev_count = existing.get("retry_count") or 0
        retry_count = prev_count + 1

        if category == "terminal":
            logger.error(f"Terminal error for {order_id}: {e}")
            db.update_order_status(
                order_id, ORDER_FAILED,
                error_message=f"TERMINAL:{err_msg}",
                retry_count=retry_count,
            )
            cleanup_files(task_data.get("files", []))
            await _notify_coordinator(order_id, thread_id, success=False)
        elif retry_count > MAX_RETRY_ATTEMPTS:
            logger.error(
                f"Retry cap hit for {order_id} after {retry_count} attempts "
                f"(category={category}): {err_msg[:200]}"
            )
            db.update_order_status(
                order_id, ORDER_FAILED,
                error_message=f"RETRY_CAP:{category.upper()}:{err_msg}",
                retry_count=retry_count,
            )
            await _notify_coordinator(order_id, thread_id, success=False)
        else:
            payload = _build_retry_payload(task_data, category, retry_count, err_msg)
            db.mark_retry_attempt(
                order_id,
                retry_data_json=json.dumps(payload, ensure_ascii=False),
                error_message=f"{category.upper()}:{err_msg}",
                retry_count=retry_count,
            )
            delay = _next_backoff_seconds(retry_count)
            logger.warning(
                f"[{order_id}] {category} failure (attempt {retry_count}, "
                f"next in {delay}s, skip_steps={task_data.get('skip_steps', [])}): "
                f"{err_msg[:160]}"
            )
            # Note: do NOT cleanup_files — recovery loop needs them for retry.
            # Files get cleaned on COMPLETED, TERMINAL, or RETRY_CAP paths above.
            # Coordinator is not notified — order is in-flight retry, thread stays open.
    finally:
        # Delete the /tmp/erp_evidence_* copies downloaded this attempt (always
        # re-downloadable from the ERP dict on retry). Prevents the 7.3GB leak.
        cleanup_files(task_data.get("_downloaded_tmp", []))
        PROCESSING_TASKS.discard(order_id)


async def _download_g2g_file(file_info: dict, api_key: str = "") -> Optional[str]:
    """Download file from ERP to temp dir. Returns local path or None."""
    url = file_info.get("url", "")
    evidence_id = file_info.get("evidence_id", "")
    name = file_info.get("name", "evidence")

    if not url or not evidence_id:
        logger.warning(f"Invalid file info: {file_info}")
        return None

    try:
        import aiohttp
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key
        params = {"evidence_id": evidence_id}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params,
                                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    logger.error(f"Download failed: {resp.status} for {evidence_id}")
                    return None
                ext = Path(name).suffix or ".mp4"
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False,
                                                   prefix="erp_evidence_")
                tmp.write(await resp.read())
                tmp.close()
                logger.info(f"Downloaded evidence: {name} ({tmp.name})")
                return tmp.name
    except Exception as e:
        logger.error(f"Download error for {evidence_id}: {e}")
        return None


async def _qty_already_delivered(api_id: str, qty: int, auth) -> bool:
    """Confirm via G2G marketplace truth that an 'already delivering' rejection on
    submit_delivered_qty is an idempotent re-dispatch (qty was set by a prior
    dispatch), NOT a real failure. Returns True only if delivered_qty already
    covers our qty AND the order isn't cancelled/refunded — so a genuinely
    cancelled order still re-raises -> terminal -> FAILED.
    """
    try:
        detail = await api_client.call_with_retry(
            api_client.get_order_detail, api_id, auth, auth.seller_id)
    except Exception as e:
        logger.warning(f"[{api_id}] cannot confirm delivered_qty, treating as failure: {e}")
        return False
    status = str(detail.get("order_item_status") or "").lower()
    delivered = int(detail.get("delivered_qty") or 0)
    if status in ("cancelled", "canceled", "refunded", "cancellation_requested"):
        return False
    return delivered >= qty


async def _undelivered_qty(api_id: str, auth):
    """Qty còn phải giao trên G2G = purchased - delivered - in_prog, hoặc None nếu
    không đọc được / đơn đã terminal. Dùng để phục hồi khi submit_delivered_qty bị
    G2G từ chối 'deliver more than undelivered quantity' — xảy ra với đơn giao TỪNG
    PHẦN (trader đã giao tay một phần trước): ta submit đúng phần còn lại thay vì gửi
    trọn order_quantity."""
    try:
        detail = await api_client.call_with_retry(
            api_client.get_order_detail, api_id, auth, auth.seller_id)
    except Exception as e:
        logger.warning(f"[{api_id}] cannot read undelivered_qty: {e}")
        return None
    status = str(detail.get("order_item_status") or "").lower()
    if status in ("cancelled", "canceled", "refunded", "cancellation_requested"):
        return None
    purchased = int(detail.get("purchased_qty") or 0)
    delivered = int(detail.get("delivered_qty") or 0)
    in_prog = int(detail.get("in_prog_qty") or 0)
    return max(purchased - delivered - in_prog, 0)


async def handle_g2g_api(order_id: str, task_data: dict):
    """Delivery via REST API — runs steps individually to track progress."""
    import re
    auth = await auth_manager.get_auth()
    qty = int(task_data.get("delivery_qty", "1"))
    raw_files = task_data.get("files", [])
    message_path = Path("message.txt")
    message = ""
    if message_path.exists():
        message = message_path.read_text(encoding="utf-8").strip() or "Done"

    # Extract full order_item_id (with -1 suffix) from order_url
    api_id = order_id
    order_url = task_data.get("order_url", "")
    if order_url:
        m = re.search(r'order/item/([A-Za-z0-9\-]+)', order_url)
        if m:
            api_id = m.group(1)

    skip_steps = set(task_data.get("skip_steps", []))

    # Download ERP dict files to local paths.
    # Track the downloaded /tmp copies so process_task can delete them after the
    # attempt (re-downloaded on retry). Discord PROOF_DIR paths (str) pass through
    # and keep their own cleanup-on-terminal in process_task.
    erp_api_key = task_data.get("erp_api_key", "")
    files = []
    downloaded_tmp = []
    task_data["_downloaded_tmp"] = downloaded_tmp  # cleaned in process_task.finally
    for fp in raw_files:
        if isinstance(fp, dict):
            local = await _download_g2g_file(fp, erp_api_key)
            if local:
                files.append(local)
                downloaded_tmp.append(local)
        else:
            files.append(fp)
    if files != raw_files and files:
        logger.info(f"[{order_id}] Downloaded {len(files)}/{len(raw_files)} files from ERP")

    # Run each step, tracking which succeed
    completed_steps = set(skip_steps)

    # Step 1: Submit qty
    if "qty" not in completed_steps:
        try:
            await api_client.call_with_retry(
                api_client.submit_delivered_qty, api_id, qty, auth, auth.seller_id)
            completed_steps.add("qty")
            logger.info(f"[{order_id}] delivered_qty={qty} OK")
        except Exception as e:
            # Idempotent-success guard: "cannot perform action when order item
            # status is delivering/delivered" means the qty was ALREADY set by a
            # prior dispatch (e.g. double-dispatch from a duplicate scanner). Do
            # NOT mark FAILED — confirm via marketplace truth, then resume the
            # remaining steps. A cancelled/refunded order (qty mismatch) re-raises
            # and stays terminal. See .ai decision 2026-06-14.
            if "cannot perform action when order item status" in str(e).lower() \
                    and await _qty_already_delivered(api_id, qty, auth):
                completed_steps.add("qty")
                task_data["skip_steps"] = list(completed_steps)
                logger.warning(
                    f"[{order_id}] qty already delivered on G2G (idempotent re-dispatch); "
                    f"resuming proof/chat without re-submitting qty")
            elif "more than undelivered quantity" in str(e).lower():
                # Đơn giao TỪNG PHẦN (vd trader đã giao tay một phần trước): G2G còn
                # undelivered < order_quantity ta gửi → 400. Đọc lại undelivered thật
                # rồi submit ĐÚNG phần còn lại (không gửi trọn order_quantity).
                remaining = await _undelivered_qty(api_id, auth)
                if remaining is None:  # đọc lỗi / đơn terminal → giữ nguyên fail
                    task_data["skip_steps"] = list(completed_steps)
                    raise
                if remaining > 0:
                    await api_client.call_with_retry(
                        api_client.submit_delivered_qty, api_id, remaining, auth, auth.seller_id)
                    logger.info(
                        f"[{order_id}] delivered_qty={remaining} OK (phần còn lại; "
                        f"order_quantity={qty} bị chặn do đã giao một phần)")
                else:  # undelivered=0 → đã giao đủ, coi như idempotent
                    logger.warning(
                        f"[{order_id}] undelivered=0 → qty đã giao đủ; resuming proof/chat")
                completed_steps.add("qty")
                task_data["skip_steps"] = list(completed_steps)
            else:
                task_data["skip_steps"] = list(completed_steps)
                raise

    # Step 2: Upload proof
    if "proof" not in completed_steps and files:
        try:
            await api_client._upload_proofs(api_id, files, auth, auth.seller_id)
            completed_steps.add("proof")
        except Exception:
            task_data["skip_steps"] = list(completed_steps)
            raise

    # Step 3: Send chat (retry on auth error)
    if "chat" not in completed_steps and message:
        chat_ok = False
        for attempt in range(2):
            try:
                await api_client._send_chat(api_id, message, auth, auth.seller_id)
                completed_steps.add("chat")
                chat_ok = True
                break
            except Exception as e:
                err_str = str(e)
                if "401" in err_str or "auth error" in err_str.lower():
                    logger.warning(f"[{order_id}] Chat auth error (attempt {attempt+1}), refreshing JWT")
                    try:
                        auth = await auth_manager.get_auth()
                    except Exception:
                        break
                else:
                    logger.warning(f"[{order_id}] Chat failed (non-fatal): {e}")
                    break
        if not chat_ok:
            logger.warning(f"[{order_id}] Chat not sent after retries")


# ── G2G Delivery Logic ──

async def handle_g2g(drv, url, qty, file_paths, order_id: str = ""):
    def _sync():
        drv.switch_to.window(drv.window_handles[0])
        drv.get(url)
        wait = WebDriverWait(drv, 20)
        import time

        try:
            inp = wait.until(EC.presence_of_element_located(
                (By.XPATH, "//input[@data-attr='order-item-add-delivered-qty-input']")))
            inp.click()
            inp.clear()
            inp.send_keys(qty)
            logger.info(f"Entered quantity: {qty}")
        except Exception as e:
            logger.warning(f"Quantity input error: {e}")

        has_cancel = bool(drv.find_elements(By.CSS_SELECTOR, "div.g-alert-box.bg-negative"))

        try:
            btn = wait.until(EC.element_to_be_clickable(
                (By.XPATH, "//button[@data-attr='order-item-add-delivered-qty-submit-btn']")))
            drv.execute_script("arguments[0].click();", btn)
            time.sleep(2)

            if has_cancel:
                for _ in range(10):
                    overlay = drv.find_elements(By.XPATH,
                        "//button[contains(., 'Continue') and contains(@class, 'bg-primary')]")
                    if overlay and overlay[0].is_displayed():
                        drv.execute_script("arguments[0].click();", overlay[0])
                        time.sleep(2)
                        break
                    time.sleep(0.5)
        except Exception as e:
            logger.warning(f"Submit qty error: {e}")

        try:
            drv.execute_script("arguments[0].click();",
                wait.until(EC.element_to_be_clickable(
                    (By.XPATH, "//span[contains(text(), 'Proof gallery')]"))))
            wait.until(EC.presence_of_element_located((By.ID, "fileUploader"))).send_keys(
                "\n".join([os.path.abspath(f) for f in file_paths]))
            logger.info(f"Uploaded {len(file_paths)} files")
            time.sleep(4)

            for i in range(20):
                try:
                    sub_btns = drv.find_elements(By.XPATH,
                        "//button[@data-attr='order-item-delivery-proof-dialog-submit-btn']")
                    if not sub_btns:
                        break
                    sub = sub_btns[0]
                    if sub.is_displayed() and sub.is_enabled():
                        drv.execute_script("arguments[0].click();", sub)
                        time.sleep(1)
                    else:
                        time.sleep(0.5)
                except:
                    time.sleep(1)
        except Exception as e:
            raise e

        # Chat
        try:
            chat_btn = wait.until(EC.element_to_be_clickable(
                (By.XPATH, "//a[@data-attr='order-item-chat-btn']")))
            drv.execute_script("arguments[0].click();", chat_btn)
            time.sleep(3)

            if len(drv.window_handles) > 1:
                drv.switch_to.window(drv.window_handles[-1])
                try:
                    editor = wait.until(EC.presence_of_element_located(
                        (By.CSS_SELECTOR, ".ProseMirror.toastui-editor-contents")))
                    msg = "Done"
                    msg_path = Path("message.txt")
                    if msg_path.exists():
                        msg = msg_path.read_text(encoding="utf-8").strip()

                    drv.execute_script("""
                        var el = arguments[0]; var txt = arguments[1];
                        el.innerText = txt;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    """, editor, msg)
                    time.sleep(1)

                    send_btn = drv.find_element(By.XPATH, "//button[.//i[text()='send']]")
                    drv.execute_script("arguments[0].click();", send_btn)
                    logger.info("Chat message sent")
                    time.sleep(2)
                except Exception as e:
                    logger.warning(f"Chat error: {e}")
        except:
            pass
        finally:
            if len(drv.window_handles) > 1:
                drv.close()
            drv.switch_to.window(drv.window_handles[0])

    await _run_sync(_sync, timeout=180)


# ── Main ──

async def run_worker():
    global driver, db, api_client, auth_manager

    db = Database(DATABASE_PATH)

    # API mode: use REST API instead of Selenium
    if G2G_USE_API:
        from shared.g2g_auth import G2GAuthManager
        from shared.g2g_api import G2GAPIClient

        auth_manager = G2GAuthManager(auth_url=AUTH_SERVICE_URL)
        api_client = G2GAPIClient(auth_manager)
        logger.info("Running in API mode (no browser)")
    else:
        driver = get_driver(
            profile_dir="chrome_profile_g2g_worker",
            headless=HEADLESS_MODE,
            chrome_binary=CHROME_BINARY_PATH,
        )
        logger.info("Running in Selenium mode")

    app = web.Application()
    app.router.add_post("/task", handle_task)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WORKER_PORT)
    await site.start()
    logger.info(f"G2G Worker listening on port {WORKER_PORT} (PID: {os.getpid()})")

    async def heartbeat():
        while not _shutdown_event.is_set():
            db.update_heartbeat("worker_g2g", os.getpid())
            await asyncio.sleep(30)
    asyncio.create_task(heartbeat())

    # Recovery: pick up orders in RETRY_PENDING whose next_retry_at is due.
    # Also picks up legacy FAILED orders with old "JWT_EXPIRED:" prefix so
    # the in-flight queue from the previous deployment doesn't get stranded.
    async def recover_pending_retries():
        while not _shutdown_event.is_set():
            await asyncio.sleep(60)
            if not (G2G_USE_API and api_client and auth_manager):
                continue

            now = datetime.datetime.utcnow()
            pending = db.get_orders_by_status("g2g", ORDER_RETRY_PENDING)
            # Backward-compat sweep: legacy auth-only retries still in FAILED.
            legacy = [
                o for o in db.get_orders_by_status("g2g", ORDER_FAILED)
                if (o.get("error_message") or "").startswith("JWT_EXPIRED:")
            ]

            for order in pending + legacy:
                order_id = order["order_id"]
                if order_id in PROCESSING_TASKS:
                    continue

                retry_json = order.get("retry_data")
                if not retry_json:
                    continue
                try:
                    payload = json.loads(retry_json)
                except Exception:
                    continue

                # Normalize: new format has "task_data" key; legacy is raw task_data.
                if isinstance(payload, dict) and "task_data" in payload:
                    task_data = payload.get("task_data") or {}
                    category = payload.get("category", "auth")
                    next_at = payload.get("next_retry_at")
                else:
                    task_data = payload if isinstance(payload, dict) else {}
                    category = "auth"
                    next_at = None

                # Respect backoff schedule.
                if next_at:
                    try:
                        due = datetime.datetime.fromisoformat(next_at)
                        if due > now:
                            continue
                    except Exception:
                        pass

                # Auth-category requires healthy JWT before retry; if auth still
                # broken, defer the whole sweep — every order would hit the same wall.
                if category == "auth":
                    try:
                        await auth_manager.get_auth()
                    except Exception:
                        logger.info("Auth still unhealthy — deferring retry sweep")
                        break

                if not task_data.get("order_id"):
                    task_data["order_id"] = order_id
                if "thread_id" not in task_data:
                    task_data["thread_id"] = order.get("discord_thread_id", "") or ""

                logger.info(
                    f"Retrying {order_id} (category={category}, "
                    f"attempt={(order.get('retry_count') or 0) + 1}, "
                    f"skip={task_data.get('skip_steps', [])})"
                )
                asyncio.create_task(process_task(task_data))
    asyncio.create_task(recover_pending_retries())

    await _shutdown_event.wait()

    logger.info("Shutting down...")
    await runner.cleanup()
    if driver:
        driver.quit()
    if api_client:
        api_client.close()
    logger.info("Worker shut down cleanly")


def main():
    def handle_signal(sig, frame):
        _shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        _shutdown_event.set()


if __name__ == "__main__":
    main()
