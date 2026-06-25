"""Eldorado Worker — HTTP API server that executes Eldorado deliveries.

Receives tasks from Coordinator via POST /task.
Supports two modes:
  - API mode (ELDO_USE_API=true): Uses REST API, no browser needed
  - Selenium mode (default): Uses Chrome automation
"""

import asyncio
import os
import re
import json
import signal
import tempfile
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Optional

from aiohttp import web

from shared.config import (
    CHROME_BINARY_PATH, HEADLESS_MODE, DATABASE_PATH,
    ELDO_USE_API, AUTH_SERVICE_URL,
)
from shared.constants import ORDER_DELIVERING, ORDER_COMPLETED, ORDER_FAILED
from shared.database import Database
from shared.driver_manager import get_driver
from shared.logging_config import setup_logger
from workers.base_worker import (
    cleanup_files, implicit_wait_override, sanitize_filename, _executor,
)
from workers.talkjs_client import TalkJSClient

logger = setup_logger("worker.eldo")

WORKER_PORT = int(os.getenv("WORKER_ELDO_PORT", "8001"))

PROCESSING_TASKS: set = set()
talkjs_client: TalkJSClient = None
driver = None
db: Database = None
_shutdown_event = asyncio.Event()

# API mode components
api_client = None
auth_manager = None


async def _run_sync(func, *args, timeout=120):
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
    """POST /task — receive task from Coordinator."""
    try:
        task_data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    order_id = task_data.get("order_id", "unknown")
    action = task_data.get("action", "normal_delivery")
    files_count = len(task_data.get("files", []))
    logger.info(f"HTTP /task received: {order_id} action={action} files={files_count}")

    if order_id in PROCESSING_TASKS and action != "fast_delivery":
        return web.json_response({"status": "already_processing"})

    # Process in background
    asyncio.create_task(process_task(task_data))
    return web.json_response({"status": "accepted", "order_id": order_id})


async def handle_health(request: web.Request):
    return web.json_response({"status": "ok", "service": "eldo_worker"})


# ── Task Processing ──

COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://localhost:8030")


async def _notify_coordinator(order_id: str, thread_id: str, success: bool,
                            action: str = "normal_delivery"):
    if not thread_id:
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{COORDINATOR_URL}/complete",
                json={"order_id": order_id, "thread_id": thread_id,
                      "success": success, "action": action},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as e:
        logger.warning(f"Coordinator notify failed: {e}")


