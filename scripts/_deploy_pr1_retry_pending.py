"""Deploy PR1 (retry_pending) to 192.168.2.220.

Upload shared/* + workers/g2g_worker.py, stop watchdog, restart:
  - g2g_worker         (new RETRY_PENDING handling + recovery loop)
  - g2g_scanner        (uses cleanup_old_orders body that changed)
  - eldo_scanner       (uses cleanup_old_orders body that changed)
Restart watchdog last. Verify health.

Other services (auth, coordinator, status_sync, dashboard) keep running —
they don't call cleanup_old_orders and don't use ORDER_RETRY_PENDING yet,
so the new shared/ files take effect on their next routine restart.
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
    ("shared/constants.py",   "/opt/BotPasteDon/shared/constants.py"),
    ("shared/order_state.py", "/opt/BotPasteDon/shared/order_state.py"),
    ("shared/database.py",    "/opt/BotPasteDon/shared/database.py"),
    ("workers/g2g_worker.py", "/opt/BotPasteDon/workers/g2g_worker.py"),
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

# ── 2. Stop watchdog ──────────────────────────────────────────────────────────
print("\n===== Stop watchdog =====")
run("pgrep -f 'watchdog.py' | xargs -r kill -9")
time.sleep(2)
out, _ = run("pgrep -af 'watchdog.py'")
print(f"  watchdog leftover: {out or '(none)'}")

# ── 3. Kill g2g_worker + scanners ─────────────────────────────────────────────
print("\n===== Kill g2g_worker + scanners =====")
for pat in ("workers.g2g_worker", "scanners.main.*g2g", "scanners.main.*eldo"):
    run(f"pgrep -af '{pat}' | grep -v 'bash -c' | awk '{{print $1}}' | xargs -r kill -9")
time.sleep(3)
for pat in ("workers.g2g_worker", "scanners.main"):
    pids = py_pids(pat)
    print(f"  after kill, {pat!r}: {[p[0] for p in pids] or '(empty)'}")

# ── 4. Restart services ───────────────────────────────────────────────────────
print("\n===== Restart g2g_worker + scanners =====")
fire_and_forget(
    "cd /opt/BotPasteDon && nohup venv/bin/python -u -m workers.g2g_worker "
    "</dev/null >/tmp/g2g_worker.log 2>&1 & disown"
)
time.sleep(2)
fire_and_forget(
    "cd /opt/BotPasteDon && nohup venv/bin/python -u -m scanners.main --platform g2g "
    "</dev/null >/tmp/g2g_scanner.log 2>&1 & disown"
)
fire_and_forget(
    "cd /opt/BotPasteDon && nohup venv/bin/python -u -m scanners.main --platform eldorado "
    "</dev/null >/tmp/eldo_scanner.log 2>&1 & disown"
)
time.sleep(5)

# ── 5. Restart watchdog ───────────────────────────────────────────────────────
print("\n===== Restart watchdog =====")
fire_and_forget(
    "cd /opt/BotPasteDon && nohup venv/bin/python scripts/watchdog.py "
    "</dev/null >/tmp/watchdog.log 2>&1 & disown"
)
time.sleep(3)

# ── 6. Verify ─────────────────────────────────────────────────────────────────
print("\n===== Verify =====")
for pat in ("workers.g2g_worker", "scanners.main.*g2g", "scanners.main.*eldo", "watchdog.py"):
    pids = py_pids(pat)
    print(f"  {pat!r}: {[p[0] for p in pids] or 'DOWN'}")

# Reachability + log preview
out, _ = run("curl -s -o /dev/null -w '%{http_code}' http://localhost:8002/health")
print(f"  g2g_worker /health -> HTTP {out}")

print("\n===== g2g_worker last 25 log lines =====")
out, _ = run("tail -25 /tmp/g2g_worker.log")
print(out or "(empty)")

print("\n===== g2g_scanner last 10 log lines =====")
out, _ = run("tail -10 /tmp/g2g_scanner.log")
print(out or "(empty)")

ssh.close()
print("\nDone.")
