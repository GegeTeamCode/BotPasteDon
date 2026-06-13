"""Verify all bot processes: count only the python instance, not bash launcher."""
import paramiko
import re

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("192.168.2.220", username="root", password="123456", timeout=15)

def run(cmd, timeout=30):
    _, stdout, _ = ssh.exec_command(cmd, timeout=timeout)
    return stdout.read().decode(errors="replace").strip()

# Expected: pattern -> (name, port_or_None)
# Pattern matched against full cmdline; we then keep only entries whose
# argv[0] is python (filtering out 'bash -c "...python..."' launcher shells).
EXPECTED = [
    ("auth.main",                          "Auth Service",        8010),
    ("scanners.main.*eldorado",            "Eldo Scanner",        None),
    ("scanners.main.*g2g",                 "G2G Scanner",         None),
    ("workers.eldorado_worker",            "Eldo Worker",         8001),
    ("workers.g2g_worker",                 "G2G Worker",          8002),
    ("coordinator.main",                   "Coordinator",         8030),
    ("status_sync",                        "Status Sync",         None),
    ("dashboard.server",                   "Dashboard",           8766),
    ("scripts/watchdog.py",                "Watchdog",            None),
]

def py_pids(pattern: str):
    """Return list of (pid, cmdline) for python (or nohup-wrapped python) processes matching pattern.
    Drops bash launcher wrappers and the pgrep itself."""
    raw = run(f"pgrep -af '{pattern}'")
    pids = []
    for line in raw.splitlines():
        m = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not m:
            continue
        pid, cmd = int(m.group(1)), m.group(2)
        if "pgrep" in cmd:
            continue
        # Drop bash launcher shells: they start with `bash -c`
        if re.match(r"^/bin/bash\s+-c|^bash\s+-c", cmd):
            continue
        pids.append((pid, cmd))
    return pids

print("=" * 76)
print(f"{'Service':<20} {'PIDs':<14} {'Port':<6} {'Heartbeat (HH:MM:SS)':<22} {'Status'}")
print("=" * 76)

# Heartbeat lookup - write a small python script via stdin to avoid quoting issues
hb_script = (
    "import sqlite3\n"
    "conn=sqlite3.connect('/opt/BotPasteDon/data/orders.db')\n"
    "for r in conn.execute('SELECT service_name, pid, last_beat FROM heartbeat').fetchall():\n"
    "    print(f'{r[0]}|{r[1]}|{r[2]}')\n"
)
sftp = ssh.open_sftp()
with sftp.open("/tmp/_hb_check.py", "w") as f:
    f.write(hb_script)
sftp.close()
hb_raw = run("/opt/BotPasteDon/venv/bin/python /tmp/_hb_check.py")
hb_map = {}
for line in hb_raw.splitlines():
    parts = line.split("|")
    if len(parts) == 3:
        hb_map[parts[0]] = (parts[1], parts[2])

# Heartbeat service-id mapping
HB_KEY = {
    "Auth Service":   "auth_service",
    "Eldo Scanner":   "scanner_eldorado",
    "G2G Scanner":    "scanner_g2g",
    "Eldo Worker":    "worker_eldo",
    "G2G Worker":     "worker_g2g",
    "Coordinator":    "coordinator",
    "Dashboard":      "dashboard",
}

problems = []
for pattern, name, port in EXPECTED:
    pids = py_pids(pattern)
    actual = len(pids)

    port_ok = None
    if port:
        ss = run(f"ss -tlnp 2>/dev/null | grep ':{port} '")
        port_ok = bool(ss)

    hb_pid, hb_time = hb_map.get(HB_KEY.get(name, ""), ("-", "-"))
    hb_display = hb_time.split()[1] if " " in hb_time else hb_time

    if actual == 1 and (port is None or port_ok):
        status = "OK"
    elif actual == 0:
        status = "DOWN"
        problems.append((name, "DOWN", []))
    elif actual > 1:
        status = f"DUP x{actual}"
        problems.append((name, f"duplicate ({actual} python instances)", pids))
    elif port and not port_ok:
        status = "NO-PORT"
        problems.append((name, f"port {port} not listening", pids))
    else:
        status = "?"

    pid_str = ",".join(str(p[0]) for p in pids) or "-"
    print(f"{name:<20} {pid_str:<14} {str(port or '-'):<6} {hb_display:<22} {status}")

print("=" * 76)

# Auth health
print("\nAuth /health:")
print(run("curl -s -m 5 http://localhost:8010/health | python3 -m json.tool") or "  (no response)")

# Browser orphan summary
print("\nBrowser processes (expected only if auth recently launched):")
cnt = run("ps aux | grep -E 'camoufox-bin|chromedriver|chrome --|chrome_profile_' | grep -v grep | wc -l")
print(f"  Total chrome/camoufox processes: {cnt}")
cam = run("pgrep -af camoufox-bin | head -3")
print(f"  Camoufox (top 3):\n{cam or '  none'}")

# Final verdict
print("\n" + "=" * 76)
if problems:
    print("PROBLEMS DETECTED:")
    for name, issue, pids in problems:
        print(f"  - {name}: {issue}")
        for pid, line in pids:
            print(f"      PID {pid}: {line[:100]}")
else:
    print("ALL OK — all 8 services up (1 python instance each), ports listening.")

ssh.close()