async def process_task(task_data: dict):
    global talkjs_client
    order_id = task_data["order_id"].upper()
    action = task_data.get("action", "normal_delivery")
    thread_id = task_data.get("thread_id", "")
    logger.info(f"Task received: {order_id} action={action} files={len(task_data.get('files', []))}")

    if action != "fast_delivery":
        PROCESSING_TASKS.add(order_id)

    try:
        if action == "fast_delivery":
            logger.info(f"FAST mode: {order_id} — deliver only, no proof/chat")
            if ELDO_USE_API and api_client and auth_manager:
                auth = await auth_manager.get_auth()
                await api_client.call_with_retry(api_client.deliver_order, order_id, auth)
                logger.info(f"FAST delivered via API: {order_id}")
            else:
                status = await handle_eldorado_fast(driver, task_data["order_url"], order_id)
                if status != "success":
                    raise Exception("Fast delivery failed (Selenium)")
            db.update_order_status(order_id, ORDER_COMPLETED)
            logger.info(f"Completed (fast): {order_id}")
            await _notify_coordinator(order_id, thread_id, success=True,
                                      action="fast_delivery")
        elif ELDO_USE_API and api_client and auth_manager:
            await handle_eldo_api(order_id, task_data)
            db.update_order_status(order_id, ORDER_COMPLETED)
            cleanup_files(task_data.get("files", []))
            logger.info(f"Completed: {order_id}")
            await _notify_coordinator(order_id, thread_id, success=True)
        else:
            # Selenium mode, normal delivery
            files = task_data.get("files", [])
            logger.info(f"NORMAL delivery: {order_id}, {len(files)} files")
            db.update_order_status(order_id, ORDER_DELIVERING)
            uploaded = await handle_eldorado(
                driver, task_data["order_url"],
                task_data.get("delivery_qty", "1"),
                files, order_id,
            )
            if uploaded == len(files):
                db.update_order_status(order_id, ORDER_COMPLETED)
                cleanup_files(files)
            else:
                db.update_order_status(order_id, ORDER_FAILED,
                                       error_message=f"Partial upload: {uploaded}/{len(files)}")
            await _notify_coordinator(order_id, thread_id, success=True)

    except Exception as e:
        err_msg = str(e)[:200]
        is_auth_err = ("AuthError" in type(e).__name__
                        or "401" in err_msg or "403" in err_msg
                        or "auth service error" in err_msg.lower())
        if is_auth_err:
            logger.warning(f"Auth error for {order_id} — will retry when auth refreshes")
            import json
            retry_data = json.dumps(task_data)
            db.update_order_status(order_id, ORDER_FAILED,
                                   error_message=f"AUTH_EXPIRED:{err_msg}",
                                   retry_data=retry_data)
        else:
            logger.error(f"Task error for {order_id}: {e}")
            db.update_order_status(order_id, ORDER_FAILED, error_message=err_msg)
            await _notify_coordinator(order_id, thread_id, success=False)
    finally:
        # Delete /tmp/erp_evidence_* copies downloaded this attempt (re-downloadable
        # on retry). Prevents the evidence-file disk leak.
        cleanup_files(task_data.get("_downloaded_tmp", []))
        PROCESSING_TASKS.discard(order_id)


# ── API Delivery ──

async def _download_file(file_info) -> Optional[str]:
    """Download file from ERP to temp dir. Accepts dict {url, evidence_id, api_key} or path str."""
    import tempfile
    if isinstance(file_info, str):
        return file_info  # Already a local path (from Discord coordinator)

    url = file_info.get("url", "")
    evidence_id = file_info.get("evidence_id", "")
    api_key = file_info.get("api_key", "")
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
                # Determine extension from content-type or name
                ext = Path(name).suffix or ".mp4"
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False,
                                                   prefix="erp_evidence_")
                tmp.write(await resp.read())
                tmp.close()
                logger.info(f"Downloaded evidence: {name} → {tmp.name}")
                return tmp.name
    except Exception as e:
        logger.error(f"Download error for {evidence_id}: {e}")
        return None


async def _talkjs_ensure_connected(auth) -> bool:
    """Ensure TalkJS client has fresh auth token and is connected. Returns True if ready."""
    if not talkjs_client:
        return False
    try:
        if not talkjs_client.auth_token:
            jwt = await api_client.call_with_retry(
                api_client.get_talkjs_auth, auth)
            talkjs_client.auth_token = jwt
            talkjs_client.user_id = api_client.get_talkjs_user_id(jwt)

        if not talkjs_client.is_connected:
            await talkjs_client.connect()

        return talkjs_client.is_connected
    except Exception as e:
        logger.warning(f"TalkJS connect failed: {e}")
        return False


async def _talkjs_send_with_retry(order_id: str, conv_id: str,
                                   text: str, auth, max_retries: int = 2):
    """Send TalkJS message with retry on 401. Reconnects with fresh token on auth failure."""
    for attempt in range(max_retries + 1):
        if not await _talkjs_ensure_connected(auth):
            logger.warning(f"[{order_id}] TalkJS not connected (attempt {attempt + 1})")
            continue

        msg_id = await talkjs_client.send_text_message(conv_id, text)
        if msg_id:
            return msg_id

        # Send failed — likely 401. Reconnect with fresh token.
        logger.warning(f"[{order_id}] TalkJS msg failed (attempt {attempt + 1}), refreshing auth")
        try:
            await talkjs_client.close()
        except Exception:
            pass
        talkjs_client.auth_token = None
        talkjs_client._is_connected = False
        # Fetch fresh TalkJS auth
        try:
            jwt = await api_client.call_with_retry(api_client.get_talkjs_auth, auth)
            talkjs_client.auth_token = jwt
            talkjs_client.user_id = api_client.get_talkjs_user_id(jwt)
        except Exception as e:
            logger.warning(f"[{order_id}] TalkJS auth refresh failed: {e}")
            break

    return None


