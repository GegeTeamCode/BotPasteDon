import asyncio, sys
sys.path.insert(0, "/opt/BotPasteDon")
from shared.eldo_auth import EldoAuthManager
from shared.eldo_api import EldoradoAPIClient
from scanners.eldorado_scanner_api import EldoradoAPIScanner
from shared.database import Database
from shared.config import SCANNER_CONFIG

async def main():
    mgr = EldoAuthManager()
    auth = await mgr.get_auth()
    c = EldoradoAPIClient(mgr)
    detail = c.get_order_detail("8a00381c-34e7-4561-9b20-4365b23a4761", auth)
    db = Database("data/orders.db")
    scanner = EldoradoAPIScanner(mgr, SCANNER_CONFIG, db)
    mapped = scanner._map_order_data(detail, auth)
    for k, v in mapped.items():
        print(f"{k}: {v}")
    c.close()

asyncio.run(main())
