import asyncio, sys, json
sys.path.insert(0, "/opt/BotPasteDon")
from shared.eldo_auth import EldoAuthManager
from shared.eldo_api import EldoradoAPIClient

async def main():
    mgr = EldoAuthManager()
    auth = await mgr.get_auth()
    c = EldoradoAPIClient(mgr)
    orders = c.get_pending_orders(auth)
    if orders:
        print(f"Found {len(orders)} pending orders")
        for o in orders:
            oid = o.get("id", "?")
            offer = o.get("orderOfferDetails", {})
            cat = offer.get("gameCategoryTitle", "?")
            attrs = offer.get("offerAttributesProperties", [])
            item_parts = [a.get("value", "") for a in attrs if a.get("value")]
            item_name = " - ".join(item_parts) if item_parts else cat
            state = o.get("state", {})
            if isinstance(state, dict):
                state = state.get("state", "?")
            print(f"  {oid} | {item_name} | {state}")
    else:
        print("No pending orders from API")
    c.close()

asyncio.run(main())
