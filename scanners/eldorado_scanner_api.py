"""Eldorado Scanner using REST API instead of Selenium.

Polls eldorado.gg/api/orders/me/seller/orders for pending orders.
No Chrome driver needed — faster, less resource usage.
"""

import asyncio
import logging
from typing import Optional, Dict, List
from functools import partial

from shared.eldo_auth import EldoAuthManager
from shared.eldo_api import EldoradoAPIClient, APIError, AuthError
from shared.database import Database
from shared.logging_config import setup_logger
from scanners.base_scanner import normalize_id, check_keywords

logger = setup_logger("scanner.eldo.api")

# Eldorado catalogs some games under a title that does NOT match the ERP Game Title,
# so the game-library lookup returns the wrong `game` and ERP's _find_game_context
# can't resolve it (order lands with NULL game_context). Map the Eldorado gameId to
# the canonical ERP Game Title here. Keyed by str(gameId).
#   225 -> library title "Flame Elementium" (the currency), ERP game is "Torchlight: Infinite".
ELDO_GAME_ID_TITLES = {
    "225": "Torchlight: Infinite",
}


class EldoradoAPIScanner:
    """Eldorado Scanner using REST API — no browser needed."""

    def __init__(self, auth_manager: EldoAuthManager, config: dict, db: Database):
        self.auth_mgr = auth_manager
        self.api = EldoradoAPIClient(auth_manager)
        self.config = config
        self.db = db

    async def scan_order_list(self) -> List[Dict]:
        logger.info("Scanning for pending orders...")
        try:
            auth = await self.auth_mgr.get_auth()
        except Exception as e:
            logger.error("Auth failed: %s", e)
            return []

        loop = asyncio.get_running_loop()
        try:
            orders = await loop.run_in_executor(
                None, self.api.get_pending_orders, auth,
            )
        except AuthError as e:
            logger.warning("Auth error: %s — skipping cycle", e)
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

            # Keyword filter on item_name + offerTitle + gameCategoryTitle
            item_name = self._get_item_name(order)
            offer = order.get("orderOfferDetails") or {}
            offer_title = offer.get("offerTitle") or ""
            category = offer.get("gameCategoryTitle") or ""
            filter_text = f"{item_name} {offer_title} {category}"
            if not check_keywords(filter_text, self.config):
                self.db.insert_order("eldorado", order_id, {"itemName": item_name})
                continue

            result.append({
                "id": order_id,
                "url": f"https://www.eldorado.gg/order/{order_id}",
                "raw": order,
            })

        if result:
            logger.info("API scan found %d new orders", len(result))
        return result

    async def extract_order_data(self, order_info: dict) -> Optional[Dict]:
        order_id = order_info["id"]
        try:
            auth = await self.auth_mgr.get_auth()
            loop = asyncio.get_running_loop()
            detail = await loop.run_in_executor(
                None, self.api.get_order_detail, order_id, auth,
            )
            # Merge buyerUsername from list data (detail API doesn't include it)
            raw = order_info.get("raw", {})
            if raw.get("buyerUsername") and not detail.get("buyerUsername"):
                detail["buyerUsername"] = raw["buyerUsername"]
            return self._map_order_data(detail, auth)
        except Exception as e:
            logger.error("Extract error for %s: %s", order_id, e)
            return None

    def _extract_order_id(self, order: dict) -> Optional[str]:
        oid = order.get("id")
        return normalize_id(str(oid)) if oid else None

    def _get_item_name(self, order: dict) -> str:
        offer = order.get("orderOfferDetails") or {}
        attrs = offer.get("offerAttributesProperties") or []
        parts = []
        for attr in attrs:
            val = (attr.get("value") or "").strip()
            if val:
                parts.append(val)
        if parts:
            return " - ".join(parts)
        # Fallback: offerTitle has the actual listing name (e.g. "S13 Flawless Horadric Amethyst")
        return offer.get("offerTitle") or offer.get("gameCategoryTitle") or ""

    def _map_order_data(self, raw: dict, auth=None) -> dict:
        offer = raw.get("orderOfferDetails") or {}
        attrs = offer.get("offerAttributesProperties") or []
        trade_env = offer.get("tradeEnvironmentProperties") or []

        # Item name: attributes > offerTitle > gameCategoryTitle
        item_parts = []
        for attr in attrs:
            val = (attr.get("value") or "").strip()
            if val:
                item_parts.append(val)
        item_name = " - ".join(item_parts) if item_parts else \
            offer.get("offerTitle") or offer.get("gameCategoryTitle") or "Unknown Item"

        # Server from trade environment (name can be "Server" or "Game mode")
        server = "N/A"
        for te in trade_env:
            val = te.get("value")
            if val:
                server = val
                break

        # Game name — prefer attributeId, then heuristic, then game library
        game = "N/A"
        game_slug = ""
        for attr in attrs:
            aid = (attr.get("attributeId") or "").lower()
            if aid:
                # Extract game slug from attributeId, e.g.:
                # "path-of-exile-2-orbs" → "path-of-exile-2"
                # "path-of-exile-currency" → "path-of-exile"
                # "diablo-4-items" → "diablo-4"
                parts = aid.split("-")
                # Try progressively shorter prefixes until we match a known game
                for end in range(len(parts), 0, -1):
                    candidate = "-".join(parts[:end])
                    if candidate in ("path-of-exile-2", "path-of-exile", "diablo-4"):
                        game_slug = candidate
                        break
                if not game_slug and len(parts) >= 2:
                    game_slug = "-".join(parts[:2])
                break
        if game_slug == "path-of-exile-2":
            game = "Path of Exile 2"
        elif game_slug == "path-of-exile":
            game = "Path of Exile"
        elif game_slug == "diablo-4":
            game = "Diablo 4"
        else:
            game_id = str(offer.get("gameId", ""))
            # gameId override wins over the library title, which mislabels some games
            # (e.g. 225 -> "Flame Elementium" instead of "Torchlight: Infinite").
            override = ELDO_GAME_ID_TITLES.get(game_id)
            if override:
                game = override
            elif auth and game_id:
                # Fallback to game library
                try:
                    game = self.api.get_game_name(game_id, auth)
                except Exception:
                    game = f"Game {game_id}"
            elif game_id:
                game = f"Game {game_id}"

        # Heuristic: game library may return wrong name (e.g. "Power Leveling" for D4 Items)
        # Detect from trade environment or offer title
        if game not in ("Path of Exile", "Path of Exile 2", "Diablo 4", *ELDO_GAME_ID_TITLES.values()):
            env_vals = " ".join(te.get("value", "").lower() for te in trade_env)
            title_lower = (item_name + " " + env_vals).lower()
            if "diablo" in title_lower or "seasonal realm" in env_vals or "softcore" in env_vals or "hardcore" in env_vals:
                game = "Diablo 4"
            elif "path of exile 2" in title_lower or "fate of the vaal" in title_lower:
                game = "Path of Exile 2"
            elif "path of exile" in title_lower:
                game = "Path of Exile"

        # Quantity + unit price — apply Eldorado `orderPricing.unitSystem`, which is
        # the gold-per-purchase-unit (e.g. "Unit1000000000" = 1B per unit, "Unit1" = 1:1).
        # ERP inventory measures Gold in MILLIONS; other currencies (POE orbs, items)
        # are 1:1. Keep qty * unit_price == total. Eldorado shows e.g. "Gold x 60B"
        # → purchaseQuantity=60 with unitSystem=Unit1000000000 → ERP qty=60000 (M),
        # unit_price scaled down so the order total is unchanged.
        _pricing = offer.get("orderPricing") or {}
        try:
            _pq = float(raw.get("purchaseQuantity", 1) or 1)
        except (TypeError, ValueError):
            _pq = 1.0
        try:
            _unit_val = int(str(_pricing.get("unitSystem", "Unit1")).replace("Unit", "")) or 1
        except ValueError:
            _unit_val = 1
        try:
            _ppu = float((_pricing.get("pricePerUnit") or {}).get("amount", 0) or 0)
        except (TypeError, ValueError):
            _ppu = 0.0
        _is_gold = (offer.get("gameCategoryTitle") or "").strip().lower() == "gold"
        if _is_gold:
            _erp_base = 1_000_000  # ERP Gold unit = 1 million
            _qty_num = _pq * _unit_val / _erp_base
            _unit_price_num = (_ppu * _erp_base / _unit_val) if _unit_val else _ppu
        else:
            _qty_num = _pq * _unit_val
            _unit_price_num = (_ppu / _unit_val) if _unit_val else _ppu
        qty = str(int(_qty_num)) if float(_qty_num).is_integer() else repr(_qty_num)
        unit_price_out = repr(_unit_price_num)

        # Character / ingame name from deliveryDetails or deliveryOptions
        # Priority: BattleNetTag > Username > CharacterName
        delivery = raw.get("deliveryDetails") or []
        delivery_map = {}
        for d in delivery:
            dtype = d.get("type", "")
            dval = d.get("value", "")
            if dtype and dval:
                delivery_map[dtype] = dval
        if not delivery_map:
            opts = raw.get("deliveryOptions") or {}
            for key in sorted(opts.keys()):
                opt = opts[key]
                oname = opt.get("name", "")
                oval = opt.get("value", "")
                if oname and oval:
                    delivery_map[oname] = oval

        # Pick character based on game
        if game == "Diablo 4":
            character = delivery_map.get("BattleNetTag") or delivery_map.get("CharacterName") or "Check Order"
        else:
            character = delivery_map.get("Username") or delivery_map.get("CharacterName") or "Check Order"

        # Customer name from buyerInfo
        buyer_info = raw.get("buyerInfo") or {}
        buyer_user = buyer_info.get("user") or {}
        customer_name = buyer_user.get("username") or raw.get("buyerUsername") or "Unknown"

        # Order ID — normalize to uppercase for consistent DB lookups
        from scanners.base_scanner import normalize_id
        order_id = normalize_id(raw.get("id", ""))

        # Pricing
        total_price_obj = raw.get("totalPrice") or {}
        pricing = offer.get("orderPricing") or {}
        total_amount = total_price_obj.get("amount")

        # Commission from sellerPayments.sellerFees
        seller_payments = raw.get("sellerPayments") or {}
        seller_fees = seller_payments.get("sellerFees") or {}
        channel_fee = seller_fees.get("amount")

        # Earning = total - commission
        earning = None
        if total_amount is not None and channel_fee is not None:
            earning = round(total_amount - channel_fee, 2)

        # Commission rate (channel_fee / total_price * 100)
        channel_fee_rate = None
        if total_amount and channel_fee:
            channel_fee_rate = round(channel_fee / total_amount * 100, 2)

        # Order date
        order_date = raw.get("createdDate", "")

        return {
            "platform": "Eldorado",
            "orderId": order_id,
            "customerName": customer_name,
            "game": game,
            "server": server,
            "itemName": item_name,
            "quantity": qty,
            "character": character,
            "url": f"https://www.eldorado.gg/order/{order_id}",
            # Pricing fields for ERP
            "sale_currency": total_price_obj.get("currency", "USD"),
            "unit_price": unit_price_out,
            "total_price": str(total_amount or ""),
            "earning": str(earning) if earning is not None else None,
            "channel_fee": str(channel_fee) if channel_fee is not None else None,
            "channel_fee_rate": str(channel_fee_rate) if channel_fee_rate is not None else None,
            "order_date": order_date,
        }
