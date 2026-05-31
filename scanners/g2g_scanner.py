"""G2G-specific scanning logic."""

import re
import time
from typing import Optional, Dict, List

from selenium.webdriver.common.by import By

from scanners.base_scanner import BaseScanner, normalize_id, check_keywords
from shared.database import Database
from shared.logging_config import setup_logger


class G2GScanner(BaseScanner):
    def __init__(self, driver, config: dict, db: Database):
        super().__init__(driver, "g2g", config, db)

    async def scan_order_list(self) -> List[Dict]:
        def _sync_scan():
            orders = []
            seen_ids = set()
            try:
                rows = self.driver.find_elements(By.CSS_SELECTOR, "a.g-card-no-deco")
                for row in rows:
                    try:
                        link = row.get_attribute("href")
                        if not link:
                            continue
                        id_match = re.search(r'order/item/([A-Z0-9-]+)', link)
                        if not id_match:
                            continue
                        order_id = normalize_id(id_match.group(1))
                        if not order_id or self._is_processed(order_id):
                            continue
                        if order_id in seen_ids:
                            continue
                        seen_ids.add(order_id)

                        title_el = row.find_elements(By.CSS_SELECTOR, 'span[data-attr="order-item-offer-title"]')
                        title = title_el[0].text if title_el else ""
                        qty_el = row.find_elements(By.CSS_SELECTOR, '[data-attr="order-item-purchased-qty"]')
                        qty_text = qty_el[0].text if qty_el else ""

                        full_text = f"{title} {qty_text}"
                        if not check_keywords(full_text, self.config):
                            self._mark_processed(order_id)
                            continue

                        orders.append({"id": order_id, "url": link, "title": title, "quantity": qty_text})
                    except:
                        continue
            except Exception as e:
                self.logger.error(f"Scan error: {e}")
            return orders

        return await self._run_sync(_sync_scan, timeout=30) or []

    async def extract_order_data(self, url: str) -> Optional[Dict]:
        def _sync_extract():
            data = {
                "platform": "G2G",
                "orderId": None, "customerName": None, "game": None,
                "server": None, "itemName": None, "quantity": None,
                "character": None, "url": self.driver.current_url,
            }

            max_attempts = 40
            for attempt in range(max_attempts):
                try:
                    # Click "View details" if brand not visible
                    brand_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr="order-item-brand"]')
                    if not brand_el or not brand_el[0].text.strip():
                        detail_btns = self.driver.find_elements(By.XPATH, "//span[contains(text(), 'View details')]")
                        if detail_btns:
                            try:
                                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", detail_btns[0])
                                detail_btns[0].click()
                                time.sleep(1)
                            except:
                                pass

                    # Click "Start deliver" if delivery info not visible
                    char_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr^="order-item-delivery-info"]')
                    if not char_el or not char_el[0].text.strip():
                        start_btns = self.driver.find_elements(By.XPATH, "//button[contains(., 'Start deliver')]")
                        if start_btns:
                            try:
                                start_btns[0].click()
                                time.sleep(1)
                            except:
                                pass

                    data = self._extract_from_page()

                    has_game = data.get("game") and data["game"] not in ["N/A", ""]
                    has_server = data.get("server") and data["server"] not in ["N/A", ""]
                    has_char = data.get("character") and data["character"] not in ["N/A", "", data.get("customerName", "")]

                    if has_game and has_server and has_char:
                        return data
                    time.sleep(0.5)
                except Exception as e:
                    self.logger.warning(f"Extract attempt {attempt + 1}: {e}")
                    time.sleep(0.5)

            return data if data.get("orderId") else None

        import asyncio
        return_url = await self._get_current_url()
        await self._driver_get(url)
        await asyncio.sleep(2)

        result = await self._run_sync(_sync_extract, timeout=60)

        await self._driver_get(return_url)
        await asyncio.sleep(1)
        return result

    def _extract_from_page(self) -> Dict:
        data = {
            "platform": "G2G",
            "orderId": None, "customerName": None, "game": None,
            "server": None, "itemName": None, "quantity": None,
            "character": None, "url": self.driver.current_url,
        }
        try:
            # Order ID
            oid_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr="order-item-order-id"]')
            if oid_el:
                data["orderId"] = normalize_id(oid_el[0].text)
            if not data["orderId"]:
                url_match = re.search(r'order/item/([A-Z0-9-]+)', self.driver.current_url)
                if url_match:
                    data["orderId"] = normalize_id(url_match.group(1))

            # Buyer
            buyer_el = self.driver.find_elements(By.CSS_SELECTOR, 'a[data-attr="order-item-buyer-username"]')
            if buyer_el:
                data["customerName"] = buyer_el[0].text.strip()

            # Brand/Game
            brand_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr="order-item-brand"]')
            if brand_el:
                data["game"] = brand_el[0].text.strip()
            if not data["game"]:
                data["game"] = self._get_value_by_label("Brand") or self._get_value_by_label("Game")
            if not data["game"]:
                bc = self.driver.find_elements(By.CSS_SELECTOR, '.custom-breadcrumb')
                if bc and ">" in bc[0].text:
                    parts = bc[0].text.split(">")
                    if len(parts) >= 2:
                        data["game"] = parts[1].strip()

            # Server
            data["server"] = (self._get_value_by_label("Server")
                              or self._get_value_by_label("Realm")
                              or self._get_value_by_label("Region"))
            if not data["server"]:
                title_el = self.driver.find_elements(By.CSS_SELECTOR, '[data-attr="order-item-offer-title"]')
                if title_el and "-" in title_el[0].text:
                    data["server"] = title_el[0].text.strip()

            # Quantity
            qty_el = self.driver.find_elements(By.CSS_SELECTOR, 'div[data-attr="order-item-purchased-qty"]')
            if not qty_el:
                qty_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr="order-item-purchased-qty"]')
            raw_qty = ""
            if qty_el:
                raw_qty = qty_el[0].text.strip()
            else:
                raw_qty = self._get_value_by_label("Purchase quantity") or self._get_value_by_label("Quantity") or ""
            if raw_qty:
                cleaned = re.sub(r'[×x]', '', raw_qty, flags=re.IGNORECASE)
                cleaned = re.sub(r'\b(Mil|Gold|Unit|Units)\b', '', cleaned, flags=re.IGNORECASE)
                data["quantity"] = re.sub(r'\s+', ' ', cleaned).strip()

            # Item Name
            specific_item = None
            item_type = self._get_value_by_label("Item Type")
            if item_type:
                detail = self._get_value_by_label(item_type)
                specific_item = f"{item_type} - {detail}" if detail else item_type
            if not specific_item:
                specific_item = (self._get_value_by_label("Item")
                                 or self._get_value_by_label("Product")
                                 or self._get_value_by_label("Currency"))
            if not specific_item:
                svc_el = self.driver.find_elements(By.CSS_SELECTOR, 'div[data-attr="order-item-service-type"]')
                if svc_el and "coin" in svc_el[0].text.lower():
                    specific_item = "Gold"
            if not specific_item:
                title_link = (self.driver.find_elements(By.CSS_SELECTOR, 'a[data-attr="order-item-offer-title"]')
                              or self.driver.find_elements(By.CSS_SELECTOR, '[data-attr="order-item-offer-title"]'))
                if title_link:
                    t = title_link[0].text.strip()
                    specific_item = "Gold" if "gold" in t.lower() else t
                else:
                    specific_item = "Unknown Item"
            data["itemName"] = specific_item

            # Character
            char_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr^="order-item-delivery-info"]')
            if char_el and char_el[0].text.strip():
                data["character"] = char_el[0].text.strip()
            if not data["character"]:
                for lbl in ["Character Name", "BattleTag", "Riot ID", "IGN", "Account", "User ID"]:
                    val = self._get_value_by_label(lbl)
                    if val:
                        data["character"] = val
                        break
            if not data["character"] and data.get("customerName"):
                data["character"] = data["customerName"]

        except Exception as e:
            self.logger.error(f"Extract error: {e}")
        return data

    def _get_value_by_label(self, label: str) -> Optional[str]:
        try:
            safe_label = label.replace("'", "\\'")
            xpath = f"//div[contains(@class, 'text-font-2nd') and contains(text(), '{safe_label}')]"
            label_el = self.driver.find_elements(By.XPATH, xpath)
            if label_el:
                row = label_el[0].find_element(By.XPATH, './ancestor::div[contains(@class, "row")]')
                if row:
                    value_el = row.find_elements(By.CSS_SELECTOR, '.text-right span, .text-right')
                    if value_el:
                        return value_el[0].text.strip()
        except:
            pass
        return None
