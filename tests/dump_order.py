import asyncio, sys, json
sys.path.insert(0, "/opt/BotPasteDon")
from shared.eldo_auth import EldoAuthManager
from shared.eldo_api import EldoradoAPIClient

ORDER_ID = sys.argv[1] if len(sys.argv) > 1 else "8a00381c-34e7-4561-9b20-4365b23a4761"

async def main():
    mgr = EldoAuthManager()
    auth = await mgr.get_auth()
    c = EldoradoAPIClient(mgr)
    d = c.get_order_detail(ORDER_ID, auth)

    # Print all keys
    for k, v in sorted(d.items()):
        print(f"{k}: {str(v)[:200]}")

    print("\n=== offer ===")
    offer = d.get("orderOfferDetails", {})
    for k, v in sorted(offer.items()):
        print(f"  {k}: {str(v)[:300]}")

    print("\n=== attrs ===")
    attrs = offer.get("offerAttributesProperties", [])
    for a in attrs:
        print(f"  {json.dumps(a, ensure_ascii=False)}")

    print("\n=== trade_env ===")
    trade_env = offer.get("tradeEnvironmentProperties", [])
    for t in trade_env:
        print(f"  {json.dumps(t, ensure_ascii=False)}")

    print("\n=== delivery_details ===")
    dd = d.get("deliveryDetails", [])
    for d2 in dd:
        print(f"  {json.dumps(d2, ensure_ascii=False)}")

    print("\n=== delivery_options ===")
    do = d.get("deliveryOptions", {})
    for k2, v2 in sorted(do.items()):
        print(f"  {k2}: {json.dumps(v2, ensure_ascii=False)[:300]}")

    print("\n=== buyer_info ===")
    bi = d.get("buyerInfo", {})
    print(f"  {json.dumps(bi, ensure_ascii=False)[:500]}")

    print("\n=== state ===")
    state = d.get("state", {})
    print(f"  {json.dumps(state, ensure_ascii=False)}")

    c.close()

asyncio.run(main())
