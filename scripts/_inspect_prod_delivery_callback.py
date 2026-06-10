"""Check prod WS Activity Log for delivery_callback history.

Goal: confirm whether the save()-as-Guest bug has actually fired in
prod, and which payload actions were involved.
"""
import json as _json

import frappe

frappe.init(site="erp.gegeteam.net")
frappe.connect()
frappe.set_user("Administrator")

print("=== delivery_callback by status (all-time) ===")
rows = frappe.get_all(
    "WS Activity Log",
    filters={"action": "delivery_callback"},
    fields=["status"],
    limit=20000,
)
by_status = {}
for r in rows:
    by_status[r.status] = by_status.get(r.status, 0) + 1
print(f"  Total: {len(rows)}")
for k, v in sorted(by_status.items(), key=lambda x: -x[1]):
    print(f"    {k:15} {v}")

print()
print("=== Recent Errors (last 30) ===")
err_rows = frappe.get_all(
    "WS Activity Log",
    filters={"action": "delivery_callback", "status": "Error"},
    fields=["name", "detail", "creation"],
    order_by="creation desc",
    limit=30,
)
for r in err_rows:
    print("  [{}] {}".format(str(r.creation)[:19], (r.detail or "")[:200]))
if not err_rows:
    print("  (no Error entries — bug never logged as Error here)")

print()
print("=== Detail-text search for PermissionError ===")
perm_rows = frappe.get_all(
    "WS Activity Log",
    filters={"action": "delivery_callback",
             "detail": ["like", "%PermissionError%"]},
    fields=["creation", "detail"],
    order_by="creation desc",
    limit=20,
)
print(f"  Hits: {len(perm_rows)}")
for r in perm_rows:
    print("  [{}] {}".format(str(r.creation)[:19], (r.detail or "")[:200]))

print()
print("=== Action breakdown in recent 50 callback payloads ===")
rows = frappe.get_all(
    "WS Activity Log",
    filters={"action": "delivery_callback"},
    fields=["payload", "status", "creation"],
    order_by="creation desc",
    limit=50,
)
counts_action = {}
status_by_action = {}
for r in rows:
    try:
        p = _json.loads(r.payload or "{}")
        a = p.get("action", "(none)")
        s = r.status or "(none)"
        counts_action[a] = counts_action.get(a, 0) + 1
        status_by_action.setdefault(a, {})[s] = status_by_action[a].get(s, 0) + 1
    except Exception:
        pass
print(f"  Action distribution: {counts_action}")
for a, by_s in status_by_action.items():
    print(f"    {a}: {by_s}")

print()
print("=== Recent samples (last 10, with payload action + success + status) ===")
for r in rows[:10]:
    try:
        p = _json.loads(r.payload or "{}")
    except Exception:
        p = {}
    print("  [{}] log_status={:10} payload.action={:18} payload.success={}".format(
        str(r.creation)[:19], r.status or "-",
        str(p.get("action", "-"))[:18],
        p.get("success", "-")))

print()
print("=== Frappe error log for any save() permission failures on Sell Order ===")
err_log = frappe.get_all(
    "Error Log",
    filters={"error": ["like", "%PermissionError%Sell Order%"]},
    fields=["name", "method", "creation"],
    order_by="creation desc",
    limit=20,
)
print(f"  Total Error Log hits mentioning Sell Order + PermissionError: {len(err_log)}")
for r in err_log:
    print(f"  [{str(r.creation)[:19]}] method={r.method} | {r.name}")
