"""Deploy GMT+7 + date logging to 220 + full restart via start.sh.

Touches shared/logging_config.py which is imported by every service, so a
full restart is required. start.sh already handles cleanup + ordered
start of all 8 services (auth → workers → coordinator → scanners →
watchdog → dashboard). Also wipes /tmp/dashboard.log so the stale
crash traceback doesn't pollute the new log.
"""
import hashlib
import sys
import time
from pathlib import Path

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.2.220", username="root", password="123456", timeout=15)


def run(cmd, timeout=60):
    _, stdout, stderr = s.exec_command(cmd, timeout=timeout)
    return (stdout.read().decode(errors="replace").strip(),
            stderr.read().decode(errors="replace").strip())


LOCAL = Path(r"d:\Code Bot\BotPasteDon\shared\logging_config.py")
REMOTE = "/opt/BotPasteDon/shared/logging_config.py"
expected = hashlib.md5(LOCAL.read_bytes()).hexdigest()

print("===== Upload =====")
ts = time.strftime("%Y%m%d_%H%M%S")
run(f"cp -p {REMOTE} {REMOTE}.bak.{ts} 2>/dev/null; true")
sftp = s.open_sftp()
sftp.put(str(LOCAL), REMOTE)
sftp.close()
got, _ = run(f"md5sum {REMOTE} | awk '{{print $1}}'")
print(f"  remote md5: {got}")
print(f"  local  md5: {expected}")
assert got == expected, "md5 mismatch — aborting"
print("  OK")

print("\n===== Clean old logs =====")
# Wipe (don't delete) so file descriptors stay valid for any straggler
# writing into it, while the new processes get a fresh slate.
for f in ("dashboard.log", "auth6.log", "g2g_worker.log", "eldo_worker.log",
          "coordinator.log", "g2g_scanner.log", "eldo_scanner.log", "watchdog.log"):
    run(f": > /tmp/{f}")
print("  /tmp/*.log truncated")

print("\n===== Run start.sh (full cleanup + restart) =====")
# start.sh uses `set -e` and runs ~60s with the auth health-wait loop.
# Capture full stdout.
out, err = run("cd /opt/BotPasteDon && bash scripts/start.sh 2>&1", timeout=180)
print(out)
if err:
    print("STDERR:", err[:500])

print("\n===== Wait 25s for services to settle =====")
time.sleep(25)

print("\n===== Verify health =====")
out, _ = run("curl -s http://localhost:8010/health | python3 -m json.tool")
print(out)

print("\n===== Sample fresh logs (verify GMT+7 + date format) =====")
for f in ("auth6.log", "g2g_worker.log", "coordinator.log", "dashboard.log"):
    out, _ = run(f"tail -5 /tmp/{f}")
    print(f"--- /tmp/{f} ---")
    print(out or "(empty)")
    print()

print("===== Process audit =====")
out, _ = run(
    "for p in 'auth.main' 'workers.g2g_worker' 'workers.eldorado_worker' "
    "'coordinator.main' 'scanners.main --platform g2g' "
    "'scanners.main --platform eldorado' 'dashboard.server' 'watchdog.py'; do "
    "  count=$(pgrep -af \"$p\" | grep -v 'bash -c' | wc -l); "
    "  echo \"  $p: $count\"; "
    "done"
)
print(out)

s.close()
