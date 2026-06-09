"""Inspect ERP Sell Order workflow + state distribution + handler code."""
import datetime
import os
import subprocess

import frappe

frappe.init(site="erp.gegeteam.net")
frappe.connect()
frappe.set_user("Administrator")

print("=== Sell Order columns (find date column) ===")
cols = frappe.db.sql(
    "SHOW COLUMNS FROM `tabSell Order` "
    "WHERE Field IN ('creation','modified','transaction_date','posting_date',"
    "                'order_date','channel_create_at','channel_completed_at',"
    "                'channel_delivered_at','external_order_id','sell_channel',"
    "                'workflow_state')",
    as_dict=True,
)
for c in cols:
    print("  {:25} {}".format(c.Field, c.Type))

print()
print("=== Recent Delivered SOs (top 20 by creation) ===")
rows = frappe.db.sql(
    "SELECT name, sell_channel, external_order_id, workflow_state, creation, modified "
    "FROM `tabSell Order` "
    "WHERE workflow_state = 'Delivered' "
    "ORDER BY creation DESC LIMIT 20",
    as_dict=True,
)
for r in rows:
    print("  {} | ch={:10} | ext={:28} | created={} | modified={}".format(
        r.name, (r.sell_channel or "-")[:10],
        (r.external_order_id or "-")[:28],
        str(r.creation)[:16], str(r.modified)[:16],
    ))

print()
print("=== Delivered SO count by sell_channel (creation >= 30 days ago) ===")
cutoff = (datetime.datetime.now() - datetime.timedelta(days=30))
rows = frappe.db.sql(
    "SELECT sell_channel, COUNT(*) AS cnt "
    "FROM `tabSell Order` "
    "WHERE workflow_state = 'Delivered' AND creation >= %s "
    "GROUP BY sell_channel ORDER BY cnt DESC",
    (cutoff,),
    as_dict=True,
)
for r in rows:
    print("  {:15} {}".format(r.sell_channel or "-", r.cnt))

print()
print("=== Status_update handler signature ===")
handler_path = ("/home/frappe/frappe-bench/apps/gege_custom/gege_custom/"
                "gege_custom/api/botpastedon.py")
if os.path.exists(handler_path):
    with open(handler_path, "r") as f:
        src = f.read()
    # Find def status_update + 100 lines context
    idx = src.find("def status_update")
    if idx >= 0:
        end = src.find("\n@", idx + 5)  # next decorator marks next function
        if end < 0:
            end = idx + 6000
        print(src[idx:end][:5500])
    else:
        print("  status_update not found in file")
    print()
    print("=== _map_marketplace_to_workflow helper ===")
    idx = src.find("def _map_marketplace_to_workflow")
    if idx >= 0:
        end = src.find("\n\ndef ", idx + 5)
        print(src[idx:end if end > 0 else idx + 2500][:2500])
    else:
        print("  helper not found")
else:
    print("  handler file NOT FOUND at", handler_path)
