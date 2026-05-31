import sys
sys.path.insert(0, "/opt/BotPasteDon")
from shared.config import SCANNER_CONFIG
from scanners.base_scanner import check_keywords

config = SCANNER_CONFIG

# Simulate what scanner does - get item name the same way
from shared.eldo_auth import EldoAuthManager
from shared.eldo_api import EldoradoAPIClient
import asyncio

async def main():
    mgr = EldoAuthManager()
    auth = await mgr.get_auth()
    c = EldoradoAPIClient(mgr)
    orders = c.get_pending_orders(auth)

    for order in orders:
        oid = order.get("id", "?")
        offer = order.get("orderOfferDetails", {})
        attrs = offer.get("offerAttributesProperties", [])
        parts = []
        for attr in attrs:
            val = (attr.get("value") or "").strip()
            if val:
                parts.append(val)
        if parts:
            item_name = " - ".join(parts)
        else:
            item_name = offer.get("gameCategoryTitle") or ""

        kw = check_keywords(item_name, config)
        print(f"Order {oid}:")
        print(f"  item_name: {item_name!r}")
        print(f"  check_keywords: {kw}")
        print(f"  config whitelist: {config.get('whitelist', '')!r}")
    c.close()

asyncio.run(main())