async def handle_eldo_api(order_id: str, task_data: dict):
    """Delivery via API — per-step with progress tracking."""
    from shared.eldo_api import AuthError

    auth = await auth_manager.get_auth()
    message = ""
    proof_urls = []
    message_path = Path("message.txt")
    if message_path.exists():
        message = message_path.read_text(encoding="utf-8").strip() or "Done"

    skip_steps = set(task_data.get("skip_steps", []))
    completed_steps = set(skip_steps)
    files = task_data.get("files", [])
    erp_api_key = task_data.get("erp_api_key", "")

    # Track /tmp copies downloaded from ERP dicts so process_task can delete them
    # after the attempt (re-downloaded on retry). Discord PROOF_DIR paths (str)
    # are NOT tracked here — they keep cleanup-on-terminal in process_task.
    downloaded_tmp = []
    task_data["_downloaded_tmp"] = downloaded_tmp  # cleaned in process_task.finally

    # Inject API key into file dicts so _download_file can use it
    if erp_api_key:
        for fp in files:
            if isinstance(fp, dict):
                fp["api_key"] = erp_api_key

    # Step 1: Deliver order (skip if already delivered e.g. "Khách vào" was pressed)
    # Also cache conversation ID to avoid re-fetching in later steps
    conv_id = ""
    if "deliver" not in completed_steps:
        try:
            detail = await api_client.call_with_retry(
                api_client.get_order_detail, order_id, auth)
            conv_id = detail.get("talkJsConversationId", "")
            state = (detail.get("state") or {}).get("state", "")
            if state in ("Delivering", "Delivered", "Completed", "Dispute"):
                logger.info(f"[{order_id}] Already delivered (state={state}), skipping")
                completed_steps.add("deliver")
            else:
                await api_client.call_with_retry(api_client.deliver_order, order_id, auth)
                completed_steps.add("deliver")
                logger.info(f"[{order_id}] Delivered OK")
        except Exception:
            task_data["skip_steps"] = list(completed_steps)
            raise

    # Step 2: Upload proof files + send via TalkJS
    if "proofs" not in completed_steps and files and talkjs_client:
        try:
            # Try to get conv_id if step 1 was skipped (from retry_data)
            if not conv_id:
                detail = await api_client.call_with_retry(
                    api_client.get_order_detail, order_id, auth)
                conv_id = detail.get("talkJsConversationId", "")
            if conv_id:
                if not await _talkjs_ensure_connected(auth):
                    logger.warning(f"[{order_id}] TalkJS not connected for proof upload")

                uploaded = 0
                for fp in files:
                    # ERP sends files as dicts {url, name, evidence_id}
                    # Need to download first, then upload to Firebase
                    local_path = await _download_file(fp)
                    if not local_path:
                        logger.warning(f"[{order_id}] Failed to download: {fp}")
                        continue
                    if isinstance(fp, dict):
                        downloaded_tmp.append(local_path)  # /tmp copy → dọn sau

                    file_info = await talkjs_client.upload_file(local_path, conv_id)
                    if not file_info:
                        logger.warning(f"[{order_id}] Failed to upload: {fp}")
                        continue
                    proof_urls.append(file_info["url"])
                    uploaded += 1

                if uploaded == len(files):
                    completed_steps.add("proofs")
                    logger.info(f"[{order_id}] All {uploaded} proofs uploaded")
                else:
                    logger.warning(f"[{order_id}] Partial upload: {uploaded}/{len(files)}")
            else:
                logger.warning(f"[{order_id}] No TalkJS conversation ID for proof upload")
        except Exception as e:
            logger.warning(f"[{order_id}] Proof upload failed (non-fatal): {e}")

    # Step 3: Send chat message (include proof URLs if file attachment failed)
    if "chat" not in completed_steps and talkjs_client:
        try:
            if not conv_id:
                detail = await api_client.call_with_retry(
                    api_client.get_order_detail, order_id, auth)
                conv_id = detail.get("talkJsConversationId", "")
            if conv_id:
                # Build message with proof URLs as clickable links
                full_msg = message
                if proof_urls:
                    links = "\n".join(f"Proof {i+1}: {url}" for i, url in enumerate(proof_urls))
                    full_msg = f"{message}\n\n{links}"

                if full_msg:
                    msg_id = await _talkjs_send_with_retry(
                        order_id, conv_id, full_msg, auth)
                    if msg_id:
                        completed_steps.add("chat")
                        logger.info(f"[{order_id}] Chat sent")
                    else:
                        logger.warning(f"[{order_id}] Chat send failed after retry")
                else:
                    logger.warning(f"[{order_id}] Chat send failed")
            else:
                logger.info(f"[{order_id}] No TalkJS conversation ID")
        except Exception as e:
            logger.warning(f"[{order_id}] Chat failed (non-fatal): {e}")


