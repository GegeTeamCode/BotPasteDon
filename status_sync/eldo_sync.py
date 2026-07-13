"""Eldorado platform status sync — polls statesCount + paginated seller/orders."""

import asyncio
import json
from typing import Optional

from shared.eldo_api import EldoradoAPIClient
from shared.eldo_auth import EldoAuthManager
from shared.database import Database
from shared.logging_config import setup_logger

from status_sync.erp_client import ERPClient
from status_sync.erp_reconcile import reconcile_from_erp
from status_sync.reconcile import reconcile_unpushed
from shared.config import (
    ERP_GO_LIVE_ISO,
    ERP_RECONCILE_BACKOFF_H,
    ERP_RECONCILE_BATCH,
    ERP_RECONCILE_EVERY_N_CYCLES,
    ERP_RECONCILE_THROTTLE_SEC,
)

logger = setup_logger("status_sync.eldo")


# States we push to ERP. PendingDelivery / Received are ignored (handled by traders
# or treated identically to Delivered → no transition).
TRACKED_STATES = ("Delivered", "Disputed", "Completed", "Canceled")

# Map ERP-side lowercase to API CamelCase
STATE_CASE = {s.lower(): s for s in TRACKED_STATES}


class EldoSync:
    """Run one sync cycle on Eldorado."""

    def __init__(self, db: Database, erp: ERPClient,
                 auth_mgr: Optional[EldoAuthManager] = None):
        self.db = db
        self.erp = erp
        self.auth_mgr = auth_mgr or EldoAuthManager()
        self.api = EldoradoAPIClient(self.auth_mgr)
        self.platform = "eldorado"
        self._cycle = 0

    async def run_once(self) -> None:
        # Reconcile orphaned terminal pushes first — independent of marketplace
        # auth/API, so it runs even if the eldo fetch below fails this cycle.
        try:
            await reconcile_unpushed(
                self.db, self.erp, self.platform,
                [s.lower() for s in TRACKED_STATES],
                created_json_path="$.createdDate", created_min=ERP_GO_LIVE_ISO,
            )
        except Exception as e:
            logger.warning("reconcile_unpushed failed: %s", e)

        try:
            auth = await self.auth_mgr.get_auth()
        except Exception as e:
            logger.warning("Auth fetch failed: %s — skip cycle", e)
            return

        loop = asyncio.get_running_loop()

        try:
            counts = await loop.run_in_executor(None, self.api.get_states_count, auth)
        except Exception as e:
            logger.warning("get_states_count failed: %s", e)
            return

        # counts keys are camelCase like {pendingDelivery, delivered, completed, ...}
        old_counts = self.db.get_marketplace_state_counts(self.platform)
        is_first_run = not old_counts

        states_to_fetch: list = []
        if is_first_run:
            logger.info("First run — full backfill for states: %s", TRACKED_STATES)
            states_to_fetch = list(TRACKED_STATES)
        else:
            for state in TRACKED_STATES:
                key = state[0].lower() + state[1:]  # camelCase
                if counts.get(key, 0) != old_counts.get(key, 0):
                    states_to_fetch.append(state)

        for state in states_to_fetch:
            await self._reconcile_state(auth, state, push=not is_first_run,
                                         full_backfill=is_first_run)

        # ERP-driven reconcile (per-order lookups) — mirror g2g_sync step 4. Closes the
        # "completed-after-first-push" gap: an order pushed once (last_pushed_at set)
        # whose terminal edge was recorded during a no-push pass is never re-pushed by
        # reconcile_unpushed, and the incremental list scan misses it once it leaves
        # the window (case SO-260708-H3PZDMX8, 2026-07-14).
        if not is_first_run and self._cycle % ERP_RECONCILE_EVERY_N_CYCLES == 0:
            try:
                await reconcile_from_erp(
                    self.db, self.erp, self.api, auth, self.platform,
                    batch=ERP_RECONCILE_BATCH, throttle=ERP_RECONCILE_THROTTLE_SEC,
                    backoff_h=ERP_RECONCILE_BACKOFF_H,
                )
            except Exception as e:
                logger.warning("erp_reconcile failed: %s", e)
        self._cycle += 1

        # Persist counts snapshot
        snapshot = {k: int(v) for k, v in counts.items() if isinstance(v, int)}
        self.db.set_marketplace_state_counts(self.platform, snapshot)

    async def _reconcile_state(self, auth, state: str, push: bool,
                                full_backfill: bool) -> None:
        """Paginate the seller/orders list for `state`. On full_backfill, scan all
        pages (silent). On incremental, scan until catching up to known orders."""
        loop = asyncio.get_running_loop()
        cursor = ""
        pages = 0
        # Safety caps to avoid runaway pagination
        max_pages = 1500 if full_backfill else 25
        consecutive_known = 0
        for _ in range(max_pages):
            try:
                results, next_cursor = await loop.run_in_executor(
                    None, self.api.list_orders_by_state, state, auth, cursor, 50)
            except Exception as e:
                logger.warning("list_orders_by_state(%s) cursor=%s failed: %s",
                                state, cursor[:30], e)
                return
            if not results:
                break
            pages += 1

            for order in results:
                order_id = str(order.get("id") or "")
                if not order_id:
                    continue
                mp_state = state.lower()
                # EL-3 (refund-after-completed): a completed order carrying the
                # post-completion-refund flag → treat as cancelled so ERP reverses the
                # wallet credit. The reliable path is the order moving to Canceled (caught
                # by the canceled count tripwire → cancelled → reverse); this is a
                # belt-and-suspenders for the rare case the state stays Completed.
                if mp_state == "completed" and order.get("hasBeenRefundedPostCompletion"):
                    mp_state = "canceled"
                # Eldo uses ISO datetime; if order has lastStateChangeDate prefer it
                state_at = order.get("lastStateChangeDate") or order.get("createdDate")
                raw_json = json.dumps(order, ensure_ascii=False, default=str)

                prev = self.db.upsert_marketplace_status(
                    self.platform, order_id, mp_state,
                    marketplace_state_at=None,  # ISO string, not epoch — skip for now
                    raw_data=raw_json,
                )

                if push and prev != mp_state:
                    payload = {
                        "platform": self.platform,
                        "external_order_id": order_id,
                        "marketplace_state": mp_state,
                        "previous_state": prev,
                        "marketplace_state_at": state_at,
                        "raw_payload": order,
                    }
                    if mp_state == "disputed":
                        # EL-2: Eldo dispute is an order state; intent lives in
                        # latestDispute. No cancel/dispute split (g2g has report_case) —
                        # always "Dispute Open" + reason → Order Log. ERP clears the alert
                        # when the order later resolves to any non-alert state (gap a).
                        ld = order.get("latestDispute") or {}
                        payload["alert"] = True
                        payload["report_reason"] = ld.get("reason")
                    ok = await self.erp.push_status_update(payload)
                    self.db.mark_marketplace_pushed(self.platform, order_id, ok)
                    consecutive_known = 0
                else:
                    consecutive_known += 1

            # Incremental: stop early when we've seen enough known orders in a row
            # (likely caught up to last sync)
            if not full_backfill and consecutive_known >= 50:
                break
            if not next_cursor:
                break
            cursor = next_cursor

        logger.debug("eldo %s: scanned %d pages", state, pages)
