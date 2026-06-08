"""G2G platform status sync — polls list_my_order + list_my_cases + count-my-orders."""

import asyncio
import json
from typing import Optional

from shared.g2g_api import G2GAPIClient
from shared.g2g_auth import G2GAuthManager
from shared.database import Database
from shared.logging_config import setup_logger

from status_sync.erp_client import ERPClient

logger = setup_logger("status_sync.g2g")


# States we actually push to ERP (from Delivered onwards).
# `delivering` is intentionally NOT here — traders handle that.
TRACKED_STATES = ("completed", "cancelled")

# We don't have a list endpoint for `disputed`; we synthesize that from cases.

# Counts endpoint returns these keys; we use them as tripwires.
COUNT_KEYS_TO_FETCH_LIST = {
    # When delivering count goes DOWN, orders likely moved into completed/cancelled.
    # When issues count goes UP, new dispute opened (cases endpoint catches that).
    "delivering": ("completed", "cancelled"),  # poll these list endpoints when delivering changes
    "issues": tuple(),  # cases-only signal; no list change to chase
}


class G2GSync:
    """Run one sync cycle on G2G."""

    def __init__(self, db: Database, erp: ERPClient,
                 auth_mgr: Optional[G2GAuthManager] = None):
        self.db = db
        self.erp = erp
        self.auth_mgr = auth_mgr or G2GAuthManager()
        self.api = G2GAPIClient(self.auth_mgr)
        self.platform = "g2g"

    async def run_once(self) -> None:
        try:
            auth = await self.auth_mgr.get_auth()
        except Exception as e:
            logger.warning("Auth fetch failed: %s — skip cycle", e)
            return

        loop = asyncio.get_running_loop()

        # 1. Counts tripwire (cheap)
        try:
            counts = await loop.run_in_executor(None, self.api.count_my_orders, auth)
        except Exception as e:
            logger.warning("count_my_orders failed: %s", e)
            return

        old_counts = self.db.get_marketplace_state_counts(self.platform)
        is_first_run = not old_counts
        # On first run, fetch ALL tracked states for silent backfill.
        # On subsequent runs, only fetch when something looks changed.
        if is_first_run:
            logger.info("First run — full backfill for states: %s", TRACKED_STATES)
            states_to_fetch = set(TRACKED_STATES)
        else:
            states_to_fetch = set()
            for key, dependents in COUNT_KEYS_TO_FETCH_LIST.items():
                if counts.get(key) != old_counts.get(key):
                    states_to_fetch.update(dependents)
            # Also fetch tracked states if last_order_completed_at advanced
            new_loc = counts.get("last_order_completed_at")
            old_loc = old_counts.get("__last_completed_at__")
            if new_loc and new_loc != old_loc:
                states_to_fetch.add("completed")

        # 2. For each state, fetch list and reconcile
        for state in sorted(states_to_fetch):
            await self._reconcile_state(auth, state, push=not is_first_run)

        # 3. Dispute cases (small list, fetch every cycle)
        await self._sync_cases(auth, push=not is_first_run)

        # 4. Persist counts snapshot
        snapshot = {k: int(counts[k]) for k in counts if isinstance(counts.get(k), int)}
        if counts.get("last_order_completed_at"):
            snapshot["__last_completed_at__"] = int(counts["last_order_completed_at"])
        self.db.set_marketplace_state_counts(self.platform, snapshot)

    async def _reconcile_state(self, auth, state: str, push: bool) -> None:
        loop = asyncio.get_running_loop()
        try:
            orders = await loop.run_in_executor(
                None, self.api.list_orders_by_status, state, auth)
        except Exception as e:
            logger.warning("list_orders_by_status(%s) failed: %s", state, e)
            return

        for order in orders:
            order_id = order.get("order_id") or ""
            order_item_id = order.get("order_item_id") or ""
            if not order_id:
                continue
            mp_state = (order.get("order_item_status") or state).lower()
            updated_at = order.get("updated_at")
            raw_json = json.dumps(order, ensure_ascii=False, default=str)

            prev = self.db.upsert_marketplace_status(
                self.platform, order_id, mp_state,
                order_item_id=order_item_id,
                marketplace_state_at=updated_at,
                raw_data=raw_json,
            )

            if not push:
                continue  # silent backfill
            if prev == mp_state:
                continue  # no actual change

            payload = {
                "platform": self.platform,
                "external_order_id": order_id,
                "marketplace_state": mp_state,
                "previous_state": prev,
                "marketplace_state_at": updated_at,
                "sub_states": {
                    "buyer_sub_status": order.get("buyer_sub_status"),
                    "seller_sub_status": order.get("seller_sub_status"),
                    "payment_status": order.get("payment_status"),
                },
                "raw_payload": order,
            }
            ok = await self.erp.push_status_update(payload)
            self.db.mark_marketplace_pushed(self.platform, order_id, ok)

    async def _sync_cases(self, auth, push: bool) -> None:
        """Fetch list_my_cases (paginate), push synthesized 'disputed' state to ERP
        for newly-open cases."""
        loop = asyncio.get_running_loop()
        next_key = ""
        scanned = 0
        for _ in range(20):  # safety cap on pagination
            try:
                results, next_key = await loop.run_in_executor(
                    None, self.api.list_my_cases, auth, "", next_key)
            except Exception as e:
                logger.warning("list_my_cases failed: %s", e)
                return
            if not results:
                break
            for case in results:
                case_id = str(case.get("case_id") or "")
                order_id = str(case.get("order_id") or "")
                status = (case.get("status") or "").lower()
                if not case_id or not order_id:
                    continue
                scanned += 1
                raw = json.dumps(case, ensure_ascii=False, default=str)
                prev = self.db.upsert_dispute(
                    self.platform, case_id, order_id, status,
                    report_case=case.get("report_case"),
                    report_reason=case.get("report_reason"),
                    report_qty=case.get("report_qty"),
                    raw_data=raw,
                )
                # New open case (just inserted or just transitioned to open)
                # synthesize 'disputed' to ERP. Only on push cycles.
                if push and status == "open" and prev != "open":
                    payload = {
                        "platform": self.platform,
                        "external_order_id": order_id,
                        "marketplace_state": "disputed",
                        "previous_state": None,
                        "marketplace_state_at": case.get("created_at"),
                        "is_dispute_open": True,
                        "raw_payload": case,
                    }
                    ok = await self.erp.push_status_update(payload)
                    if ok:
                        self.db.upsert_dispute(
                            self.platform, case_id, order_id, status,
                            mark_notified=True,
                        )
            if not next_key:
                break

        if scanned:
            logger.debug("g2g cases scanned: %d", scanned)
