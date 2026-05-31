import asyncio, sys, json
sys.path.insert(0, "/opt/BotPasteDon")
from shared.eldo_auth import EldoAuthManager
from shared.eldo_api import EldoradoAPIClient
async def main():
    mgr = EldoAuthManager()
    auth = await mgr.get_auth()
    c = EldoradoAPIClient(mgr)
    d = c.get_order_detail("7cd87bff-997a-4826-84c8-eb5f06f58b47", auth)
    # Print all keys with values
    for k, v in sorted(d.items()):
        print(f"{k}: {str(v)[:150]}")
    print("\n=== offer ===")
    offer = d.get("orderOfferDetails", {})
    for k, v in sorted(offer.items()):
        print(f"  {k}: {str(v)[:150]}")
    # Check conversationDetails
    conv = d.get("conversationDetails")
    if conv:
        print("\n=== conversationDetails ===")
        print(json.dumps(conv, indent=2)[:500])
asyncio.run(main())
