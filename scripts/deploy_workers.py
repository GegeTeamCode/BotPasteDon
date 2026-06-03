"""Deploy worker patches: upload -> stop watchdog -> kill workers ->
start workers -> restart watchdog -> verify no duplicate + health."""
import paramiko
import re
import time
from pathlib import Path

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("192.168.2.220", username="root", password="123456", timeout=15)

def run(cmd, timeout=30):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    return stdout.read().decode(errors="replace").strip(), stderr.read().decode(errors="replace").strip()

def fire_and_forget(cmd, timeout=10):
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

# 1. Upload + verify md5
LOCAL = Path(r"d:\Code Bot\BotPasteDon")
files = [
    ("workers/eldorado_worker.py", "/opt/BotPasteDon/workers/eldorado_worker.py"),
    ("workers/g2g_worker.py",       "/opt/BotPasteDon/workers/g2g_worker.py"),
]

print("===== Upload + backup =====")
sftp = ssh.open_sftp()
for rel, remote in files:
    run(f"cp {remote} {remote}.bak-$(date +%s)")
    sftp.put(str(LOCAL / rel), remote)
    out, _ = run(f"md5sum {remote}")
    print(f"  {out}")
sftp.close()

# 2. Compile check
print("\n===== Compile check =====")
for _, remote in files:
    out, err = run(f"/opt/BotPasteDon/venv/bin/python -c \"import py_compile; py_compile.compile('{remote}', doraise=True); print('OK {0}'.format('{remote}'.rsplit('/',1)[-1]))\"")
    print(f"  {out or err}")

# 3. Stop watchdog
print("\n===== Stop watchdog =====")
run("pgrep -f 'watchdog.py' | xargs -r kill -9 2>/dev/null")
time.sleep(2)
out, _ = run("pgrep -af watchdog.py | grep -v pgrep")
print(out or "  (watchdog stopped)")

# 4. Kill workers (separate pgrep calls, no self-match)
print("\n===== Kill workers =====")
run("pgrep -f 'workers.eldorado_worker' | xargs -r kill -9 2>/dev/null", timeout=10)
run("pgrep -f 'workers.g2g_worker'      | xargs -r kill -9 2>/dev/null", timeout=10)
time.sleep(3)
out_eldo = py_pids("workers.eldorado_worker")
out_g2g = py_pids("workers.g2g_worker")
print(f"  Eldo worker pids: {out_eldo or '[]'}")
print(f"  G2G worker pids:  {out_g2g or '[]'}")

# 5. Verify ports freed
print("\n===== Verify ports 8001 + 8002 free =====")
out, _ = run("ss -tlnp 2>/dev/null | grep -E ':(8001|8002) ' || echo 'both ports free'")
print(f"  {out}")

# 6. Pick log files
out, _ = run("ls /tmp/eldo_worker*.log 2>/dev/null | sort")
nums = []
for path in [l.strip() for l in out.splitlines() if l.strip()]:
    digits = "".join(c for c in path.rsplit("/", 1)[-1] if c.isdigit())
    if digits:
        nums.append(int(digits))
eldo_log = f"/tmp/eldo_worker{max(nums) + 1 if nums else 1}.log"

out, _ = run("ls /tmp/g2g_worker*.log 2>/dev/null | sort")
nums = []
for path in [l.strip() for l in out.splitlines() if l.strip()]:
    digits = "".join(c for c in path.rsplit("/", 1)[-1] if c.isdigit())
    if digits:
        nums.append(int(digits))
g2g_log = f"/tmp/g2g_worker{max(nums) + 1 if nums else 1}.log"
print(f"\n  Eldo log -> {eldo_log}")
print(f"  G2G log  -> {g2g_log}")

# 7. Start workers
print("\n===== Start patched workers =====")
fire_and_forget(
    f"cd /opt/BotPasteDon && setsid venv/bin/python -u -m workers.eldorado_worker "
    f"</dev/null >{eldo_log} 2>&1 & disown",
    timeout=8,
)
fire_and_forget(
    f"cd /opt/BotPasteDon && setsid venv/bin/python -u -m workers.g2g_worker "
    f"</dev/null >{g2g_log} 2>&1 & disown",
    timeout=8,
)
time.sleep(5)

eldo = py_pids("workers.eldorado_worker")
g2g  = py_pids("workers.g2g_worker")
print(f"  Eldo worker pids: {eldo}")
print(f"  G2G worker pids:  {g2g}")

# 8. Wait for HTTP ready + check log for errors
print("\n===== Wait 15s, check log + port =====")
time.sleep(15)

