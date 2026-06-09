"""Comprehensive ERP-side investigation for status_update redesign:
  - Full botpastedon.py source (handler + key validator + helpers)
  - Sell Order Python controller (before_save logic, workflow handlers)
  - Frappe hooks.py for any audit-related document_events
  - Existing audit/log doctypes
  - Workflow Action records — does Frappe record them and how
"""
import os
import re

import frappe

frappe.init(site="erp.gegeteam.net")
frappe.connect()
frappe.set_user("Administrator")

BENCH = "/home/frappe/frappe-bench"
APP = f"{BENCH}/apps/gege_custom/gege_custom/gege_custom"


def dump_file(path, header):
    print()
    print("=" * 78)
    print("FILE:", header)
    print("=" * 78)
    if not os.path.exists(path):
        print("  NOT FOUND:", path)
        return
    with open(path, "r") as f:
        print(f.read())


# 1. Full botpastedon.py handler
dump_file(f"{APP}/api/botpastedon.py", "api/botpastedon.py (full)")

# 2. Sell Order controller — focus on before_save / on_update / workflow events
so_py = f"{APP}/doctype/sell_order/sell_order.py"
if os.path.exists(so_py):
    print()
    print("=" * 78)
    print("FILE: doctype/sell_order/sell_order.py — TRIMMED to lifecycle methods")
    print("=" * 78)
    with open(so_py, "r") as f:
        src = f.read()
    # Find class methods that fire on save/transition
    interesting = (
        "before_save", "after_save", "on_update", "before_submit",
        "on_submit", "before_change", "validate", "on_change",
        "workflow", "_deliver", "_complete", "_cancel", "_refund",
        "_lock_inventory", "_unlock_inventory", "_finalize",
    )
    # Extract def-blocks containing these names
    lines = src.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(\s*)def (\w+)\(", line)
        if m and any(k in m.group(2) for k in interesting):
            indent = m.group(1)
            print(line)
            j = i + 1
            while j < len(lines):
                if lines[j].strip() == "" or lines[j].startswith(indent + "\t") \
                        or lines[j].startswith(indent + " "):
                    print(lines[j])
                    j += 1
                else:
                    break
            print("--")
            i = j
        else:
            i += 1

# 3. hooks.py document_events for Sell Order
hooks_py = f"{APP}/hooks.py"
if os.path.exists(hooks_py):
    print()
    print("=" * 78)
    print("FILE: hooks.py (Sell Order events + relevant overrides)")
    print("=" * 78)
    with open(hooks_py, "r") as f:
        src = f.read()
    # Find doc_events / document_events blocks
    for needle in ("doc_events", "document_events", "Sell Order"):
        idx = src.find(needle)
        if idx >= 0:
            print(f"--- around {needle!r} at offset {idx} ---")
            print(src[max(0, idx - 100):idx + 1200])
            print()

# 4. List custom doctypes that look audit-related
print()
print("=" * 78)
print("Custom doctypes in app (potential audit log candidates)")
print("=" * 78)
import glob
for d in sorted(glob.glob(f"{APP}/doctype/*")):
    name = os.path.basename(d)
    if any(k in name.lower() for k in ("audit", "log", "history", "event", "trail")):
        print("  *", name)
    else:
        print("   ", name)

# 5. Frappe Workflow Action — is it populated on this site?
print()
print("=" * 78)
print("Frappe Workflow Action records (last 10 for Sell Order)")
print("=" * 78)
rows = frappe.db.sql(
    "SELECT name, reference_name, status, user, workflow_state, "
    "       completed_by_role, creation "
    "FROM `tabWorkflow Action` "
    "WHERE reference_doctype = 'Sell Order' "
    "ORDER BY creation DESC LIMIT 10",
    as_dict=True,
)
if not rows:
    print("  (no Workflow Action rows for Sell Order)")
for r in rows:
    print("  {} | so={} | user={} | state={} | role={} | {}".format(
        r.name, r.reference_name, r.user, r.workflow_state,
        r.completed_by_role, str(r.creation)[:16],
    ))
print(f"  total Workflow Action rows for Sell Order: "
      f"{frappe.db.count('Workflow Action', {'reference_doctype': 'Sell Order'})}")

# 6. Frappe Version table change (built-in audit for any doctype change)
print()
print("=" * 78)
print("Frappe Version (built-in audit) — last 5 for any Sell Order")
print("=" * 78)
rows = frappe.db.sql(
    "SELECT name, owner, docname, data, creation "
    "FROM `tabVersion` "
    "WHERE ref_doctype = 'Sell Order' "
    "ORDER BY creation DESC LIMIT 5",
    as_dict=True,
)
for r in rows:
    # Parse data JSON
    import json
    try:
        d = json.loads(r.data)
        changed = d.get("changed", [])
        summary = ", ".join(f"{c[0]}: {c[1]!r}->{c[2]!r}" for c in changed[:3])
    except Exception:
        summary = "(parse err)"
    print("  {} | so={} | owner={} | {} | {}".format(
        r.name, r.docname, r.owner, str(r.creation)[:16], summary[:200]
    ))
print(f"  total Version rows for Sell Order: "
      f"{frappe.db.count('Version', {'ref_doctype': 'Sell Order'})}")

# 7. Look at api/bot_pastedon doctype — is there a 'BotPasteDon Bot' or similar?
print()
print("=" * 78)
print("Custom doctype names containing 'Bot' / 'BotPasteDon'")
print("=" * 78)
rows = frappe.db.sql(
    "SELECT name, module FROM `tabDocType` "
    "WHERE name LIKE '%Bot%' OR module LIKE '%Bot%' "
    "ORDER BY name LIMIT 20",
    as_dict=True,
)
for r in rows:
    print("  {} (module={})".format(r.name, r.module))
