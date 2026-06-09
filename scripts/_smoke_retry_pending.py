"""Local smoke test for PR1: ORDER_RETRY_PENDING + cleanup exemption + classifier.

Does not hit network or auth — uses a temp SQLite file.
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Smoke 1: imports + classifier behavior
import socket
from workers.g2g_worker import (
    _classify_error, _next_backoff_seconds, _build_retry_payload,
    RETRY_BACKOFF_SECONDS, MAX_RETRY_ATTEMPTS,
)
from shared.constants import (
    ORDER_DELIVERING, ORDER_COMPLETED, ORDER_FAILED, ORDER_RETRY_PENDING,
)
from shared.database import Database
from shared.order_state import TRANSITIONS

print("[1] imports OK")
print(f"    MAX_RETRY_ATTEMPTS={MAX_RETRY_ATTEMPTS}")
print(f"    RETRY_BACKOFF_SECONDS={RETRY_BACKOFF_SECONDS}")

# Smoke 2: classifier
cases = [
    (ConnectionError("Connection refused"), "network"),
    (TimeoutError("timed out"), "network"),
    (socket.gaierror(-3, "Temporary failure in name resolution"), "network"),
    (Exception("HTTPSConnectionPool ... Max retries exceeded ... NameResolutionError"), "network"),
    (Exception("connect timeout=60"), "network"),
    (Exception("HTTP 401 Unauthorized"), "auth"),
    (Exception("auth service error"), "auth"),
    (Exception("JWT expired"), "auth"),
    (Exception("delivered_qty: HTTP 400 | Validation error: Cannot perform action when order item status is delivering"), "terminal"),
    (Exception("some random thing"), "unknown"),
]
fail = 0
for exc, expected in cases:
    got = _classify_error(exc)
    ok = got == expected
    if not ok:
        fail += 1
    print(f"    {'OK' if ok else 'FAIL'}: {type(exc).__name__}({str(exc)[:50]!r}) -> {got} (want {expected})")

if fail:
    print(f"[2] classifier {fail} FAIL"); sys.exit(1)
print(f"[2] classifier OK")

# Smoke 3: backoff schedule
for rc in [1, 2, 5, 6, 10, 11, 15, 16, 50, 100, 1000]:
    print(f"    backoff(retry_count={rc}) = {_next_backoff_seconds(rc)}s")
assert _next_backoff_seconds(1) == 60
assert _next_backoff_seconds(6) == 300
assert _next_backoff_seconds(11) == 1800
assert _next_backoff_seconds(16) == 3600
assert _next_backoff_seconds(1000) == 3600
print("[3] backoff OK")

# Smoke 4: retry payload structure
payload = _build_retry_payload(
    {"order_id": "TEST123", "files": ["a"], "skip_steps": ["qty"]},
    "network", retry_count=3, err_msg="some s3 error",
)
assert payload["task_data"]["skip_steps"] == ["qty"]
assert payload["category"] == "network"
assert payload["retry_count"] == 3
assert payload["next_retry_at"]
assert payload["last_error"] == "some s3 error"
print(f"[4] payload OK: {json.dumps(payload, indent=2)[:300]}")

# Smoke 5: DB transitions + cleanup exemption
tmpdir = tempfile.mkdtemp()
db_path = os.path.join(tmpdir, "smoke.db")
db = Database(db_path)
db.insert_order("g2g", "ORD_RETRY", {
    "url": "u", "game": "G", "itemName": "i", "quantity": "1",
    "customerName": "cust",
})
db.update_order_status("ORD_RETRY", ORDER_DELIVERING)
db.mark_retry_attempt(
    "ORD_RETRY",
    retry_data_json=json.dumps(payload),
    error_message="NETWORK:s3 timeout",
    retry_count=1,
)
row = db.get_order("ORD_RETRY")
assert row["status"] == ORDER_RETRY_PENDING, f"got status {row['status']}"
assert row["retry_count"] == 1
assert "NETWORK:" in row["error_message"]
assert json.loads(row["retry_data"])["category"] == "network"
print(f"[5a] mark_retry_attempt OK: status={row['status']} retry_count={row['retry_count']}")

# Add a stale COMPLETED order (older than 3h) and a stale RETRY_PENDING
# Manually push updated_at back in time via SQL.
conn = sqlite3.connect(db_path)
db.insert_order("g2g", "ORD_DONE", {"url": "u"})
db.update_order_status("ORD_DONE", ORDER_COMPLETED)
db.insert_order("g2g", "ORD_FAILED", {"url": "u"})
db.update_order_status("ORD_FAILED", ORDER_FAILED, error_message="TERMINAL:x")
conn.execute(
    "UPDATE orders SET updated_at = datetime('now', '-5 hours') "
    "WHERE order_id IN ('ORD_DONE', 'ORD_FAILED', 'ORD_RETRY')"
)
conn.commit()
conn.close()

db.cleanup_old_orders()

remain = {r["order_id"]: r["status"] for r in [
    db.get_order(oid) for oid in ("ORD_DONE", "ORD_FAILED", "ORD_RETRY") if db.get_order(oid)
]}
print(f"[5b] cleanup result: {remain}")
assert "ORD_DONE" not in remain, "COMPLETED should have been pruned"
assert remain.get("ORD_FAILED") == ORDER_FAILED, "FAILED must survive (audit trail)"
assert remain.get("ORD_RETRY") == ORDER_RETRY_PENDING, "RETRY_PENDING must NEVER be pruned"
print("[5b] cleanup exemption OK")

# Smoke 6: state machine transitions
assert ORDER_RETRY_PENDING in TRANSITIONS[ORDER_DELIVERING]
assert ORDER_DELIVERING in TRANSITIONS[ORDER_RETRY_PENDING]
assert ORDER_FAILED in TRANSITIONS[ORDER_RETRY_PENDING]
print("[6] transitions OK")

print("\nAll smoke checks passed.")
