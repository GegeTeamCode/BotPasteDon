import asyncio, sys
sys.path.insert(0, "/opt/BotPasteDon")
from shared.eldo_auth import EldoAuthManager
from shared.eldo_api import EldoradoAPIClient

async def main():
    mgr = EldoAuthManager()
    auth = await mgr.get_auth()
    c = EldoradoAPIClient(mgr)
    lib = c._fetch_game_library(auth)
    # POE related
    for gid, name in sorted(lib.items()):
        if "poe" in name.lower() or "exile" in name.lower() or "path" in name.lower() or "point" in name.lower():
            print(f"{gid}: {name}")
    # Order gameId
    d = c.get_order_detail("7cd87bff-997a-4826-84c8-eb5f06f58b47", auth)
    gid = d.get("orderOfferDetails", {}).get("gameId")
    print(f"\nOrder gameId: {gid} -> {lib.get(str(gid), 'NOT FOUND')}")

asyncio.run(main())
