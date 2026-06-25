"""Scanner entry point. Usage: python -m scanners.main --platform eldorado|g2g"""

import argparse
import asyncio
import os
import random
import signal
import sys

from shared.config import (
    SCANNER_CONFIG, CHROME_BINARY_PATH, HEADLESS_MODE, DATABASE_PATH,
    G2G_USE_API, AUTH_SERVICE_URL, ELDO_USE_API,
    ERP_WEBHOOK_URL, ERP_API_KEY_ELDO, ERP_API_KEY_G2G,
)
from shared.database import Database
from shared.driver_manager import get_driver
from shared.discord_utils import format_order_message, match_webhook, send_discord_webhook, send_erp_webhook
from shared.logging_config import setup_logger
from shared.constants import URL_DEFAULTS, ORDER_NOTIFIED, ORDER_FAILED, ORDER_EXTRACT_FAILED

from scanners.eldorado_scanner import EldoradoScanner
from scanners.eldorado_scanner_api import EldoradoAPIScanner
from scanners.g2g_scanner import G2GScanner
from scanners.g2g_scanner_api import G2GAPIScanner
from scanners.base_scanner import shutdown_executor

logger = setup_logger("scanner.main")

# Global state for graceful shutdown
_shutdown_event = asyncio.Event()

# Set in run_scanner() so send_order_webhook can mark/claim ERP sync state.
_scanner_db = None


def handle_signal(sig, frame):
    logger.info(f"Received signal {sig}, shutting down...")
    _shutdown_event.set()


async def send_order_webhook(order_data: dict, platform: str) -> bool:
    webhook_config = SCANNER_CONFIG.get("webhooks", {})
    game_name = (order_data.get("game") or "").lower().strip()
    item_name = (order_data.get("itemName") or "").lower()

    target_url, matched_game = match_webhook(game_name, item_name, webhook_config)

    if not target_url:
        logger.error(f"No webhook found for game: {game_name}")
        return False

    logger.info(f"Sending webhook to: {matched_game}")
    fields_config = SCANNER_CONFIG.get("fields", {})
    message = format_order_message(order_data, fields_config.get("showLabels", False))
    discord_ok = await send_discord_webhook(target_url, message, order_data)

    # Send to ERP — await and track sync status
    if ERP_WEBHOOK_URL:
        erp_key = ERP_API_KEY_ELDO if platform == "eldorado" else ERP_API_KEY_G2G
        if erp_key:
            order_id = order_data.get("orderId", "")
            # Claim the order in-flight so erp_retry_loop can't post it concurrently.
            claimed = bool(_scanner_db and order_id and _scanner_db.claim_erp_order(order_id))
            erp_ok = await send_erp_webhook(order_data, ERP_WEBHOOK_URL, erp_key)
            if _scanner_db and order_id:
                if erp_ok:
                    _scanner_db.mark_erp_synced(order_id)
                elif claimed:
                    _scanner_db.release_erp_order(order_id)

    return discord_ok



async def erp_retry_loop(platform: str):
    """Background loop: retry this platform's unsynced orders to ERP every 60s.

    Scoped to ``platform`` and gated by an atomic claim so two scanner processes
    never post the same order at once — that race was creating duplicate ERP
    Sell Orders (one marketplace order -> two SOs).
    """
    db = Database(DATABASE_PATH)
    while not _shutdown_event.is_set():
        try:
            unsynced = db.get_unsynced_orders(max_retries=50, platform=platform)
            if unsynced:
                logger.warning(f"ERP retry: {len(unsynced)} unsynced orders")
                for order in unsynced:
                    if _shutdown_event.is_set():
                        break
                    order_id = order["order_id"]
                    # Win the claim before posting; skip if another loop has it.
                    if not db.claim_erp_order(order_id):
                        continue
                    raw = order.get("raw_data")
                    if not raw:
                        db.increment_erp_retry(order_id)
                        db.release_erp_order(order_id)
                        continue
                    import json as _json
                    order_data = _json.loads(raw)
                    erp_key = ERP_API_KEY_ELDO if order["platform"] == "eldorado" else ERP_API_KEY_G2G
                    try:
                        erp_ok = await send_erp_webhook(order_data, ERP_WEBHOOK_URL, erp_key)
                    except Exception:
                        erp_ok = False
                    if erp_ok:
                        db.mark_erp_synced(order_id)
                        logger.info("ERP retry OK: " + order_id)
                    else:
                        db.increment_erp_retry(order_id)
                        db.release_erp_order(order_id)
        except Exception as e:
            logger.error(f"ERP retry loop error: {e}")
        await asyncio.sleep(60)