# ── Eldorado Fast Delivery (Selenium) ──

async def handle_eldorado_fast(drv, url, order_id: str = "") -> str:
    def _sync():
        from selenium.webdriver.common.by import By
        import time

        drv.switch_to.window(drv.window_handles[0])
        drv.get(url)

        TAG_ACTION = "eld-seller-deliver-item"
        TAG_WAITING = "eld-seller-waiting-buyer-response"
        TAG_COMPLETED = "eld-seller-order-completed"
        BTN_XPATH = "//button[@data-testid='order-page-seller-order-delivered-button-xm0D']"

        for attempt in range(10):
            logger.info(f"Fast check ({attempt + 1}/10)")
            drv.switch_to.default_content()

            with implicit_wait_override(drv, 0):
                state_found = False
                for _ in range(60):
                    if len(drv.find_elements(By.TAG_NAME, TAG_ACTION)) > 0:
                        state_found = True
                        try:
                            btns = drv.find_elements(By.XPATH, BTN_XPATH)
                            if btns:
                                drv.execute_script(
                                    "arguments[0].scrollIntoView({behavior:'smooth',block:'center'});", btns[0]
                                )
                                drv.execute_script("arguments[0].click();", btns[0])
                                logger.info("Clicked Delivered")
                                time.sleep(3)
                                drv.refresh()
                                time.sleep(5)
                                break
                        except:
                            break
                    elif len(drv.find_elements(By.TAG_NAME, TAG_WAITING)) > 0:
                        return "success"
                    elif len(drv.find_elements(By.TAG_NAME, TAG_COMPLETED)) > 0:
                        return "already_done"
                    time.sleep(0.5)

            if not state_found:
                drv.refresh()
                time.sleep(3)

        return "timeout"

    return await _run_sync(_sync, timeout=300) or "error"


# ── Eldorado Normal Delivery (Selenium) ──

