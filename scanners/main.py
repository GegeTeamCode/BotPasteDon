"""Scanner entry point. Usage: python -m scanners.main --platform eldorado|g2g"""

import argparse
import asyncio
import os
import random
import signal
import sys

import json

from aiohttp import web

from shared.config import (
    SCANNER_CONFIG, CHROME_BINARY_PATH, HEADLESS_MODE, DATABASE_PATH,
    G2G_USE_API, AUTH_SERVICE_URL, ELDO_USE_API,
    erp_target_for_game, erp_key_for_target,
    MANUAL_PASTE_SECRET, MANUAL_PASTE_PORT_G2G, MANUAL_PASTE_PORT_ELDO,
)
from shared.database import Database
from shared.driver_manager import get_driver
from shared.discord_utils import format_order_message, match_webhook, send_discord_webhook, send_erp_webhook
from shared.logging_config import setup_logger
from shared.constants import URL_DEFAULTS, ORDER_NOTIFIED, ORDER_FAILED, ORDER_EXTRACT_FAILED

from scanners.eldorado_scanner import EldoradoScanner
from scanners.eldorado_scanner_api import EldoradoAPIScanner
from scanners.g2g_scanner import G2GScanner
from scanners.g2g_scanner_api import G2GAPIScanner, NotReadyError, DetailFetchError
from scanners.base_scanner import normalize_id, shutdown_executor

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

    # Send to ERP — route by game (currency games → .102), await + track sync.
    erp_target = erp_target_for_game(order_data.get("game", ""))
    webhook_url = erp_target["webhook_url"]
    erp_key = erp_key_for_target(erp_target, platform)
    if webhook_url and erp_key:
        order_id = order_data.get("orderId", "")
        # Claim the order in-flight so erp_retry_loop can't post it concurrently.
        claimed = bool(_scanner_db and order_id and _scanner_db.claim_erp_order(order_id))
        erp_ok = await send_erp_webhook(order_data, webhook_url, erp_key)
        if _scanner_db and order_id:
            if erp_ok:
                _scanner_db.mark_erp_synced(order_id)
            elif claimed:
                _scanner_db.release_erp_order(order_id)

    return discord_ok


# ── Manual paste (ERP-triggered, on-demand) ─────────────────────────────────
#
# Same extract+push path as the scanner, but for ONE explicit order_id and
# WITHOUT the keyword filter (check_keywords is never called here). Lets the
# owner pull a specific gear/custom order into ERP that the allow-all blacklist
# would otherwise drop ("Any Gears", "Any Items - Aspects").

async def handle_manual_paste(scanner, platform: str, db, order_id: str) -> dict:
    """Fetch one order by external id, then push it to ERP like the scanner.

    Returns a dict: {"status": "ok"|"error", "order_id": ..., "error"?: msg}.
    For G2G this calls start_deliver+mark_as_delivering (order moves to
    'delivering' on G2G → trader must hand-deliver) exactly like a scan.
    """
    order_id = normalize_id(str(order_id)) or ""
    if not order_id:
        return {"status": "error", "error": "order_id rỗng/không hợp lệ"}

    # 1. Fetch + map via the same scanner code paths.
    try:
        if platform == "g2g":
            # The display id (what the seller copies) has the item suffix
            # stripped; the API needs the real order_item_id (often <id>-1).
            # Resolve it first (read-only) so BOTH a brand-new order (then
            # start_deliver) and one already delivering (read straight) work.
            resolved = await scanner.resolve_order_item_id(order_id)
            if not resolved:
                return {"status": "error", "order_id": order_id,
                        "error": "Không tìm thấy đơn G2G với ID này (đã thử cả dạng có và không có hậu tố -1). Kiểm tra lại External Order ID."}
            order_info = {
                "id": resolved,
                "api_id": resolved,
                "url": f"https://www.g2g.com/g2g-user/sale/order/item/{resolved}",
            }
            # _extract_with_auth_retry surfaces NotReady/DetailFetch so we can
            # give a precise reason instead of the scanner's silent None.
            order_data = await scanner._extract_with_auth_retry(
                order_info, prefer_offer_title=True
            )
        else:  # eldorado — get_order_detail takes the order_id directly
            order_data = await scanner.extract_order_data({"id": order_id, "raw": {}})
    except NotReadyError as e:
        st = getattr(e, "status", "") or "chưa delivering"
        return {"status": "error", "order_id": order_id,
                "error": f"Đơn G2G chưa ở trạng thái giao được ({st}) — không lấy được thông tin giao. Thử lại khi buyer đã thanh toán."}
    except DetailFetchError:
        return {"status": "error", "order_id": order_id,
                "error": "G2G trả thông tin đơn không đọc được — thử lại sau ít phút."}
    except Exception as e:
        logger.error("manual paste %s/%s fetch error: %s", platform, order_id, e)
        return {"status": "error", "order_id": order_id,
                "error": f"Lỗi fetch {platform}: {e}"}

    if not order_data:
        return {"status": "error", "order_id": order_id,
                "error": "Không lấy được dữ liệu đơn (ID sai, đơn không tồn tại, hoặc chưa đọc được)."}

    # 2. Track in DB with the FULL raw_data (insert may be a no-op if the order
    #    was already recorded DETECTED by a prior filtered scan — force-refresh
    #    raw_data so erp_retry_loop has real data, mirroring recovery_loop).
    db.insert_order(platform, order_id, order_data)
    db.update_order_status(
        order_id, ORDER_NOTIFIED,
        item_name=order_data.get("itemName", ""),
        game=order_data.get("game", ""),
        server=order_data.get("server", ""),
        quantity=order_data.get("quantity", ""),
        customer_name=order_data.get("customerName", ""),
        order_url=order_data.get("url", ""),
        raw_data=json.dumps(order_data, ensure_ascii=False),
    )

    # 3. Push to ERP (authoritative). send_erp_webhook returns True on ok OR
    #    duplicate; the ERP side dedupes by external_order_id. On failure we
    #    leave erp_synced=0 so erp_retry_loop keeps retrying.
    erp_target = erp_target_for_game(order_data.get("game", ""))
    webhook_url = erp_target["webhook_url"]
    erp_key = erp_key_for_target(erp_target, platform)
    if not (webhook_url and erp_key):
        return {"status": "error", "order_id": order_id,
                "error": "ERP webhook chưa cấu hình trên bot"}
    claimed = bool(db.claim_erp_order(order_id))
    try:
        erp_ok = await send_erp_webhook(order_data, webhook_url, erp_key)
    except Exception as e:
        erp_ok = False
        logger.error("manual paste %s ERP push error: %s", order_id, e)
    if erp_ok:
        db.mark_erp_synced(order_id)
        logger.info("manual paste OK: %s/%s", platform, order_id)
        return {"status": "ok", "order_id": order_id,
                "item_name": order_data.get("itemName", "")}
    if claimed:
        db.release_erp_order(order_id)
    return {"status": "error", "order_id": order_id,
            "error": "ERP từ chối hoặc chưa tạo được đơn — sẽ tự retry, xem log bot."}


