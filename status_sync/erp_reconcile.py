"""ERP-driven reconcile — closes the g2g list-window gap.

status_sync only sees the ~100 newest orders per status via list_my_order, so older
completions/cancellations are never fetched and ERP stays stuck non-terminal. Here ERP
lists ITS non-terminal marketplace orders; the bot looks each up on the marketplace
(`get_order_detail`) and pushes the real terminal state via status_update (idempotent).

ERP is stateless (just the list). The bot owns throttle + per-order back-off + batch cap
so a big backlog (prod: ~578 g2g) drains over a few runs without a 429 storm. Self-draining:
once an order is pushed terminal, ERP drops it from the pending list and we stop checking.
"""

import asyncio
from datetime import datetime, timezone

from shared.logging_config import setup_logger

logger = setup_logger("status_sync.erp_reconcile")


def _is_rate_limit(exc) -> bool:
    """G2G RateLimitError without importing g2g_api (keeps this module curl_cffi-free
    + unit-testable). RateLimitError carries status==429."""
    return getattr(exc, "status", None) == 429 or type(exc).__name__ == "RateLimitError"

# marketplace order_item_status -> state we push to ERP. Anything else (delivering /
# preparing / unknown) is still in progress: record for back-off, don't push.
_TERMINAL_LOOKUP = {
    "completed": "completed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "refunded": "cancelled",
}


def _lookup_id(platform, ext):
    """Marketplace lookup id: g2g detail wants order_item_id (ext + '-1');
    eldorado's /orders/me/{id} takes the order GUID itself."""
    return ext if platform == "eldorado" else ext + "-1"


def _db_key(platform, ext):
    """marketplace_status key: eldo GUIDs are stored lowercase (ERP stores UPPER,
    sqlite compares exact) — normalize or the back-off never matches its row."""
    return ext.lower() if platform == "eldorado" else ext


def _detail_status(platform, detail) -> str:
    """Extract the order status from a detail payload, per platform shape.

    g2g: flat `order_item_status`. eldorado: nested `state.state` (CamelCase) +
    EL-3: a Completed order with hasBeenRefundedPostCompletion means the money
    was clawed back → treat as canceled so ERP reverses the wallet credit.
    """
    detail = detail or {}
    if platform == "eldorado":
        status = str(((detail.get("state") or {}).get("state")) or "").lower()
        if status == "completed" and detail.get("hasBeenRefundedPostCompletion"):
            return "canceled"
        return status
    return str(detail.get("order_item_status") or "").lower()


def _recently_checked(db, platform, order_id, backoff_h) -> bool:
    """True if this order was looked up within backoff_h and was still non-terminal —
    skip it this run so we don't re-hammer orders that are genuinely still delivering."""
    row = db.get_marketplace_status(platform, order_id)
    if not row or not row.get("last_synced_at"):
        return False
    if (row.get("marketplace_state") or "") in _TERMINAL_LOOKUP:
        return False  # terminal locally but ERP still lists it → keep trying to push
    try:
        ts = datetime.fromisoformat(str(row["last_synced_at"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    return age_h < backoff_h


async def reconcile_from_erp(db, erp, api, auth, platform, *,
                             batch, throttle, backoff_h) -> tuple:
    """One ERP-driven reconcile pass. Returns (completed, cancelled, skipped)."""
    # Over-fetch: back-off filters out most still-delivering ones, so ask for more than
    # `batch` to keep a full batch of fresh lookups available.
    orders = await erp.get_pending_orders(platform, limit=batch * 3)
    if not orders:
        return 0, 0, 0

    loop = asyncio.get_running_loop()
    completed = cancelled = skipped = looked = 0

    for o in orders:
        ext = (o.get("external_order_id") or "").strip()
        if not ext:
            continue
        key = _db_key(platform, ext)
        if _recently_checked(db, platform, key, backoff_h):
            continue
        if looked >= batch:
            break
        looked += 1

        try:
            detail = await loop.run_in_executor(None, api.get_order_detail, _lookup_id(platform, ext), auth)
        except Exception as e:
            if _is_rate_limit(e):
                logger.warning("erp_reconcile rate-limited after %d lookups — stop (retry_after=%ss)",
                               looked, getattr(e, "retry_after", "?"))
                break
            logger.warning("erp_reconcile lookup %s failed: %s", ext, str(e)[:120])
            continue

        status = _detail_status(platform, detail)
        # Record what we saw — updates last_synced_at (drives back-off).
        db.upsert_marketplace_status(platform, key, status or "unknown",
                                     order_item_id=None if platform == "eldorado" else ext + "-1")

        target = _TERMINAL_LOOKUP.get(status)
        if not target:
            skipped += 1  # still delivering/preparing — re-check after back-off
            await asyncio.sleep(throttle)
            continue

        ok = await erp.push_status_update({
            "platform": platform,
            "external_order_id": ext,
            "marketplace_state": target,
            "raw_payload": detail,
        })
        if ok:
            db.mark_marketplace_pushed(platform, key, True)
            if target == "completed":
                completed += 1
            else:
                cancelled += 1
        await asyncio.sleep(throttle)

    logger.info("%s erp_reconcile: looked=%d completed=%d cancelled=%d skip=%d (pending=%d)",
                platform, looked, completed, cancelled, skipped, len(orders))
    return completed, cancelled, skipped