async def handle_eldorado(drv, url, qty, file_paths, order_id: str = ""):
    global talkjs_client
    from selenium.webdriver.common.by import By

    if not order_id:
        match = re.search(r'/order/([a-f0-9\-]{36})', url)
        order_id = match.group(1) if match else "unknown"

    sent_registry = []
    total_files = len(file_paths) if file_paths else 0

    for global_attempt in range(3):
        def _sync_navigate():
            drv.switch_to.window(drv.window_handles[0])
            if global_attempt == 0:
                drv.get(url)
            else:
                drv.refresh()
        await _run_sync(_sync_navigate)
        await asyncio.sleep(5 if global_attempt > 0 else 2)

        # Click Delivered button
        def _sync_click():
            import time
            drv.implicitly_wait(0)
            for _ in range(90):
                if (len(drv.find_elements(By.TAG_NAME, "eld-seller-deliver-item")) > 0 or
                    len(drv.find_elements(By.TAG_NAME, "eld-seller-waiting-buyer-response")) > 0):
                    break
                time.sleep(0.5)
            drv.switch_to.default_content()
            btns = drv.find_elements(By.XPATH,
                "//button[@data-testid='order-page-seller-order-delivered-button-xm0D']")
            if btns and btns[0].is_displayed():
                drv.execute_script("arguments[0].click();", btns[0])
            drv.implicitly_wait(10)
        await _run_sync(_sync_click, timeout=120)
        await asyncio.sleep(2)

        # Upload files
        if file_paths:
            for idx, file_path in enumerate(file_paths):
                file_name = os.path.basename(file_path)
                if file_name in sent_registry:
                    continue

                def _sync_upload(fp=file_path, fn=file_name):
                    import time
                    drv.switch_to.default_content()
                    iframe = None
                    for sel in ["iframe[name*='talkjs']", "iframe[src*='talkjs']", "iframe[src*='app.talkjs.com']"]:
                        frames = drv.find_elements(By.CSS_SELECTOR, sel)
                        if frames:
                            src = frames[0].get_attribute("src") or ""
                            if src and not src.startswith("about:blank"):
                                iframe = frames[0]
                                break
                    if not iframe:
                        for _ in range(5):
                            time.sleep(3)
                            for sel in ["iframe[name*='talkjs']", "iframe[src*='talkjs']"]:
                                frames = drv.find_elements(By.CSS_SELECTOR, sel)
                                if frames:
                                    src = frames[0].get_attribute("src") or ""
                                    if src and not src.startswith("about:blank"):
                                        iframe = frames[0]
                                        break
                            if iframe:
                                break
                    if not iframe:
                        drv.refresh()
                        return False

                    drv.switch_to.frame(iframe)
                    time.sleep(3)
                    inputs = (drv.find_elements(By.CSS_SELECTOR, "input[type='file']")
                              or drv.find_elements(By.CSS_SELECTOR, "input.test__fileupload-input"))
                    if not inputs:
                        return False

                    abs_path = os.path.abspath(fp).replace("\\", "/")
                    drv.execute_script(
                        "arguments[0].style.display='block'; arguments[0].style.visibility='visible';",
                        inputs[0],
                    )
                    time.sleep(0.3)
                    inputs[0].send_keys(abs_path)
                    time.sleep(3)

                    for _ in range(80):
                        btns = (drv.find_elements(By.CSS_SELECTOR, ".confirm-send.test__confirm-upload-button")
                                or drv.find_elements(By.CSS_SELECTOR, ".confirm-send")
                                or drv.find_elements(By.CSS_SELECTOR, "button[class*='confirm']"))
                        if btns and btns[0].is_displayed() and btns[0].is_enabled():
                            drv.execute_script("arguments[0].click();", btns[0])
                            return True
                        time.sleep(0.3)
                    return False

                result = await _run_sync(_sync_upload, timeout=120)
                if result:
                    sent_registry.append(file_name)
                    await asyncio.sleep(2)

        if len(sent_registry) == total_files:
            break

    # Send TalkJS message
    msg = "Done"
    msg_path = Path("message.txt")
    if msg_path.exists():
        msg = msg_path.read_text(encoding="utf-8").strip()

    try:
        drv.switch_to.default_content()
        conv_id = await _extract_conversation_id(drv, order_id)
        if conv_id and talkjs_client:
            if not talkjs_client.auth_token:
                await talkjs_client.extract_auth_from_browser()
            if not talkjs_client.is_connected:
                await talkjs_client.connect()
            if talkjs_client.is_connected:
                msg_id = await talkjs_client.send_text_message(conv_id, msg)
                if not msg_id:
                    logger.warning("TalkJS WebSocket send failed")
    except Exception as e:
        logger.warning(f"TalkJS message error: {e}")

    drv.implicitly_wait(10)
    return len(sent_registry)


