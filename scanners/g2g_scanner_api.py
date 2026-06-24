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
        logger.info("Scanning for pending orders...")
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
            logger.warning("Auth error: %s — skipping cycle, waiting for fresh JWT", e)
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

            # Filter keywords — Gold orders detected by unit_name only
            # service_keyword="Game coins" for ALL D4 orders, not just Gold
            title = order.get("title", "") or order.get("item_name", "") or order.get("offer_title", "")
            unit_name = (order.get("unit_name") or "").lower()
            is_gold = "gold" in unit_name
            if not is_gold and not check_keywords(title, self.config):
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

        On AuthError (401): invalidates JWT cache, waits for auth service to
        capture a fresh JWT, then retries once. Does NOT retry blindly — only
        proceeds after confirming the new JWT is different from the failed one.
        """
        from shared.g2g_api import AuthError

        order_id = order_info["id"]
        api_id = order_info.get("api_id") or order_id

        old_jwt_token = None
        try:
            auth = await self.auth_mgr.get_auth()
            old_jwt_token = auth.jwt_token
        except Exception:
            pass

        # Initial attempt
        try:
            return await self._do_extract(api_id, order_id, order_info)
        except AuthError as e:
            logger.warning(
                "Extract 401 for %s: %s — invalidating JWT, waiting for fresh one",
                order_id, e,
            )

        # Invalidate and wait for auth service to capture a new JWT
        await self.auth_mgr.invalidate()

        # Poll auth service until we get a JWT that is actually new
        fresh_jwt = await self._wait_for_fresh_jwt(old_jwt_token, timeout=120)
        if not fresh_jwt:
            logger.error(
                "Extract failed for %s: no fresh JWT after 120s", order_id)
            return None

        logger.info("Got fresh JWT for %s, retrying extract", order_id)
        try:
            return await self._do_extract(api_id, order_id, order_info)
        except AuthError as e:
            logger.error(
                "Extract failed for %s even with fresh JWT: %s", order_id, e)
            return None
        except Exception as e:
            logger.error("Extract error for %s: %s", order_id, e)
            return None

    async def _wait_for_fresh_jwt(self, old_jwt: Optional[str],
                                  timeout: int = 60) -> Optional[str]:
        """Poll auth manager until a new JWT (different from old_jwt) arrives.
        Returns the new JWT token or None on timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                auth = await self.auth_mgr.get_auth()
                new_jwt = auth.jwt_token
                if new_jwt and new_jwt != old_jwt:
                    return new_jwt
            except Exception:
                pass
            await asyncio.sleep(5)
        return None

    async def _do_extract(self, api_id: str, order_id: str,
                          order_info: dict) -> Optional[Dict]:
        """Execute the 4-step extract flow. Raises AuthError on 401."""
        from shared.g2g_api import AuthError

        auth = await self.auth_mgr.get_auth()
        loop = asyncio.get_running_loop()

        # Step 1: Start deliver
        try:
            await loop.run_in_executor(
                None, self.api.start_deliver,
                api_id, auth, self._seller_id or ""
            )
            logger.info("Started deliver for %s", order_id)
        except AuthError:
            raise
        except Exception as e:
            logger.warning("start_deliver for %s: %s", order_id, e)

        # Step 2: Mark as delivering
        try:
            await loop.run_in_executor(
                None, self.api.mark_as_delivering,
                api_id, auth, self._seller_id or ""
            )
            logger.info("Marked delivering for %s", order_id)
        except AuthError:
            raise
        except Exception as e:
            logger.warning("mark_as_delivering for %s: %s", order_id, e)

        # Step 3: Re-fetch full detail after state change.
        # Retry on transient errors (curl timeout, 5xx). Steps 1+2 already
        # committed state on G2G — if we give up here, the order is orphaned:
        # locked in `delivering` on marketplace, never inserted to local DB.
        # AuthError still raises so the outer JWT-refresh path runs.
        await asyncio.sleep(1)
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                detail = await loop.run_in_executor(
                    None, self.api.get_order_detail,
                    api_id, auth, self._seller_id or ""
                )
                return self._map_order_data(detail, order_info.get("url", ""))
            except AuthError:
                raise
            except Exception as e:
                last_err = e
                if attempt < 3:
                    wait = 2 * attempt
                    logger.warning(
                        "get_order_detail for %s attempt %d failed (%s) — retry in %ds",
                        order_id, attempt, e, wait,
                    )
                    await asyncio.sleep(wait)

        logger.error(
            "get_order_detail for %s gave up after 3 attempts: %s — "
            "order is in 'delivering' on G2G but NOT in local DB; "
            "manual recovery needed",
            order_id, last_err,
        )
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

        # Detect Gold orders via unit_name only (service_keyword="Game coins" for ALL D4 orders)
        unit_name_raw = (raw.get("unit_name") or "").lower()
        is_gold = "gold" in unit_name_raw

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
        elif is_gold:
            item_name = "Gold"
        else:
            item_name = attrs.get("Item") or attrs.get("Product") or attrs.get("Currency") or ""

        if not item_name:
            if is_gold:
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

        # Quantity: format with Vietnamese thousand separator (period)
        raw_qty = raw.get("purchased_qty") or raw.get("qty") or raw.get("quantity") or 1
        if isinstance(raw_qty, (int, float)):
            qty = str(int(raw_qty))
        else:
            import re
            qty = re.sub(r'[×x]', '', str(raw_qty), flags=re.IGNORECASE)
            qty = re.sub(r'\b(Mil|Gold|Unit|Units)\b', '', qty, flags=re.IGNORECASE)
            qty = re.sub(r'\s+', ' ', qty).strip()

        # Pricing fields for ERP — use actual transaction prices (not rounded listing prices)
        from decimal import Decimal

        earning = raw.get("earning")
        commission_fee = raw.get("commission_fee_amount")
        commission_rate = raw.get("commission_rate")

        # VAT (EU buyers): G2G withholds commission VAT (e.g. UK 20%) on top of the
        # commission fee. It is exposed per tax line in multi_commission_info[]
        # (short_title "VAT", tax_with_qty = the absolute amount); tax_exist flags it.
        # `earning` is already net of VAT, so the gross the buyer actually paid =
        # earning + commission_fee + VAT (== offer_amount). We send VAT explicitly so
        # ERP records vat_native AND derives gross correctly; without it the order's
        # gross is understated by the VAT and vat_native stays 0 (earning happens to
        # stay right only because total_price absorbs the shortfall — see ERP
        # botpastedon.py: earning = gross - sell_fee - vat).
        vat = Decimal("0")
        if raw.get("tax_exist"):
            for tax in (raw.get("multi_commission_info") or []):
                if isinstance(tax, dict) and tax.get("tax_with_qty") not in (None, ""):
                    try:
                        vat += Decimal(str(tax["tax_with_qty"]))
                    except Exception:
                        pass

        # total_price = gross actually paid = earning + commission_fee + VAT
        # (Decimal to avoid float errors). Falls back to offer_amount if components
        # are missing. For non-EU orders VAT is 0, so this is unchanged.
        total_price = ""
        if earning and commission_fee:
            try:
                total_price = str(Decimal(str(earning)) + Decimal(str(commission_fee)) + vat)
            except Exception:
                total_price = str(raw.get("offer_amount", ""))

        # unit_price: compute from total_price / qty for maximum precision
        # G2G API's unit_price_usd is often rounded; actual price = total / qty
        unit_price = ""
        if total_price:
            try:
                up = Decimal(total_price) / Decimal(str(qty))
                unit_price = format(up, "f").rstrip("0").rstrip(".")
            except Exception:
                unit_price = str(raw.get("unit_price_usd", ""))
        if not unit_price:
            unit_price = str(raw.get("unit_price_usd", ""))

        # Order date — G2G uses epoch ms
        created_at = raw.get("created_at")
        if created_at:
            from datetime import datetime, timezone
            order_date = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc).isoformat()
        else:
            order_date = ""

        return {
            "platform": "G2G",
            "orderId": order_id_display,
            "customerName": raw.get("buyer_username") or raw.get("buyer_name") or "Unknown",
            "game": game,
            "server": server,
            "itemName": item_name,
            "quantity": qty,
            "character": character,
            "url": order_url,
            # Pricing fields for ERP
            "sale_currency": raw.get("offer_currency", "USD"),
            "unit_price": unit_price,
            "total_price": total_price,
            "earning": str(earning) if earning else None,
            "channel_fee": str(commission_fee) if commission_fee else None,
            "channel_fee_rate": str(commission_rate) if commission_rate else None,
            # VAT (absolute, EU commission VAT). None when no tax → ERP defaults to 0.
            "vat": (format(vat, "f").rstrip("0").rstrip(".") if vat else None),
            "order_date": order_date,
        }
