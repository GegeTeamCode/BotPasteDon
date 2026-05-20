"""G2G Scanner using REST API instead of Selenium.

Polls sls.g2g.com/order/list_my_order for pending orders.
No Chrome driver needed — faster, less resource usage.
Falls back to Selenium G2GScanner on API failure.
"""

import asyncio
import logging
from typing import Optional, Dict, List
from functools import partial

from shared.g2g_auth import G2GAuthManager
from shared.g2g_api import G2GAPIClient, APIError, AuthError
from shared.database import Database
from shared.logging_config import setup_logger
from scanners.base_scanner import normalize_id, check_keywords

logger = setup_logger("scanner.g2g.api")


class G2GAPIScanner:
    """G2G Scanner using REST API — no browser needed."""

    def __init__(self, auth_manager: G2GAuthManager, config: dict, db: Database):
        self.auth_mgr = auth_manager
        self.api = G2GAPIClient(auth_manager)
        self.config = config
        self.db = db
        self._seller_id: Optional[str] = None

    async def scan_order_list(self) -> List[Dict]:
        """Poll API for pending orders."""
        try:
            auth = await self.auth_mgr.get_auth()
        except Exception as e:
            logger.error("Auth failed: %s", e)
            return []

        loop = asyncio.get_running_loop()
        try:
            orders = await loop.run_in_executor(
                None, self.api.get_pending_orders, auth, self._seller_id or ""
            )
        except AuthError as e:
            logger.error("Auth error: %s — invalidating cache", e)
            await self.auth_mgr.invalidate()
            return []
        except APIError as e:
            logger.error("API error: %s", e)
            return []
        except Exception as e:
            logger.error("Unexpected: %s", e)
            return []

        result = []
        seen = set()
        for order in orders:
            order_id = self._extract_order_id(order)
            if not order_id:
                continue
            if order_id in seen or self.db.is_order_processed(order_id):
                continue
            seen.add(order_id)

            # Capture seller_id from first order
            if not self._seller_id:
                self._seller_id = order.get("seller_id", "")
                if self._seller_id:
                    logger.info("Captured seller_id: %s", self._seller_id)

            # Filter keywords
            title = order.get("title", "") or order.get("item_name", "") or order.get("offer_title", "")
            if not check_keywords(title, self.config):
                # Insert as DETECTED so it's tracked (won't re-process)
                self.db.insert_order("g2g", order_id, {"itemName": title})
                continue

            result.append({
                "id": order_id,
                "api_id": order.get("order_item_id") or order.get("order_id") or order_id,
                "url": order.get("url", ""),
                "raw": order,
            })

        if result:
            logger.info("API scan found %d new orders", len(result))
        return result

    async def extract_order_data(self, order_info: dict) -> Optional[Dict]:
        """Fetch order detail → start deliver → mark delivering → re-fetch.

        Matches HAR 4 flow:
        1. GET  /order/item/{id}          (View details)
        2. PUT  /order/item/{id}/start_deliver
        3. PUT  /order/item/{id}/mark_as_delivering
        4. GET  /order/item/{id}          (re-fetch full data)
        """
        order_id = order_info["id"]
        api_id = order_info.get("api_id") or order_id
        try:
            auth = await self.auth_mgr.get_auth()
            loop = asyncio.get_running_loop()

            # Step 1: Start deliver
            try:
                await loop.run_in_executor(
                    None, self.api.start_deliver,
                    api_id, auth, self._seller_id or ""
                )
                logger.info("Started deliver for %s", order_id)
            except Exception as e:
                logger.warning("start_deliver for %s: %s", order_id, e)

            # Step 2: Mark as delivering
            try:
                await loop.run_in_executor(
                    None, self.api.mark_as_delivering,
                    api_id, auth, self._seller_id or ""
                )
                logger.info("Marked delivering for %s", order_id)
            except Exception as e:
                logger.warning("mark_as_delivering for %s: %s", order_id, e)

            # Step 3: Re-fetch full detail after state change
            await asyncio.sleep(1)
            detail = await loop.run_in_executor(
                None, self.api.get_order_detail,
                api_id, auth, self._seller_id or ""
            )

            return self._map_order_data(detail, order_info.get("url", ""))
        except Exception as e:
            logger.error("Extract error for %s: %s", order_id, e)
            return None

    def _extract_order_id(self, order: dict) -> Optional[str]:
        for key in ("order_item_id", "order_id", "id"):
            val = order.get(key)
            if val:
                return normalize_id(str(val))
        return None

    def _map_order_data(self, raw: dict, fallback_url: str = "") -> dict:
        """Map API response → same format as old bot (order_scanner.py)."""
        order_item_id = raw.get("order_item_id") or raw.get("order_id")
        if order_item_id:
            order_url = f"https://www.g2g.com/g2g-user/sale/order/item/{order_item_id}"
        else:
            order_url = fallback_url

        # orderId: strip -1 suffix like old bot normalize_id
        order_id_display = order_item_id
        if order_id_display and order_id_display.upper().endswith("-1"):
            order_id_display = order_id_display[:-2]

        # Game: from brand_keyword (like old bot: Brand element)
        game = "N/A"
        brand_kw = raw.get("brand_keyword")
        if isinstance(brand_kw, dict):
            game = brand_kw.get("en") or brand_kw.get("id") or "N/A"
        elif isinstance(brand_kw, str):
            game = brand_kw

        # Build attribute lookup: label → value
        attrs = {}
        for attr in (raw.get("offer_attributes") or []):
            if not isinstance(attr, dict):
                continue
            label = ""
            label_obj = attr.get("label")
            if isinstance(label_obj, dict):
                label = label_obj.get("en", "")
            elif isinstance(label_obj, str):
                label = label_obj
            value = attr.get("value") or attr.get("option") or ""
            if label and value:
                attrs[label] = value

        # Server: from attributes (like old bot: label "Server"/"Realm"/"Region")
        server = attrs.get("Server") or attrs.get("Realm") or attrs.get("Region") or "N/A"

        # ItemName: follow old bot logic (Item Type → use as label to find detail)
        item_type = attrs.get("Item Type")
        if item_type:
            # Fuzzy match: label might be plural (e.g. "Gears" for "Gear")
            detail = ""
            for label, val in attrs.items():
                if label == "Item Type" or label == "Server":
                    continue
                if item_type.lower() in label.lower() or label.lower() in item_type.lower():
                    detail = val
                    break
            if detail:
                item_name = f"{item_type} - {detail}"
            else:
                item_name = item_type
        else:
            item_name = attrs.get("Item") or attrs.get("Product") or attrs.get("Currency") or ""

        if not item_name:
            svc_kw = raw.get("service_keyword")
            if isinstance(svc_kw, dict):
                svc_name = svc_kw.get("en", "")
            else:
                svc_name = str(svc_kw or "")
            if "coin" in svc_name.lower() or "gold" in svc_name.lower():
                item_name = "Gold"
            elif raw.get("offer_title"):
                item_name = raw.get("offer_title", "Unknown Item")
            else:
                item_name = "Unknown Item"

        # Character: from checkout_info.delivery_method_details.delivery_info
        character = ""
        checkout = raw.get("checkout_info") or {}
        delivery_details = (checkout.get("delivery_method_details") or {})
        for info in (delivery_details.get("delivery_info") or []):
            val = (info.get("value") or "").strip()
            if val:
                character = val
                break
        if not character:
            character = raw.get("buyer_username") or "Check Order"

        # Title mapping: override itemName when offer_title matches a pattern
        offer_title = raw.get("offer_title") or ""
        if offer_title:
            for rule in self.config.get("G2G_TITLE_MAP", []):
                pattern = rule.get("title_pattern", "")
                if pattern and pattern.lower() in offer_title.lower():
                    item_name = rule["display_name"]
                    break

        # Quantity
        qty = raw.get("purchased_qty") or raw.get("qty") or raw.get("quantity") or "1"

        return {
            "platform": "G2G",
            "orderId": order_id_display,
            "customerName": raw.get("buyer_username") or raw.get("buyer_name") or "Unknown",
            "game": game,
            "server": server,
            "itemName": item_name,
            "quantity": str(qty),
            "character": character,
            "url": order_url,
        }
