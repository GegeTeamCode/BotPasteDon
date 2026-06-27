"""G2G Scanner using REST API instead of Selenium.

Polls sls.g2g.com/order/list_my_order for pending orders.
No Chrome driver needed — faster, less resource usage.
Falls back to Selenium G2GScanner on API failure.
"""

import asyncio
import logging
import re
from typing import Optional, Dict, List
from functools import partial

from shared.g2g_auth import G2GAuthManager
from shared.g2g_api import G2GAPIClient, APIError, AuthError
from shared.database import Database
from shared.logging_config import setup_logger
from shared.constants import ORDER_EXTRACT_FAILED, ORDER_NEEDS_MANUAL
from scanners.base_scanner import normalize_id, check_keywords

logger = setup_logger("scanner.g2g.api")

# Max consecutive scans an order may fail to reach 'delivering' before we stop
# retrying it and flag it NEEDS_MANUAL (prevents an unstartable order looping).
MAX_START_ATTEMPTS = 5


class NotReadyError(Exception):
    """start_deliver/mark_as_delivering didn't take — order is not actually in
    'delivering' state, so its delivery_info is incomplete. Do NOT push to ERP;
    retry on a later scan instead. `status` carries the order_item_status read
    back (e.g. 'cancelled') so the recovery loop can act on terminal states."""

    def __init__(self, msg: str, status: str = ""):
        super().__init__(msg)
        self.status = status


class DetailFetchError(Exception):
    """start+mark succeeded (order IS delivering on G2G) but get_order_detail was
    unreadable. Must not be dropped — record EXTRACT_FAILED for the recovery loop."""

    def __init__(self, msg: str, api_id: str, url: str = ""):
        super().__init__(msg)
        self.api_id = api_id
        self.url = url