async def start_manual_paste_server(scanner, platform: str, db):
    """Expose POST /manual-paste on this scanner process so ERP can trigger an
    on-demand paste. Bound 0.0.0.0; protected by the X-Manual-Secret header."""
    port = MANUAL_PASTE_PORT_G2G if platform == "g2g" else MANUAL_PASTE_PORT_ELDO

    async def handle(request: web.Request):
        if MANUAL_PASTE_SECRET and request.headers.get("X-Manual-Secret") != MANUAL_PASTE_SECRET:
            return web.json_response({"status": "error", "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"status": "error", "error": "bad json body"}, status=400)
        order_id = str(body.get("order_id") or "").strip()
        if not order_id:
            return web.json_response({"status": "error", "error": "missing order_id"}, status=400)
        logger.info("manual paste request: %s/%s", platform, order_id)
        result = await handle_manual_paste(scanner, platform, db, order_id)
        status = 200 if result.get("status") == "ok" else 422
        return web.json_response(result, status=status)

    async def health(request: web.Request):
        return web.json_response({"ok": True, "platform": platform})

    app = web.Application()
    app.router.add_post("/manual-paste", handle)
    app.router.add_get("/manual-paste/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Manual-paste endpoint listening on 0.0.0.0:%d (%s)", port, platform)
    # Keep the runner alive until shutdown, then clean up the socket.
    await _shutdown_event.wait()
    await runner.cleanup()


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
                    erp_target = erp_target_for_game(order_data.get("game", ""))
                    webhook_url = erp_target["webhook_url"]
                    erp_key = erp_key_for_target(erp_target, order["platform"])
                    if not (webhook_url and erp_key):
                        db.increment_erp_retry(order_id)
                        db.release_erp_order(order_id)
                        continue
                    try:
                        erp_ok = await send_erp_webhook(order_data, webhook_url, erp_key)
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
            """Recover g2g orders stuck EXTRACT_FAILED. Re-runs the FULL extract
            (_do_extract re-attempts start_deliver + mark_as_delivering, then gates
            on the real order_item_status) so it un-sticks even orders that never
            reached delivering (e.g. a 429/timeout double-failure), not only ones
            already delivering. On success the row is flipped to NOTIFIED with full
            data so the existing erp_retry_loop pushes it to ERP (idempotent claim).
            Cancelled/refunded orders are marked FAILED so they stop retrying."""
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
                        # Re-run the full extract: re-attempts start_deliver +
                        # mark_as_delivering (un-sticks orders that never reached
                        # delivering), then gates on the real order_item_status.
                        try:
                            order_data = await scanner._do_extract(
                                api_id, oid, {"url": row.get("order_url", "")})
                        except NotReadyError as e:
                            st = getattr(e, "status", "")
                            if st in ("cancelled", "canceled", "refunded"):
                                db.update_order_status(oid, ORDER_FAILED,
                                                       error_message=f"recovery: order {st}")
                                logger.info("recovery: %s is %s -> FAILED", oid, st)
                            else:
                                logger.info("recovery: %s not ready (%r), retry next cycle", oid, st)
                            continue
                        except DetailFetchError:
                            logger.info("recovery: %s detail still unreadable, retry next cycle", oid)
                            continue
                        except Exception as e:
                            logger.warning("recovery: extract failed for %s: %s", oid, e)
                            continue
                        # Got full data → flip to NOTIFIED (erp_synced still 0) so the
                        # erp_retry_loop pushes it to ERP idempotently.
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
        asyncio.create_task(start_manual_paste_server(scanner, platform, db))
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
        asyncio.create_task(start_manual_paste_server(scanner, platform, db))
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
