"""Deploy PR2 (coordinator dispatch retry queue) to 192.168.2.220.

Uploads:
  - shared/database.py        (adds pending_dispatches table + helpers)
  - coordinator/discord_bot.py (queue on dispatch fail + retry loop)

Restarts:
  - coordinator  (picks up new code + creates the new table on init)

g2g_worker and scanners already have the matching shared/database.py from
PR1, but the new pending_dispatches table is created idempotently — they
do not need a restart unless you want them on the freshest shared copy.
"""
import hashlib
import paramiko
import re
import time
from pathlib import Path

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("192.168.2.220", username="root", password="123456", timeout=15)


def run(cmd, timeout=30):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    return (stdout.read().decode(errors="replace").strip(),
            stderr.read().decode(errors="replace").strip())


def fire_and_forget(cmd, timeout=15):
    chan = ssh.get_transport().open_session()
    chan.settimeout(timeout)
    chan.exec_command(cmd)
    for _ in range(int(timeout * 2)):
        if chan.exit_status_ready():
            break
        time.sleep(0.5)
    chan.close()


def py_pids(pattern: str):
    raw, _ = run(f"pgrep -af '{pattern}'")
    pids = []
    for line in raw.splitlines():
        m = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not m:
            continue
        pid, cmd = int(m.group(1)), m.group(2)
        if "pgrep" in cmd:
            continue
        if re.match(r"^/bin/bash\s+-c|^bash\s+-c", cmd):
            continue
        pids.append((pid, cmd))
    return pids


def md5_local(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


LOCAL = Path(r"d:\Code Bot\BotPasteDon")
FILES = [
    ("shared/database.py",          "/opt/BotPasteDon/shared/database.py"),
    ("coordinator/discord_bot.py",  "/opt/BotPasteDon/coordinator/discord_bot.py"),
]

# ── 1. Upload + verify md5 ────────────────────────────────────────────────────
print("===== Upload + verify md5 =====")
sftp = ssh.open_sftp()
ts = time.strftime("%Y%m%d_%H%M%S")
for rel, remote in FILES:
    local_path = LOCAL / rel
    expected = md5_local(local_path)
    bak = f"{remote}.bak.{ts}"
    run(f"cp -p {remote} {bak} 2>/dev/null; true")
    sftp.put(str(local_path), remote)
    got, _ = run(f"md5sum {remote} | awk '{{print $1}}'")
    ok = (got == expected)
    print(f"  [{'OK' if ok else 'FAIL'}] {rel}  remote={got[:10]} local={expected[:10]}  bak={bak}")
    if not ok:
        sftp.close()
        raise SystemExit("md5 mismatch — aborting deploy")
sftp.close()

# ── 2. Stop watchdog + coordinator ────────────────────────────────────────────
print("\n===== Stop watchdog + coordinator =====")
run("pgrep -f 'watchdog.py' | xargs -r kill -9")
run("pgrep -af 'coordinator.main' | grep -v 'bash -c' | awk '{print $1}' | xargs -r kill -9")
time.sleep(3)
for pat in ("watchdog.py", "coordinator.main"):
    pids = py_pids(pat)
    print(f"  after kill, {pat!r}: {[p[0] for p in pids] or '(empty)'}")

# ── 3. Restart coordinator ────────────────────────────────────────────────────
print("\n===== Restart coordinator =====")
fire_and_forget(
    "cd /opt/BotPasteDon && nohup venv/bin/python -u -m coordinator.main "
    "</dev/null >/tmp/coordinator.log 2>&1 & disown"
)
time.sleep(6)

# ── 4. Restart watchdog ───────────────────────────────────────────────────────
print("\n===== Restart watchdog =====")
fire_and_forget(
    "cd /opt/BotPasteDon && nohup venv/bin/python scripts/watchdog.py "
    "</dev/null >/tmp/watchdog.log 2>&1 & disown"
)
time.sleep(3)

# ── 5. Verify ─────────────────────────────────────────────────────────────────
print("\n===== Verify =====")
for pat in ("coordinator.main", "watchdog.py"):
    pids = py_pids(pat)
    print(f"  {pat!r}: {[p[0] for p in pids] or 'DOWN'}")

# Check pending_dispatches table got created
out, _ = run(
    "cd /opt/BotPasteDon && venv/bin/python -c "
    "\"import sqlite3; "
    "c=sqlite3.connect('data/orders.db'); "
    "rows=c.execute(\\\"SELECT name FROM sqlite_master WHERE type='table' "
    "AND name='pending_dispatches'\\\").fetchall(); "
    "print('table_exists:', bool(rows))\""
)
print(f"  pending_dispatches table: {out}")

# /complete endpoint reachability
out, _ = run("curl -s -o /dev/null -w '%{http_code}' http://localhost:8030/complete -X POST -d '{}' -H 'Content-Type: application/json'")
print(f"  coordinator /complete -> HTTP {out} (expects 200)")

print("\n===== coordinator last 25 log lines =====")
out, _ = run("tail -25 /tmp/coordinator.log")
print(out or "(empty)")

ssh.close()
print("\nDone.")