print(f"\n  Eldo worker log ({eldo_log}) tail:")
out, _ = run(f"tail -n 25 {eldo_log}")
print("  " + out.replace("\n", "\n  "))

print(f"\n  G2G worker log ({g2g_log}) tail:")
out, _ = run(f"tail -n 25 {g2g_log}")
print("  " + out.replace("\n", "\n  "))

out, _ = run("ss -tlnp 2>/dev/null | grep -E ':(8001|8002) '")
print(f"\n  Ports listening: {out or '(NONE!)'}")

# 9. Restart watchdog
print("\n===== Restart watchdog =====")
fire_and_forget(
    "cd /opt/BotPasteDon && setsid venv/bin/python scripts/watchdog.py "
    "</dev/null >/tmp/watchdog.log 2>&1 & disown",
    timeout=8,
)
time.sleep(3)
out = py_pids("scripts/watchdog.py")
print(f"  watchdog pids: {out}")

# 10. Run full process check
print("\n===== Final process audit =====")
sftp = ssh.open_sftp()
with sftp.open("/tmp/_audit.py", "w") as f:
    f.write("""
import sqlite3, re, subprocess, json

EXPECTED = [
    ("auth.main",                          "Auth Service",        8010),
    ("scanners.main.*eldorado",            "Eldo Scanner",        None),
    ("scanners.main.*g2g",                 "G2G Scanner",         None),
    ("workers.eldorado_worker",            "Eldo Worker",         8001),
    ("workers.g2g_worker",                 "G2G Worker",          8002),
    ("coordinator.main",                   "Coordinator",         8030),
    ("dashboard.server",                   "Dashboard",           8766),
    ("scripts/watchdog.py",                "Watchdog",            None),
]

HB_KEY = {
    "Auth Service":   "auth_service",
    "Eldo Scanner":   "scanner_eldorado",
    "G2G Scanner":    "scanner_g2g",
    "Eldo Worker":    "worker_eldo",
    "G2G Worker":     "worker_g2g",
    "Coordinator":    "coordinator",
    "Dashboard":      "dashboard",
}

def py_pids(pattern):
    raw = subprocess.run(["pgrep", "-af", pattern], capture_output=True, text=True).stdout
    out = []
    for line in raw.splitlines():
        m = re.match(r"^\\s*(\\d+)\\s+(.*)$", line)
        if not m: continue
        pid, cmd = int(m.group(1)), m.group(2)
        if "pgrep" in cmd: continue
        if re.match(r"^/bin/bash\\s+-c|^bash\\s+-c", cmd): continue
        out.append(pid)
    return out

conn = sqlite3.connect("/opt/BotPasteDon/data/orders.db")
hb = {r[0]: r[2] for r in conn.execute("SELECT service_name, pid, last_beat FROM heartbeat")}
conn.close()

print(f"{'Service':<18} {'PIDs':<10} {'Port':<6} {'Heartbeat':<22} {'Status'}")
print("-" * 70)

problems = []
for pattern, name, port in EXPECTED:
    pids = py_pids(pattern)
    port_ok = True
    if port:
        ss = subprocess.run(["ss", "-tln"], capture_output=True, text=True).stdout
        port_ok = f":{port} " in ss
    hb_time = hb.get(HB_KEY.get(name, ""), "-")
    hb_disp = hb_time.split()[1] if " " in hb_time else hb_time
    if len(pids) == 1 and port_ok:
        status = "OK"
    elif len(pids) == 0:
        status = "DOWN"; problems.append(name + " DOWN")
    elif len(pids) > 1:
        status = f"DUP x{len(pids)}"; problems.append(name + f" duplicate ({len(pids)})")
    elif not port_ok:
        status = "NO-PORT"; problems.append(name + f" port {port} not listening")
    else:
        status = "?"
    pid_str = ",".join(str(p) for p in pids) or "-"
    print(f"{name:<18} {pid_str:<10} {str(port or '-'):<6} {hb_disp:<22} {status}")

print()
if problems:
    print("PROBLEMS: " + " | ".join(problems))
else:
    print("ALL OK")
""")
sftp.close()

out, _ = run("/opt/BotPasteDon/venv/bin/python /tmp/_audit.py")
print(out)

# 11. Send a dry test? Just check workers respond to /health if they have it
print("\n===== Worker reachability probes =====")
for port, name in [(8001, "eldo"), (8002, "g2g")]:
    out, _ = run(f"curl -sS -m 3 -o /dev/null -w '%{{http_code}}' http://localhost:{port}/ ; echo ' (root)'")
    print(f"  {name} {port}: {out}")

ssh.close()
