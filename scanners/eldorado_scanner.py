"""Eldorado-specific scanning logic."""

import asyncio
import re
import time
from typing import Optional, Dict, List

from selenium.webdriver.common.by import By

from scanners.base_scanner import BaseScanner, normalize_id, check_keywords
from shared.database import Database
from shared.logging_config import setup_logger


class EldoradoScanner(BaseScanner):
    def __init__(self, driver, config: dict, db: Database):
        super().__init__(driver, "eldorado", config, db)

    async def scan_order_list(self) -> List[Dict]:
        def _sync_scan():
            orders = []
            seen_ids = set()
            try:
                rows = self.driver.find_elements(By.CSS_SELECTOR, 'a[href*="/order/"]')
                for row in rows:
                    try:
                        row_text = row.text.lower()
                        if "pendingdelivery" not in row_text and "pending" not in row_text:
                            continue
                        link = row.get_attribute("href")
                        if not link:
                            continue
                        id_match = re.search(r'order/([a-zA-Z0-9-]+)', link)
                        if not id_match:
                            continue
                        order_id = normalize_id(id_match.group(1))
                        if not order_id or self._is_processed(order_id):
                            continue
                        if order_id in seen_ids:
                            continue
                        seen_ids.add(order_id)
                        if not check_keywords(row.text, self.config):
                            self._mark_processed(order_id)
                            continue
                        orders.append({"id": order_id, "url": link})
                    except:
                        continue
            except Exception as e:
                self.logger.error(f"Scan error: {e}")
            return orders

        return await self._run_sync(_sync_scan, timeout=30) or []

    async def extract_order_data(self, url: str) -> Optional[Dict]:
        def _sync_extract():
            data = {
                "platform": "Eldorado",
                "orderId": None, "customerName": None, "game": None,
                "server": None, "itemName": None, "quantity": None,
                "character": None, "url": self.driver.current_url,
            }

            max_attempts = 40
            for attempt in range(max_attempts):
                try:
                    data = self._extract_from_page()
                    has_game = data.get("game") and data["game"] not in ["N/A", ""]
                    has_item = data.get("itemName") and data["itemName"] not in ["Unknown Item", ""]
                    if has_game and has_item:
                        return data
                    time.sleep(0.5)
                except Exception as e:
                    self.logger.warning(f"Extract attempt {attempt + 1}: {e}")
                    time.sleep(0.5)
            return data if data.get("orderId") else None

        return_url = await self._get_current_url()
        await self._driver_get(url)
        await asyncio.sleep(2)

        result = await self._run_sync(_sync_extract, timeout=60)

        await self._driver_get(return_url)
        await asyncio.sleep(1)
        return result

    def _extract_from_page(self) -> Dict:
        data = {
            "platform": "Eldorado",
            "orderId": None, "customerName": None, "game": None,
            "server": None, "itemName": None, "quantity": None,
            "character": None, "url": self.driver.current_url,
        }
        try:
            # Order ID from URL
            url_match = re.search(r'order/([a-zA-Z0-9-]+)', self.driver.current_url)
            if url_match:
                data["orderId"] = normalize_id(url_match.group(1))
            if not data["orderId"]:
                oid_el = self.driver.find_elements(By.CSS_SELECTOR, '.order-info .order-id')
                if oid_el:
                    m = re.search(r'Order ID:\s*([a-zA-Z0-9-]+)', oid_el[0].text)
                    if m:
                        data["orderId"] = normalize_id(m.group(1))

            # Item Name
            for sel in ['h1', '.offer-title', '[class*="offerTitle"]']:
                el = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if el and el[0].text.strip():
                    data["itemName"] = el[0].text.strip()
                    break
            if not data["itemName"]:
                data["itemName"] = "Unknown Item"

            # Label-based extraction
            data["game"] = self._get_value_by_label("Game") or "N/A"
            data["server"] = self._get_value_by_label("Server") or self._get_value_by_label("Realm") or "N/A"
            data["quantity"] = self._get_value_by_label("Quantity") or "1"
            data["customerName"] = self._get_value_by_label("Buyer") or self._get_value_by_label("Customer") or "Unknown"

            # Fallback game from image alt or breadcrumb
            if not data["game"] or data["game"] == "N/A":
                img_el = self.driver.find_elements(By.CSS_SELECTOR, '.order-details-card .game-image img')
                if img_el:
                    alt = img_el[0].get_attribute("alt")
                    if alt:
                        data["game"] = alt
            if not data["game"] or data["game"] == "N/A":
                bc = self.driver.find_elements(By.CSS_SELECTOR, 'eld-breadcrumbs')
                if bc and ">" in bc[0].text:
                    parts = bc[0].text.split(">")
                    if len(parts) >= 2:
                        data["game"] = parts[1].strip()

            # Character
            char_name = (self._get_value_by_label("Username")
                         or self._get_value_by_label("Character name")
                         or self._get_value_by_label("Account Name"))
            btag = (self._get_value_by_label("Battle.net Tag")
                    or self._get_value_by_label("Discord Tag"))
            if char_name and btag:
                data["character"] = f"{char_name} ({btag})"
            else:
                data["character"] = char_name or btag or "Check Order"

        except Exception as e:
            self.logger.error(f"Extract error: {e}")
        return data

    def _get_value_by_label(self, label_text: str) -> Optional[str]:
        try:
            # Escape single quotes for XPath safety
            safe_label = label_text.replace("'", "\\'")
            xpath = f"//span[contains(@class, 'text-secondary') and contains(text(), '{safe_label}')]/following-sibling::span[contains(@class, 'text-primary')]"
            els = self.driver.find_elements(By.XPATH, xpath)
            if els and els[0].text.strip():
                return els[0].text.strip()

            xpath_backup = f"//span[contains(@class, 'text-secondary') and contains(text(), '{safe_label}')]/../*[last()]"
            els_backup = self.driver.find_elements(By.XPATH, xpath_backup)
            if els_backup and els_backup[0].text.strip():
                return els_backup[0].text.strip()

            rows = self.driver.find_elements(By.CSS_SELECTOR, '.order-details-card .flex.items-center.justify-between')
            for row in rows:
                label_el = row.find_elements(By.CSS_SELECTOR, '.text-secondary')
                if label_el and label_text.lower() in label_el[0].text.strip().lower():
                    value_el = row.find_elements(By.CSS_SELECTOR, '.text-primary, eld-trade-env-item')
                    if value_el and value_el[0].text.strip():
                        return value_el[0].text.strip()
        except:
            pass
        return None
