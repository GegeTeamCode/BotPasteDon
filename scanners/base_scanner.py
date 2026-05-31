"""Base scanner class with scan loop, caching, async Selenium helpers."""

import asyncio
import json
import re
import time
import random
import threading
from datetime import datetime
from typing import Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from selenium.webdriver.common.by import By

from shared.constants import (
    URL_DEFAULTS, CACHE_MAX_AGE_HOURS, CACHE_DIR,
    ORDER_DETECTED, ORDER_NOTIFIED,
)
from shared.database import Database
from shared.logging_config import setup_logger

# Thread pool for Selenium (avoids blocking asyncio event loop)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="selenium_")


def shutdown_executor():
    _executor.shutdown(wait=True)


def normalize_id(id_str: str) -> Optional[str]:
    if not id_str:
        return None
    clean_id = str(id_str).replace("#", "").strip().upper()
    if clean_id.endswith("-1"):
        clean_id = clean_id[:-2]
    return clean_id


def check_keywords(text: str, config: dict) -> bool:
    if not config:
        return True
    lower_text = (text or "").lower()
    if config.get("blacklist"):
        blacklist = [k.strip().lower() for k in config["blacklist"].split(",") if k.strip()]
        if any(k in lower_text for k in blacklist):
            return False
    if config.get("whitelist"):
        whitelist = [k.strip().lower() for k in config["whitelist"].split(",") if k.strip()]
        if whitelist and not any(k in lower_text for k in whitelist):
            return False
    return True


