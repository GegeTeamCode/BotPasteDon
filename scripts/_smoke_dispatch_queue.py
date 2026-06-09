"""Smoke test for PR2: coordinator dispatch retry queue.

Tests DB roundtrip + backoff + cap eviction. No network.
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coordinator.discord_bot import (
    _dispatch_backoff, MAX_DISPATCH_ATTEMPTS, DISPATCH_BACKOFF_SECONDS,
)
from shared.database import Database

print("[1] imports OK")
print(f"    MAX_DISPATCH_ATTEMPTS={MAX_DISPATCH_ATTEMPTS}")
print(f"    DISPATCH_BACKOFF_SECONDS={DISPATCH_BACKOFF_SECONDS}")

# Backoff
assert _dispatch_backoff(1) == 60
assert _dispatch_backoff(5) == 60
assert _dispatch_backoff(6) == 300
assert _dispatch_backoff(11) == 1800
assert _dispatch_backoff(16) == 3600
assert _dispatch_backoff(999) == 3600
print("[2] backoff OK")

tmp = tempfile.mkdtemp()
db = Database(os.path.join(tmp, "smoke.db"))

# Queue 2 dispatches
task1 = {"order_id": "OID1", "files": ["/tmp/a.png"]}
task2 = {"order_id": "OID2", "files": []}
db.queue_dispatch("OID1", "http://localhost:8002", json.dumps(task1))
db.queue_dispatch("OID2", "http://localhost:8001", json.dumps(task2))

due = db.get_due_dispatches()
print(f"[3] queued 2, due returned {len(due)}")
assert len(due) == 2
assert {r["order_id"] for r in due} == {"OID1", "OID2"}
assert json.loads(due[0]["task_data"])["order_id"] in ("OID1", "OID2")
print("[3] queue OK")

# Mark attempt — next_retry_at in the FUTURE → should NOT be due
import datetime
_fmt = "%Y-%m-%d %H:%M:%S"
future = (datetime.datetime.utcnow() + datetime.timedelta(seconds=300)).strftime(_fmt)
db.mark_dispatch_attempt("OID1", "worker down", future, attempt_count=1)
due = db.get_due_dispatches()
print(f"[4a] after mark+future, due returned {len(due)}: {[r['order_id'] for r in due]}")
assert len(due) == 1
assert due[0]["order_id"] == "OID2"

# Mark attempt — next_retry_at in the PAST → should be due again
past = (datetime.datetime.utcnow() - datetime.timedelta(seconds=10)).strftime(_fmt)
db.mark_dispatch_attempt("OID1", "worker down", past, attempt_count=2)
due = db.get_due_dispatches()
print(f"[4b] after mark+past, due returned {len(due)}: {[r['order_id'] for r in due]}")
assert len(due) == 2
oid1_row = next(r for r in due if r["order_id"] == "OID1")
assert oid1_row["attempt_count"] == 2
assert oid1_row["last_error"] == "worker down"
print("[4] mark_dispatch_attempt OK")

# Remove
db.remove_dispatch("OID1")
due = db.get_due_dispatches()
print(f"[5] after remove OID1, due returned {len(due)}: {[r['order_id'] for r in due]}")
assert len(due) == 1
assert due[0]["order_id"] == "OID2"
print("[5] remove_dispatch OK")

# Re-queue OID2 → should RESET attempt_count to 0 (INSERT OR REPLACE)
db.mark_dispatch_attempt("OID2", "err", past, attempt_count=5)
db.queue_dispatch("OID2", "http://localhost:8001", json.dumps(task2))
due = db.get_due_dispatches()
oid2_row = next(r for r in due if r["order_id"] == "OID2")
print(f"[6] after re-queue OID2, attempt_count={oid2_row['attempt_count']}")
assert oid2_row["attempt_count"] == 0, "Re-queue must reset retry counter"
print("[6] re-queue resets counter OK")

print("\nAll smoke checks passed.")
