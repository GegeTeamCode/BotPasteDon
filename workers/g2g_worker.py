"""G2G Worker — HTTP API server that executes G2G deliveries.

Receives tasks from Coordinator via POST /task.
Supports two modes:
  - API mode (G2G_USE_API=true): Uses REST API, no browser needed
  - Selenium mode (default): Uses Chrome automation
"""

import asyncio
import os
import re
import signal
from pathlib import Path
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
from shared.constants import ORDER_DELIVERING, ORDER_COMPLETED, ORDER_FAILED
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
        err_msg = str(e)[:200]
        is_auth_err = ("AuthError" in type(e).__name__
                        or "401" in err_msg or "403" in err_msg
                        or "auth service error" in err_msg.lower()
                        or "jwt" in err_msg.lower())
        if is_auth_err:
            logger.warning(f"Auth error for {order_id} — will retry when JWT refreshes")
            # Save task data as JSON so retry can pick up files + skip completed steps
            import json
            retry_data = json.dumps(task_data)
            db.update_order_status(order_id, ORDER_FAILED,
                                   error_message=f"JWT_EXPIRED:{err_msg}",
                                   retry_data=retry_data)
        else:
            logger.error(f"Task error for {order_id}: {e}")
            db.update_order_status(order_id, ORDER_FAILED, error_message=err_msg)
            await _notify_coordinator(order_id, thread_id, success=False)
    finally:
        PROCESSING_TASKS.discard(order_id)


async def handle_g2g_api(order_id: str, task_data: dict):
    """Delivery via REST API — runs steps individually to track progress."""
    import re
    auth = await auth_manager.get_auth()
    qty = int(task_data.get("delivery_qty", "1"))
    files = task_data.get("files", [])
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

    # Run each step, tracking which succeed
    completed_steps = set(skip_steps)

    # Step 1: Submit qty
    if "qty" not in completed_steps:
        try:
            await api_client.call_with_retry(
                api_client.submit_delivered_qty, api_id, qty, auth, auth.seller_id)
            completed_steps.add("qty")
            logger.info(f"[{order_id}] delivered_qty={qty} OK")
        except Exception:
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

    # Step 3: Send chat
    if "chat" not in completed_steps and message:
        try:
            await api_client._send_chat(api_id, message, auth, auth.seller_id)
            completed_steps.add("chat")
        except Exception as e:
            logger.warning(f"[{order_id}] Chat failed (non-fatal): {e}")


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

    # Recovery: retry orders that failed due to expired JWT
    async def recover_auth_failed():
        while not _shutdown_event.is_set():
            await asyncio.sleep(60)
            if not (G2G_USE_API and api_client and auth_manager):
                continue
            failed = db.get_orders_by_status("g2g", ORDER_FAILED)
            for order in failed:
                err = order.get("error_message", "")
                if not err.startswith("JWT_EXPIRED:"):
                    continue
                order_id = order["order_id"]
                if order_id in PROCESSING_TASKS:
                    continue
                # Check if auth is healthy before retrying
                try:
                    auth = await auth_manager.get_auth()
                except Exception:
                    break  # Still no JWT, stop retrying
                logger.info(f"Retrying JWT-failed order: {order_id}")

                # Restore full task data from retry_data if available
                import json
                retry_json = order.get("retry_data")
                if retry_json:
                    try:
                        task_data = json.loads(retry_json)
                        logger.info(f"Restored task data with {len(task_data.get('files', []))} files, skip={task_data.get('skip_steps', [])}")
                    except Exception:
                        task_data = None
                else:
                    task_data = None

                if not task_data:
                    task_data = {
                        "order_id": order_id,
                        "order_url": order.get("order_url", ""),
                        "delivery_qty": order.get("quantity", "1"),
                        "files": [],
                        "thread_id": order.get("discord_thread_id", ""),
                    }

                asyncio.create_task(process_task(task_data))
    asyncio.create_task(recover_auth_failed())

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
