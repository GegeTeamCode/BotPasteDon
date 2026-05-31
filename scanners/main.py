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
from shared.constants import URL_DEFAULTS, ORDER_NOTIFIED

from scanners.eldorado_scanner import EldoradoScanner
from scanners.eldorado_scanner_api import EldoradoAPIScanner
from scanners.g2g_scanner import G2GScanner
from scanners.g2g_scanner_api import G2GAPIScanner
from scanners.base_scanner import shutdown_executor

logger = setup_logger("scanner.main")

# Global state for graceful shutdown
_shutdown_event = asyncio.Event()


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

    # Send to ERP in parallel (non-blocking)
    if ERP_WEBHOOK_URL:
        erp_key = ERP_API_KEY_ELDO if platform == "eldorado" else ERP_API_KEY_G2G
        if erp_key:
            asyncio.create_task(send_erp_webhook(order_data, ERP_WEBHOOK_URL, erp_key))

    return discord_ok


async def run_scanner(platform: str):
    db = Database(DATABASE_PATH)

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

        async def heartbeat():
            while not _shutdown_event.is_set():
                db.update_heartbeat(f"scanner_{platform}", os.getpid())
                await asyncio.sleep(30)

        asyncio.create_task(heartbeat())
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