async def run_scanner(platform: str):
    global _scanner_db
    db = Database(DATABASE_PATH)
    _scanner_db = db

    if platform == "g2g" and G2G_USE_API:
        # API mode — no browser needed
        from shared.g2g_auth import G2GAuthManager

        auth_manager = G2GAuthManager(auth_url=AUTH_SERVICE_URL)
        scanner = G2GAPIScanner(auth_manager, SCANNER_CONFIG, db)

        async def on_order(order_data: dict):
            return await send_order_webhook(order_data, platform)

        # API scanner has its own scan loop
        async def api_scan_loop():
            while not _shutdown_event.is_set():
                db.cleanup_old_orders()
                try:
                    orders = await scanner.scan_order_list()
                    for order in orders:
                        order_data = await scanner.extract_order_data(order)
                        if not order_data:
                            continue

                        # Insert into DB (dedup — returns False if exists)
                        if not db.insert_order(platform, order["id"], order_data):
                            continue

                        success = await on_order(order_data)
                        if success:
                            db.update_order_status(order["id"], ORDER_NOTIFIED)
                except Exception as e:
                    logger.error("API scan error: %s", e)

                await asyncio.sleep(
                    random.randint(
                        SCANNER_CONFIG.get("scan_interval_min", 10),
                        SCANNER_CONFIG.get("scan_interval_max", 15),
                    )
                )

        async def recovery_loop():
            """Recover g2g orders stuck EXTRACT_FAILED (delivering on G2G but the
            detail was unreadable at scan time). Re-fetch detail; once confirmed
            delivering, rewrite the row with full data + flip to NOTIFIED so the
            existing erp_retry_loop pushes it to ERP (idempotent claim). Cancelled
            orders are marked FAILED so they stop retrying."""
            import json as _json
            while not _shutdown_event.is_set():
                try:
                    rows = db.get_orders_by_status("g2g", ORDER_EXTRACT_FAILED)
                    if rows:
                        logger.info("recovery: %d EXTRACT_FAILED order(s)", len(rows))
                    for row in rows:
                        if _shutdown_event.is_set():
                            break
                        oid = row["order_id"]
                        api_id = oid
                        raw = row.get("raw_data")
                        if raw:
                            try:
                                api_id = _json.loads(raw).get("_api_id") or oid
                            except Exception:
                                pass
                        try:
                            auth = await scanner.auth_mgr.get_auth()
                            rloop = asyncio.get_running_loop()
                            detail = await rloop.run_in_executor(
                                None, scanner.api.get_order_detail,
                                api_id, auth, scanner._seller_id or "")
                        except Exception as e:
                            logger.warning("recovery: detail fetch failed for %s: %s", oid, e)
                            continue
                        st = str(detail.get("order_item_status") or "").lower()
                        if st in ("cancelled", "canceled", "refunded"):
                            db.update_order_status(oid, ORDER_FAILED,
                                                   error_message=f"recovery: order {st}")
                            logger.info("recovery: %s is %s -> FAILED", oid, st)
                            continue
                        if st not in ("delivering", "delivered", "completed"):
                            logger.info("recovery: %s still %r, retry next cycle", oid, st)
                            continue
                        order_data = scanner._map_order_data(detail, row.get("order_url", ""))
                        # Rewrite row with full data + flip to NOTIFIED so the
                        # erp_retry_loop (erp_synced still 0) pushes it idempotently.
                        db.update_order_status(
                            oid, ORDER_NOTIFIED,
                            order_url=order_data.get("url", ""),
                            game=order_data.get("game", ""),
                            server=order_data.get("server", ""),
                            item_name=order_data.get("itemName", ""),
                            quantity=order_data.get("quantity", ""),
                            character=order_data.get("character", ""),
                            customer_name=order_data.get("customerName", ""),
                            raw_data=_json.dumps(order_data, ensure_ascii=False),
                        )
                        logger.info("recovery: %s ready -> handed to erp_retry_loop", oid)
                except Exception as e:
                    logger.error("recovery_loop error: %s", e)
                await asyncio.sleep(120)

        async def heartbeat():
            while not _shutdown_event.is_set():
                db.update_heartbeat(f"scanner_{platform}", os.getpid())
                await asyncio.sleep(30)

        asyncio.create_task(heartbeat())
        asyncio.create_task(erp_retry_loop(platform))
        asyncio.create_task(recovery_loop())
        scan_task = asyncio.create_task(api_scan_loop())

        logger.info(f"G2G API Scanner running (PID: {os.getpid()})")
        await _shutdown_event.wait()

        scan_task.cancel()
        logger.info("Scanner shut down cleanly")
        return

    # Eldorado API mode
    if platform == "eldorado" and ELDO_USE_API:
        from shared.eldo_auth import EldoAuthManager

        auth_manager = EldoAuthManager(auth_url=AUTH_SERVICE_URL)
        scanner = EldoradoAPIScanner(auth_manager, SCANNER_CONFIG, db)

        async def on_order(order_data: dict):
            return await send_order_webhook(order_data, platform)

        async def api_scan_loop():
            while not _shutdown_event.is_set():
                db.cleanup_old_orders()
                try:
                    orders = await scanner.scan_order_list()
                    for order in orders:
                        order_data = await scanner.extract_order_data(order)
                        if not order_data:
                            continue
                        if not db.insert_order(platform, order["id"], order_data):
                            continue
                        success = await on_order(order_data)
                        if success:
                            db.update_order_status(order["id"], ORDER_NOTIFIED)
                except Exception as e:
                    logger.error("API scan error: %s", e)

                await asyncio.sleep(
                    random.randint(
                        SCANNER_CONFIG.get("scan_interval_min", 10),
                        SCANNER_CONFIG.get("scan_interval_max", 15),
                    )
                )

        async def heartbeat():
            while not _shutdown_event.is_set():
                db.update_heartbeat(f"scanner_{platform}", os.getpid())
                await asyncio.sleep(30)

        asyncio.create_task(heartbeat())
        asyncio.create_task(erp_retry_loop(platform))
        scan_task = asyncio.create_task(api_scan_loop())

        logger.info(f"Eldorado API Scanner running (PID: {os.getpid()})")
        await _shutdown_event.wait()

        scan_task.cancel()
        logger.info("Scanner shut down cleanly")
        return

    # Selenium mode (default)
    profile_name = f"chrome_profile_{platform}_scanner"

    logger.info(f"Creating Chrome driver for {platform} scanner...")
    driver = get_driver(
        profile_dir=profile_name,
        headless=HEADLESS_MODE,
        chrome_binary=CHROME_BINARY_PATH,
    )

    target_url = URL_DEFAULTS.get(platform)
    if target_url:
        driver.get(target_url)
        logger.info(f"Navigated to {target_url}")

    scanner_cls = EldoradoScanner if platform == "eldorado" else G2GScanner
    scanner = scanner_cls(driver, SCANNER_CONFIG, db)

    async def on_order(order_data: dict):
        order_id = order_data.get("orderId", "")
        # Insert into DB (dedup)
        if not db.insert_order(platform, order_id, order_data):
            return False
        success = await send_order_webhook(order_data, platform)
        if success:
            db.update_order_status(order_id, ORDER_NOTIFIED)
        return success

    scanner.set_callbacks(scan_callback=on_order)

    # Heartbeat task
    async def heartbeat():
        while not _shutdown_event.is_set():
            db.update_heartbeat(f"scanner_{platform}", os.getpid())
            await asyncio.sleep(30)

    asyncio.create_task(heartbeat())
    asyncio.create_task(erp_retry_loop(platform))

    # Run scanner until shutdown
    scan_task = asyncio.create_task(scanner.start())

    logger.info(f"Scanner {platform} running (PID: {os.getpid()})")
    await _shutdown_event.wait()

    scanner.stop()
    await scan_task
    shutdown_executor()
    driver.quit()
    logger.info("Scanner shut down cleanly")


def main():
    parser = argparse.ArgumentParser(description="BotPasteDon Scanner")
    parser.add_argument("--platform", choices=["eldorado", "g2g"], required=True)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        asyncio.run(run_scanner(args.platform))
    except KeyboardInterrupt:
        _shutdown_event.set()


if __name__ == "__main__":
    main()
