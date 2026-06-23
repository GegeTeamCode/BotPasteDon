"""Reconcile terminal orders recorded locally but never pushed to ERP.

Root cause this guards against: the first-run backfill records marketplace states
with push=False, and incremental cycles only push on state-change (prev != new) and
only fetch a state when its count tripwire moves. An order already terminal (e.g.
completed) at backfill time — or whose transition the count tripwire missed — is
stored locally but never pushed, so ERP never learns it completed and the
marketplace-wallet credit (ERP fires it on Completed) never happens.

This pass re-pushes any tracked-state order with last_pushed_at IS NULL, bounded to
orders created on/after ERP go-live (pre-ERP orders were never in ERP — pushing them
only yields no_so noise). Safe to repeat every cycle: ERP status_update is idempotent
(no_change/protected) and the wallet credit skips when an In-ALE already exists.
"""

import json

from shared.logging_config import setup_logger

logger = setup_logger("status_sync.reconcile")


async def reconcile_unpushed(db, erp, platform, states, limit=1000,
                             created_json_path=None, created_min=None):
    """Push locally-recorded-but-unpushed terminal orders to ERP. Returns (seen, pushed)."""
    rows = db.get_unpushed_marketplace(
        platform, states, limit=limit,
        created_json_path=created_json_path, created_min=created_min,
    )
    if not rows:
        return 0, 0
    pushed = 0
    for row in rows:
        try:
            raw = json.loads(row["raw_data"]) if row.get("raw_data") else {}
        except Exception:
            raw = {}
        payload = {
            "platform": platform,
            "external_order_id": row["order_id"],
            "marketplace_state": row["marketplace_state"],
            "previous_state": None,
            "marketplace_state_at": row.get("marketplace_state_at"),
            "raw_payload": raw,
        }
        ok = await erp.push_status_update(payload)
        db.mark_marketplace_pushed(platform, row["order_id"], ok)
        if ok:
            pushed += 1
    logger.info("%s reconcile: pushed %d/%d previously-unpushed (states=%s)",
                platform, pushed, len(rows), states)
    return len(rows), pushed