class G2GAPIScanner:
    """G2G Scanner using REST API — no browser needed."""

    def __init__(self, auth_manager: G2GAuthManager, config: dict, db: Database):
        self.auth_mgr = auth_manager
        self.api = G2GAPIClient(auth_manager)
        self.config = config
        self.db = db
        self._seller_id: Optional[str] = None
        # order_id -> consecutive 'not ready' attempts (in-memory, capped by MAX)
        self._start_attempts: Dict[str, int] = {}

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
        """Public entry. Runs the extract (with JWT-refresh retry) and routes the
        two recoverable failure modes:
          - NotReadyError    → order not yet 'delivering' (incomplete info): don't
                               push, retry on later scans, cap to NEEDS_MANUAL.
          - DetailFetchError → order IS delivering but detail unreadable: record
                               EXTRACT_FAILED for the recovery loop (never lost).
        """
        order_id = order_info["id"]
        try:
            data = await self._extract_with_auth_retry(order_info)
            if data is not None:
                self._start_attempts.pop(order_id, None)  # clear counter on success
            return data
        except NotReadyError as e:
            return self._handle_not_ready(order_id, order_info, e)
        except DetailFetchError as e:
            return self._handle_detail_failed(order_id, order_info, e)

    async def _extract_with_auth_retry(self, order_info: dict) -> Optional[Dict]:
        """Fetch order detail → start deliver → mark delivering → re-fetch.

        On AuthError (401): invalidates JWT cache, waits for auth service to
        capture a fresh JWT, then retries once. Does NOT retry blindly — only
        proceeds after confirming the new JWT is different from the failed one.
        NotReadyError / DetailFetchError propagate to extract_order_data.
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

        # Initial attempt (NotReady/DetailFetch bubble up to extract_order_data)
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
        except (NotReadyError, DetailFetchError):
            raise
        except Exception as e:
            logger.error("Extract error for %s: %s", order_id, e)
            return None

    def _handle_not_ready(self, order_id: str, order_info: dict,
                          err: Exception) -> None:
        """Case 1: order never reached 'delivering' → don't push incomplete info.
        Retry on later scans; after MAX_START_ATTEMPTS flag NEEDS_MANUAL so it
        stops looping and surfaces for a human."""
        n = self._start_attempts.get(order_id, 0) + 1
        self._start_attempts[order_id] = n
        if n >= MAX_START_ATTEMPTS:
            logger.error("Order %s not ready after %d attempts (%s) — NEEDS_MANUAL",
                         order_id, n, err)
            self.db.insert_order("g2g", order_id, {
                "url": order_info.get("url", ""),
                "itemName": "NEEDS_MANUAL (start_deliver never took)",
            })
            self.db.update_order_status(order_id, ORDER_NEEDS_MANUAL,
                                        error_message=str(err)[:500])
            self._start_attempts.pop(order_id, None)
        else:
            logger.warning("Order %s not ready (attempt %d/%d): %s — retry next scan",
                           order_id, n, MAX_START_ATTEMPTS, err)
        return None

    def _handle_detail_failed(self, order_id: str, order_info: dict,
                              err: "DetailFetchError") -> None:
        """Case 2: order IS delivering on G2G but detail unreadable → record
        EXTRACT_FAILED with the api_id so the recovery loop re-fetches + pushes."""
        logger.error("Order %s delivering on G2G but detail unreadable (%s) — "
                     "recording EXTRACT_FAILED for recovery", order_id, err)
        self.db.insert_order("g2g", order_id, {
            "url": getattr(err, "url", "") or order_info.get("url", ""),
            "itemName": "(extract failed — pending recovery)",
            "_api_id": getattr(err, "api_id", order_id),
        })
        self.db.update_order_status(order_id, ORDER_EXTRACT_FAILED,
                                    error_message=str(err)[:500])
        self._start_attempts.pop(order_id, None)
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

    async def resolve_order_item_id(self, entered_id: str) -> Optional[str]:
        """Find the real G2G order_item_id from a user-entered external id.

        The id a seller sees / copies has the item index suffix stripped — the
        API order_item_id is e.g. '1782...-1' but the display id is '1782...'
        (see _map_order_data). Manual paste receives the display id, so probe
        get_order_detail on both forms and return the canonical order_item_id the
        API reports. READ-ONLY (no start_deliver) so it is safe for orders that
        are already delivering. Returns None if neither form exists.
        """
        auth = await self.auth_mgr.get_auth()
        loop = asyncio.get_running_loop()
        sid = self._seller_id or getattr(auth, "seller_id", "") or ""

        e = (entered_id or "").strip()
        candidates = [e]
        m = re.match(r"^(.*)-\d+$", e)
        if m:
            candidates.append(m.group(1))   # user typed a suffix → also try the bare id
        else:
            candidates.append(f"{e}-1")     # display id → real item is usually <id>-1

        seen = set()
        for cand in candidates:
            if not cand or cand in seen:
                continue
            seen.add(cand)
            try:
                d = await loop.run_in_executor(
                    None, self.api.get_order_detail, cand, auth, sid)
            except AuthError:
                raise
            except Exception as ex:
                logger.info("resolve %s: candidate %s not found (%s)",
                            e, cand, str(ex)[:80])
                continue
            if d and (d.get("order_item_id") or d.get("order_id")):
                resolved = str(d.get("order_item_id") or d.get("order_id"))
                logger.info("resolve %s -> order_item_id=%s (status=%s)",
                            e, resolved, d.get("order_item_status"))
                return resolved
        return None

    async def _do_extract(self, api_id: str, order_id: str,
                          order_info: dict) -> Optional[Dict]:
        """Best-effort transition to delivering, then GATE on the order's actual
        order_item_status read back from get_order_detail.

        Raises:
          AuthError        on 401 (outer JWT-refresh path handles it).
          DetailFetchError if start+mark may have committed 'delivering' but the
                           detail is unreadable (don't drop the order).
          NotReadyError    if the order never moved to delivering (start_deliver
                           didn't take) — its delivery_info is incomplete, so we
                           must NOT push it to ERP; retry on a later scan.
        """
        from shared.g2g_api import AuthError

        auth = await self.auth_mgr.get_auth()
        loop = asyncio.get_running_loop()

        # Best-effort: move to delivering. We do NOT trust the PUT result — it
        # errors idempotently when the order is already delivering. The real gate
        # is the order_item_status read below, so a swallowed PUT error is fine
        # ONLY because we verify the true state afterward.
        for fn, label in ((self.api.start_deliver, "start_deliver"),
                          (self.api.mark_as_delivering, "mark_as_delivering")):
            try:
                await loop.run_in_executor(None, fn, api_id, auth, self._seller_id or "")
                logger.info("%s OK for %s", label, order_id)
            except AuthError:
                raise
            except Exception as e:
                logger.warning("%s for %s: %s", label, order_id, e)

        # Re-fetch full detail. Retry transient errors (curl timeout, 5xx).
        # If unreadable, the order may already be 'delivering' on G2G → raise
        # DetailFetchError so it is recorded (EXTRACT_FAILED), never lost.
        await asyncio.sleep(1)
        last_err: Optional[Exception] = None
        detail = None
        for attempt in range(1, 4):
            try:
                detail = await loop.run_in_executor(
                    None, self.api.get_order_detail,
                    api_id, auth, self._seller_id or ""
                )
                break
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
        if detail is None:
            raise DetailFetchError(
                f"get_order_detail failed after 3 attempts: {last_err}",
                api_id, order_info.get("url", ""))

        # GATE: only push to ERP if the order really reached delivering, so we
        # never push an order whose delivery_info is still incomplete (case 1).
        # Idempotent-safe: an already-delivering order reads 'delivering' here
        # and proceeds normally — same happy path as before.
        status = str(detail.get("order_item_status") or "").lower()
        if status not in ("delivering", "delivered", "completed"):
            raise NotReadyError(
                f"order_item_status={status!r} (not delivering) for {order_id}",
                status=status)

        return self._map_order_data(detail, order_info.get("url", ""))

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
