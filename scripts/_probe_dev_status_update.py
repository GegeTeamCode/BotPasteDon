"""Probe new status_update behavior on dev .228 — test 6 verdicts.

Runs ON the dev server (SCP'd to /tmp). Uses Frappe ORM to:
  1. Find sample SOs in different workflow_state
  2. Build candidate test cases for each verdict
  3. Print all SO names so caller can construct HTTP test payloads
"""
import json

import frappe

frappe.init(site="test.localhost")
frappe.connect()
frappe.set_user("Administrator")

print("=== Sample SOs per workflow_state (need ext_id) ===")
for wf in ("Delivered", "In Delivery", "Completed", "Queued",
            "Outstanding", "Refunded", "Disputed", "Cancelled"):
    rows = frappe.get_all(
        "Sell Order",
        filters={
            "workflow_state": wf,
            "external_order_id": ["not in", ["", None]],
        },
        fields=["name", "workflow_state", "sell_channel", "external_order_id"],
        order_by="creation desc",
        limit=3,
    )
    print(f"  {wf}:")
    if not rows:
        print("    (none with external_order_id)")
        continue
    for r in rows:
        print("    {} | ch={} | ext={}".format(
            r.name, r.sell_channel or "-", r.external_order_id))

print()
print("=== Total SO count by state ===")
for r in frappe.db.sql(
    "SELECT workflow_state, COUNT(*) cnt FROM `tabSell Order` "
    "GROUP BY workflow_state ORDER BY cnt DESC",
    as_dict=True,
):
    print("  {:25} {}".format(r.workflow_state or "(null)", r.cnt))
