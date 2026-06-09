"""Diagnose why 2 orders did not get evidence delivered by worker.

Run from Windows host. Reads DB on 192.168.2.220 then greps /tmp/*.log
for the order IDs.
"""
import paramiko
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ORDERS = ["1780960546099KOT6", "1780678743300JEXR"]

REMOTE_INSPECT = r"""
import sqlite3, json, os
ORDERS = {orders!r}
db = '/opt/BotPasteDon/data/orders.db'
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row
print('=== orders table ===')
for oid in ORDERS:
    row = c.execute('SELECT * FROM orders WHERE order_id=?', (oid,)).fetchone()
    if not row:
        print(f'[{{oid}}] NOT FOUND in orders table')
        continue
    d = dict(row)
    print(f'--- {{oid}} ---')
    for k in ('platform','status','order_url','game','item_name','quantity','customer_name',
              'discord_thread_id','webhook_sent_at','delivery_started_at',
              'delivery_completed_at','retry_count','error_message','retry_data',
              'erp_synced','erp_retry_count','created_at','updated_at'):
        v = d.get(k)
        if k in ('retry_data','raw_data') and v:
            try:
                v = json.dumps(json.loads(v), indent=2)[:1500]
            except Exception:
                pass
        print(f'  {{k}} = {{v}}')

print()
print('=== marketplace_status (status_sync) ===')
for oid in ORDERS:
    row = c.execute('SELECT * FROM marketplace_status WHERE order_id=?', (oid,)).fetchone()
    if not row:
        print(f'[{{oid}}] no marketplace_status row')
        continue
    d = dict(row)
    print(f'--- {{oid}} ---')
    for k, v in d.items():
        if k == 'raw_data' and v:
            v = (v[:400] + '...') if len(v) > 400 else v
        print(f'  {{k}} = {{v}}')

print()
print('=== marketplace_disputes ===')
for oid in ORDERS:
    rows = c.execute('SELECT * FROM marketplace_disputes WHERE order_id=?', (oid,)).fetchall()
    if not rows:
        print(f'[{{oid}}] no dispute rows')
        continue
    for r in rows:
        d = dict(r)
        print(f'--- {{oid}} dispute ---')
        for k, v in d.items():
            if k == 'raw_data' and v:
                v = (v[:400] + '...') if len(v) > 400 else v
            print(f'  {{k}} = {{v}}')
c.close()
""".format(orders=ORDERS)

s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.2.220", username="root", password="123456", timeout=15)

sftp = s.open_sftp()
with sftp.open("/tmp/_diag_missing_ev.py", "w") as f:
    f.write(REMOTE_INSPECT)
sftp.close()

print("=" * 70)
print("DB INSPECTION")
print("=" * 70)
_, o, e = s.exec_command("/opt/BotPasteDon/venv/bin/python /tmp/_diag_missing_ev.py")
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err:
    print("STDERR:", err[:2000])

print()
print("=" * 70)
print("LOG GREP (last 40 lines for each order in worker/scanner/coordinator logs)")
print("=" * 70)
for oid in ORDERS:
    print()
    print(f"--- searching {oid} ---")
    cmd = (
        "ls -1 /tmp/g2g_worker*.log /tmp/eldo_worker*.log /tmp/g2g_scanner*.log "
        "/tmp/eldo_scanner*.log /tmp/coordinator*.log /tmp/status_sync*.log 2>/dev/null | "
        f"xargs grep -l '{oid}' 2>/dev/null"
    )
    _, o, _ = s.exec_command(cmd)
    files = [ln.strip() for ln in o.read().decode().splitlines() if ln.strip()]
    if not files:
        print(f"  no hits in any /tmp/*.log")
        continue
    for fpath in files:
        print(f"  >>> {fpath}")
        _, o2, _ = s.exec_command(f"grep -n '{oid}' {fpath} | tail -60")
        print(o2.read().decode(errors="replace"))

print()
print("=" * 70)
print("WORKER LOG: last 40 lines around any FAILED/ERROR mentioning order id")
print("=" * 70)
for oid in ORDERS:
    print(f"\n--- {oid} context ---")
    cmd = (
        "for f in /tmp/g2g_worker*.log /tmp/eldo_worker*.log /tmp/coordinator*.log; do "
        "  [ -f \"$f\" ] || continue; "
        f"  hits=$(grep -n '{oid}' \"$f\" | head -5); "
        "  if [ -n \"$hits\" ]; then "
        "    echo \"=== $f ===\"; "
        f"    grep -n -B2 -A20 '{oid}' \"$f\" | tail -120; "
        "  fi; "
        "done"
    )
    _, o, _ = s.exec_command(cmd)
    print(o.read().decode(errors="replace"))

s.close()
