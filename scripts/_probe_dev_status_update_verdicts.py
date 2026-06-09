"""Hit dev webhook with 7 different scenarios to verify each verdict path.

Mutating tests:
  - `updated`: flips SO-260603-1Z92628L Delivered -> Completed.
    Then explicit revert step at the end.
Non-mutating tests:
  - `no_change`, `protected`, `manual_required`, `unsafe_transition`,
    `ignored`, `no_so`
"""
import paramiko
import requests
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

URL = "http://192.168.2.228:8000/api/method/gege_custom.gege_custom.api.botpastedon.status_update"
G2G_KEY = "test_secret_g2g_456"
ELDO_KEY = "test_secret_eldo_123"


def call(payload, key=G2G_KEY, label=""):
    headers = {"X-API-Key": key, "Content-Type": "application/json",
               "Host": "test.localhost"}
    try:
        r = requests.post(URL, json=payload, headers=headers, timeout=15)
        print(f"[{label}] HTTP {r.status_code}")
        body = r.text
        try:
            j = r.json()
            inner = j.get("message", j)
            print(f"  -> {inner}")
            return inner if isinstance(inner, dict) else j
        except Exception:
            print(f"  -> {body[:300]}")
            return None
    except Exception as e:
        print(f"[{label}] EXC: {e}")
        return None


SO_DELIVERED = "SO-260603-1Z92628L"
EXT_DELIVERED = "1780504917427XG8F"

SO_COMPLETED = "SO-260603-YREDFG1A"
EXT_COMPLETED = "1780439666510IOWU"

SO_OUTSTANDING = "SO-260602-V8ZT2CLF"
EXT_OUTSTANDING = "1780374336777JTWA"

SO_IN_DELIVERY = "SO-260603-PX3CWA16"
EXT_IN_DELIVERY = "1780503408290MLQF"

SO_QUEUED = "SO-260603-ZJUUCBN0"
EXT_QUEUED = "1780505767739L9YB"


print("=" * 78)
print("Test 1: `updated` — Delivered → Completed")
print("=" * 78)
v1 = call({
    "platform": "g2g",
    "external_order_id": EXT_DELIVERED,
    "marketplace_state": "completed",
    "previous_state": "delivered",
    "marketplace_state_at": int(time.time()),
}, label="UPDATED")
assert v1 and v1.get("status") == "updated", f"expected 'updated', got {v1}"
print()

print("=" * 78)
print("Test 2: `no_change` — Completed → Completed (idempotent)")
print("=" * 78)
v2 = call({
    "platform": "g2g",
    "external_order_id": EXT_DELIVERED,  # now Completed after test 1
    "marketplace_state": "completed",
}, label="NO_CHANGE")
assert v2 and v2.get("status") == "no_change", f"expected 'no_change', got {v2}"
print()

print("=" * 78)
print("Test 3: `protected` — Outstanding SO")
print("=" * 78)
v3 = call({
    "platform": "g2g",
    "external_order_id": EXT_OUTSTANDING,
    "marketplace_state": "completed",
}, label="PROTECTED")
assert v3 and v3.get("status") == "protected", f"expected 'protected', got {v3}"
print()

print("=" * 78)
print("Test 4: `manual_required` — In Delivery SO")
print("=" * 78)
v4 = call({
    "platform": "g2g",
    "external_order_id": EXT_IN_DELIVERY,
    "marketplace_state": "completed",
}, label="MANUAL_REQUIRED")
assert v4 and v4.get("status") == "manual_required", f"expected 'manual_required', got {v4}"
print()

print("=" * 78)
print("Test 5: `unsafe_transition` — Queued (not in whitelist source)")
print("=" * 78)
v5 = call({
    "platform": "g2g",
    "external_order_id": EXT_QUEUED,
    "marketplace_state": "completed",
}, label="UNSAFE_TRANSITION")
assert v5 and v5.get("status") == "unsafe_transition", f"expected 'unsafe_transition', got {v5}"
print()

print("=" * 78)
print("Test 6: `ignored` — unmapped state")
print("=" * 78)
v6 = call({
    "platform": "g2g",
    "external_order_id": EXT_DELIVERED,
    "marketplace_state": "pendingdelivery",
}, label="IGNORED")
assert v6 and v6.get("status") == "ignored", f"expected 'ignored', got {v6}"
print()

print("=" * 78)
print("Test 7: `no_so` — fake external_order_id")
print("=" * 78)
v7 = call({
    "platform": "g2g",
    "external_order_id": "FAKE_PROBE_DOES_NOT_EXIST",
    "marketplace_state": "completed",
}, label="NO_SO")
assert v7 and v7.get("status") == "no_so", f"expected 'no_so', got {v7}"
print()

print("=" * 78)
print("REVERT: flip SO_DELIVERED Completed -> Delivered (via Frappe ORM)")
print("=" * 78)
# Use SSH + Frappe to revert
import paramiko
s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.2.228", username="yuko", password="Gege@126", timeout=15)
revert_script = f'''
import frappe
frappe.init(site="test.localhost")
frappe.connect()
frappe.set_user("Administrator")
so = frappe.get_doc("Sell Order", "{SO_DELIVERED}")
print("before revert: state =", so.workflow_state)
so.workflow_state = "Delivered"
so.flags.ignore_permissions = True
so.save()
frappe.db.commit()
print("after revert : state =", so.workflow_state)
'''
sftp = s.open_sftp()
with sftp.open("/tmp/_revert.py", "w") as f:
    f.write(revert_script)
sftp.close()
_, o, e = s.exec_command(
    "cd /home/yuko/gege-dev/frappe-bench/sites && "
    "../env/bin/python /tmp/_revert.py 2>&1",
    timeout=30,
)
print(o.read().decode(errors="replace"))
s.close()

print()
print("=" * 78)
print("VERIFY: WS Activity Log entries from test")
print("=" * 78)
s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.2.228", username="yuko", password="Gege@126", timeout=15)
verify_script = '''
import frappe
frappe.init(site="test.localhost")
frappe.connect()
frappe.set_user("Administrator")
rows = frappe.get_all(
    "WS Activity Log",
    filters={"action": "status_update"},
    fields=["name", "status", "detail", "reference_sell_order", "creation"],
    order_by="creation desc",
    limit=10,
)
for r in rows:
    print("  [{}] {} | so={} | {}".format(
        str(r.creation)[11:19], r.status, r.reference_sell_order or "-",
        (r.detail or "")[:120]))
'''
sftp = s.open_sftp()
with sftp.open("/tmp/_verify.py", "w") as f:
    f.write(verify_script)
sftp.close()
_, o, _ = s.exec_command(
    "cd /home/yuko/gege-dev/frappe-bench/sites && "
    "../env/bin/python /tmp/_verify.py 2>&1",
    timeout=30,
)
print(o.read().decode(errors="replace"))
s.close()

print()
print("All 7 verdict tests PASSED.")
