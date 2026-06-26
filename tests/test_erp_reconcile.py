"""Unit test for reconcile_from_erp — fully faked (no server / network / curl_cffi).

Run:  python tests/test_erp_reconcile.py
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from status_sync.erp_reconcile import reconcile_from_erp  # noqa: E402


class FakeDB:
    def __init__(self, preset=None):
        self.rows = dict(preset or {})
        self.pushed = []

    def get_marketplace_status(self, platform, oid):
        return self.rows.get((platform, oid))

    def upsert_marketplace_status(self, platform, oid, state, order_item_id=None, **kw):
        self.rows[(platform, oid)] = {
            "marketplace_state": state,
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
        }

    def mark_marketplace_pushed(self, platform, oid, ok):
        self.pushed.append((oid, ok))


class FakeERP:
    def __init__(self, orders):
        self.orders = orders
        self.calls = []

    async def get_pending_orders(self, platform, limit=200):
        return self.orders

    async def push_status_update(self, payload):
        self.calls.append((payload["external_order_id"], payload["marketplace_state"]))
        return True


class FakeAPI:
    def __init__(self, statuses):
        self.statuses = statuses
        self.lookups = []

    def get_order_detail(self, item_id, auth, seller_id=""):
        self.lookups.append(item_id)
        return {"order_item_status": self.statuses.get(item_id, "delivering")}


def run(coro):
    return asyncio.run(coro)


def test_basic():
    orders = [{"external_order_id": e} for e in ("C1", "X1", "D1")]
    statuses = {"C1-1": "completed", "X1-1": "cancelled", "D1-1": "delivering"}
    db, erp, api = FakeDB(), FakeERP(orders), FakeAPI(statuses)
    comp, canc, skip = run(
        reconcile_from_erp(db, erp, api, None, "g2g", batch=10, throttle=0, backoff_h=12)
    )
    assert (comp, canc, skip) == (1, 1, 1), (comp, canc, skip)
    assert ("C1", "completed") in erp.calls
    assert ("X1", "cancelled") in erp.calls
    assert all(c[0] != "D1" for c in erp.calls), "delivering must not push"
    print("test_basic OK")


def test_backoff():
    recent = datetime.now(timezone.utc).isoformat()
    db = FakeDB({("g2g", "D1"): {"marketplace_state": "delivering", "last_synced_at": recent}})
    orders = [{"external_order_id": "D1"}, {"external_order_id": "C1"}]
    api = FakeAPI({"C1-1": "completed", "D1-1": "delivering"})
    erp = FakeERP(orders)
    comp, canc, skip = run(
        reconcile_from_erp(db, erp, api, None, "g2g", batch=10, throttle=0, backoff_h=12)
    )
    assert "D1-1" not in api.lookups, "backed-off order must not be looked up"
    assert "C1-1" in api.lookups
    assert comp == 1
    print("test_backoff OK")


def test_rate_limit_stops():
    class RL(Exception):
        status = 429
        retry_after = 30

    class RLApi(FakeAPI):
        def get_order_detail(self, item_id, auth, seller_id=""):
            self.lookups.append(item_id)
            if item_id == "B-1":
                raise RL("rl")
            return {"order_item_status": "completed"}

    orders = [{"external_order_id": x} for x in ("A", "B", "C")]
    db, erp, api = FakeDB(), FakeERP(orders), RLApi({})
    run(reconcile_from_erp(db, erp, api, None, "g2g", batch=10, throttle=0, backoff_h=12))
    assert "A-1" in api.lookups and "B-1" in api.lookups, api.lookups
    assert "C-1" not in api.lookups, "must stop after rate-limit"
    print("test_rate_limit_stops OK")


if __name__ == "__main__":
    test_basic()
    test_backoff()
    test_rate_limit_stops()
    print("ALL PASS")