class BaseScanner:
    def __init__(self, driver, platform: str, config: dict, db: Database):
        self.driver = driver
        self.platform = platform.lower()
        self.platform_display = platform.upper()
        self.config = config
        self.db = db
        self.is_running = False
        self.scan_callback = None
        self.processing_orders: set = set()

        # In-memory cache with thread lock (fast lookup + SQLite as truth)
        self._cache_lock = threading.Lock()
        self.processed_orders: Dict[str, float] = {}

        # Load existing processed orders from DB
        self._load_cache_from_db()

    def _load_cache_from_db(self):
        existing = self.db.get_orders_by_status(self.platform, ORDER_NOTIFIED)
        now = time.time()
        with self._cache_lock:
            for order in existing:
                created = order.get("created_at", "")
                self.processed_orders[order["order_id"]] = now
        self.logger.info(f"Loaded {len(self.processed_orders)} cached orders from DB")

    @property
    def logger(self):
        return setup_logger(f"scanner.{self.platform}")

    # ── Async Selenium helpers ──

    async def _run_sync(self, func, *args, timeout: float = 30, **kwargs):
        try:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(_executor, partial(func, *args, **kwargs)),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self.logger.warning(f"Timeout after {timeout}s")
            return None
        except Exception as e:
            self.logger.warning(f"_run_sync error: {e}")
            return None

    async def _driver_get(self, url: str, timeout: float = 30):
        await self._run_sync(self.driver.get, url, timeout=timeout)

    async def _driver_refresh(self, timeout: float = 30):
        await self._run_sync(self.driver.refresh, timeout=timeout)

    async def _get_current_url(self, timeout: float = 5) -> str:
        return await self._run_sync(lambda: self.driver.current_url, timeout=timeout) or ""

    async def _get_page_source(self, timeout: float = 10) -> str:
        return await self._run_sync(lambda: self.driver.page_source, timeout=timeout) or ""

    async def _find_elements(self, by: By, value: str, timeout: float = 10):
        return await self._run_sync(
            lambda: self.driver.find_elements(by, value), timeout=timeout
        ) or []

    # ── Cache management ──

    def _is_processed(self, order_id: str) -> bool:
        with self._cache_lock:
            if order_id in self.processed_orders:
                return True
        return self.db.is_order_processed(order_id)

    def _mark_processed(self, order_id: str):
        with self._cache_lock:
            self.processed_orders[order_id] = time.time()

    def _cleanup_old_orders(self):
        now = time.time()
        max_age = CACHE_MAX_AGE_HOURS * 3600
        with self._cache_lock:
            old_count = len(self.processed_orders)
            self.processed_orders = {
                k: v for k, v in self.processed_orders.items() if now - v < max_age
            }
            new_count = len(self.processed_orders)
        if old_count > new_count:
            self.logger.info(f"Cleaned {old_count - new_count} old orders (>{CACHE_MAX_AGE_HOURS}h)")
        self.db.cleanup_old_orders()

    def set_callbacks(self, scan_callback=None):
        self.scan_callback = scan_callback

    # ── Main scan loop ──

    async def start(self):
        self.is_running = True
        self.logger.info("Scanner started")

        # Startup recovery: re-process orders stuck in DETECTED
        await self._recover_pending_orders()

        while self.is_running:
            try:
                await self.scan_loop()
            except Exception as e:
                self.logger.error(f"Scan loop error: {e}")
                await asyncio.sleep(5)

    async def _recover_pending_orders(self):
        pending = self.db.get_orders_by_status(self.platform, ORDER_DETECTED)
        if pending:
            self.logger.info(f"Recovering {len(pending)} pending orders")
            for order in pending:
                raw = order.get("raw_data", "{}")
                try:
                    order_data = json.loads(raw) if isinstance(raw, str) else raw
                    if self.scan_callback:
                        await self.scan_callback(order_data)
                except Exception as e:
                    self.logger.warning(f"Recovery failed for {order['order_id']}: {e}")

    async def scan_loop(self):
        if not self.is_running:
            return

        self._cleanup_old_orders()

        # Check page and refresh (Phase 4 fix: single refresh)
        if not await self._ensure_correct_page():
            self.logger.error("Cannot navigate to correct page!")
            await asyncio.sleep(5)
            return

        orders = await self.scan_order_list()

        if orders:
            new_orders = [o for o in orders if not self._is_processed(o["id"])]
            if new_orders:
                self.logger.info(f"Found {len(new_orders)} new orders")
                for order in new_orders:
                    await self.process_order(order)
            else:
                self.logger.info(f"{len(orders)} orders already processed")

        if self.is_running:
            interval_min = self.config.get("scan_interval_min", 15)
            interval_max = self.config.get("scan_interval_max", 25)
            wait_time = random.randint(interval_min, interval_max)
            await asyncio.sleep(wait_time)

    def stop(self):
        self.is_running = False
        self.logger.info("Scanner stopped")

    # ── Page navigation (Phase 4 fix: single refresh) ──

    async def _ensure_correct_page(self) -> bool:
        target_url = URL_DEFAULTS.get(self.platform)
        if not target_url:
            return False

        try:
            current_url = await self._get_current_url()
            on_correct = await self._is_on_correct_page()

            if on_correct:
                self.logger.info("Refreshing page...")
                await self._driver_refresh()
                await asyncio.sleep(3)
                return True
            else:
                if await self._is_error_page():
                    self.logger.warning("Error page detected, navigating...")
                else:
                    self.logger.info("Navigating to order list page...")
                await self._driver_get(target_url)
                await asyncio.sleep(3)
                return True
        except Exception as e:
            self.logger.error(f"Navigation error: {e}")
            try:
                await self._driver_get(target_url)
                await asyncio.sleep(3)
                return True
            except:
                return False

    async def _is_on_correct_page(self) -> bool:
        try:
            current_url = (await self._get_current_url()).lower()
            if self.platform == "g2g":
                return "status=preparing" in current_url
            elif self.platform == "eldorado":
                return "orderstate=pendingdelivery" in current_url
            return False
        except:
            return False

    async def _is_error_page(self) -> bool:
        try:
            page_source = (await self._get_page_source()).lower()
            current_url = (await self._get_current_url()).lower()
            error_indicators = [
                "something went wrong", "error page", "page not found",
                "server error", "/error/", "this site can't be reached",
                "connection refused", "err_connection", "err_internet",
            ]
            for indicator in error_indicators:
                if indicator in page_source or indicator in current_url:
                    self.logger.warning(f"Error detected: '{indicator}'")
                    return True
            return False
        except:
            return False

    # ── Order processing ──

    async def process_order(self, order: Dict):
        order_id = order["id"]
        order_url = order["url"]

        if self._is_processed(order_id):
            return

        self.processing_orders.add(order_id)
        try:
            order_data = await self.extract_order_data(order_url)

            if order_data:
                # G2G title mapping
                if self.platform == "g2g":
                    title = order.get("title", "")
                    if title:
                        for mapping in self.config.get("G2G_TITLE_MAP", []):
                            if mapping["title_pattern"].lower() in title.lower():
                                order_data["itemName"] = mapping["display_name"]
                                break

                # Insert to DB as DETECTED
                self.db.insert_order(self.platform, order_id, order_data)

                # Send webhook with retry
                success = False
                if self.scan_callback:
                    for attempt in range(3):
                        result = await self.scan_callback(order_data)
                        if result:
                            success = True
                            break
                        self.logger.warning(f"Webhook retry {attempt + 1}/3 for {order_id}")
                        await asyncio.sleep(2 ** attempt)

                if success:
                    self.db.update_order_status(order_id, ORDER_NOTIFIED, webhook_sent_at="CURRENT_TIMESTAMP")
                    self._mark_processed(order_id)
                    self.logger.info(f"Order notified: {order_id}")
                else:
                    self.logger.warning(f"Webhook failed for {order_id}, will retry next scan")
        except Exception as e:
            self.logger.error(f"Error processing {order_id}: {e}")
        finally:
            self.processing_orders.discard(order_id)

    # ── Abstract methods (implemented by platform subclasses) ──

    async def scan_order_list(self) -> List[Dict]:
        raise NotImplementedError

    async def extract_order_data(self, url: str) -> Optional[Dict]:
        raise NotImplementedError

    def get_processed_orders(self) -> Dict[str, float]:
        with self._cache_lock:
            return dict(self.processed_orders)