async def _extract_conversation_id(drv, order_id: str = ""):
    import json, base64
    from urllib.parse import unquote
    from selenium.webdriver.common.by import By

    def _sync():
        try:
            drv.switch_to.default_content()
            iframes = drv.find_elements(By.CSS_SELECTOR, "iframe[name*='talkjs']")
            if iframes:
                src = iframes[0].get_attribute("src") or ""
                sync_match = re.search(r'syncPlease=([^&]+)', src)
                if sync_match:
                    encoded = unquote(sync_match.group(1))
                    encoded += '=' * (4 - len(encoded) % 4)
                    decoded = base64.b64decode(encoded)
                    data = json.loads(decoded)
                    return data.get('externalConversationId')
        except:
            pass
        try:
            match = re.search(r'"conversationId"\s*:\s*"([a-f0-9\-]{36})"', drv.page_source)
            if match:
                return match.group(1)
        except:
            pass
        return None

    return await _run_sync(_sync, timeout=10)


# ── Main ──

async def run_worker():
    global driver, talkjs_client, db, api_client, auth_manager

    db = Database(DATABASE_PATH)

    # API mode: use REST API instead of Selenium
    if ELDO_USE_API:
        from shared.eldo_auth import EldoAuthManager
        from shared.eldo_api import EldoradoAPIClient

        auth_manager = EldoAuthManager(auth_url=AUTH_SERVICE_URL)
        api_client = EldoradoAPIClient(auth_manager)
        talkjs_client = TalkJSClient(driver=None)
        logger.info("Running in API mode (no browser)")
    else:
        driver = get_driver(
            profile_dir="chrome_profile_eldo_worker",
            headless=HEADLESS_MODE,
            chrome_binary=CHROME_BINARY_PATH,
        )
        talkjs_client = TalkJSClient(driver)
        logger.info("Running in Selenium mode")

    app = web.Application()
    app.router.add_post("/task", handle_task)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WORKER_PORT)
    await site.start()
    logger.info(f"Eldorado Worker listening on port {WORKER_PORT} (PID: {os.getpid()})")

    # Heartbeat
    async def heartbeat():
        while not _shutdown_event.is_set():
            db.update_heartbeat("worker_eldo", os.getpid())
            await asyncio.sleep(30)
    asyncio.create_task(heartbeat())

    # Recovery: retry orders that failed due to auth
    async def recover_auth_failed():
        while not _shutdown_event.is_set():
            await asyncio.sleep(60)
            if not (ELDO_USE_API and api_client and auth_manager):
                continue
            failed = db.get_orders_by_status("eldorado", ORDER_FAILED)
            for order in failed:
                err = order.get("error_message", "")
                if not err.startswith("AUTH_EXPIRED:"):
                    continue
                order_id = order["order_id"]
                if order_id in PROCESSING_TASKS:
                    continue
                try:
                    auth = await auth_manager.get_auth()
                except Exception:
                    break
                logger.info(f"Retrying auth-failed order: {order_id}")

                # Restore full task data from retry_data
                import json
                retry_json = order.get("retry_data")
                if retry_json:
                    try:
                        task_data = json.loads(retry_json)
                        logger.info(f"Restored task data, skip={task_data.get('skip_steps', [])}")
                    except Exception:
                        task_data = None
                else:
                    task_data = None

                if not task_data:
                    task_data = {
                        "order_id": order_id,
                        "order_url": order.get("order_url", ""),
                        "action": "normal_delivery",
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
    if talkjs_client:
        await talkjs_client.close()
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
