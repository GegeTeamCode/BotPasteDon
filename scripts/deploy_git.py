"""Git-based deploy for BotPasteDon.

The server `/opt/BotPasteDon` is a git checkout of `origin/main`. This script
makes the server match `origin/main` (the single source of truth) and restarts
the services you name. Runtime state (`.env`, `data/`, `chrome_profile_*`,
`venv/`) is gitignored and never touched by the reset.

Workflow:
    1. Edit code locally, `git commit`, `git push origin main`.
    2. Run this script to pull + restart the affected services.

NEVER edit code directly on the server — that recreates the drift this replaces.
If you must hotfix on the server, commit it back to the repo immediately
(`git -C /opt/BotPasteDon diff` shows any drift; this script aborts on drift).

Usage:
    python scripts/deploy_git.py                      # sync code only, no restart
    python scripts/deploy_git.py scanner_g2g          # sync + restart one service
    python scripts/deploy_git.py worker_g2g worker_eldo
    python scripts/deploy_git.py all                  # sync + restart everything

Services: auth, scanner_g2g, scanner_eldo, worker_g2g, worker_eldo,
          coordinator, dashboard
"""
import sys
import time
import paramiko

HOST = "192.168.2.220"
USER = "root"
PASSWORD = "123456"
BASE = "/opt/BotPasteDon"

# name -> (pgrep pattern, start command run from BASE)
SERVICES = {
    "auth": (
        "auth.main",
        "HEADLESS_MODE=true nohup venv/bin/python -u -m auth.main > /tmp/auth.log 2>&1 &",
    ),
    "scanner_g2g": (
        "scanners.main --platform g2g",
        "nohup venv/bin/python -u -m scanners.main --platform g2g > /tmp/g2g_scanner.log 2>&1 &",
    ),
    "scanner_eldo": (
        "scanners.main --platform eldorado",
        "nohup venv/bin/python -u -m scanners.main --platform eldorado > /tmp/eldo_scanner.log 2>&1 &",
    ),
    "worker_g2g": (
        "workers.g2g_worker",
        "nohup venv/bin/python -u -m workers.g2g_worker > /tmp/g2g_worker.log 2>&1 &",
    ),
    "worker_eldo": (
        "workers.eldorado_worker",
        "nohup venv/bin/python -u -m workers.eldorado_worker > /tmp/eldo_worker.log 2>&1 &",
    ),
    "coordinator": (
        "coordinator.main",
        "nohup venv/bin/python -u -m coordinator.main > /tmp/coordinator.log 2>&1 &",
    ),
    "dashboard": (
        "dashboard.server",
        "nohup venv/bin/python -u -m dashboard.server > /tmp/dashboard.log 2>&1 &",
    ),
    "status_sync": (
        # No leading '-': pgrep -f treats a pattern starting with '-' as a flag
        # and fails to match, leaving the old process alive (duplicate-spawn).
        "python -u -m status_sync",
        "nohup venv/bin/python -u -m status_sync > /tmp/status_sync.log 2>&1 &",
    ),
}
# Restart order for "all" (dependency-aware: auth -> workers -> coordinator -> scanners -> dashboard)
ALL_ORDER = ["auth", "worker_g2g", "worker_eldo", "coordinator",
             "scanner_g2g", "scanner_eldo", "dashboard", "status_sync"]


def main():
    args = [a for a in sys.argv[1:]]
    if "all" in args:
        targets = list(ALL_ORDER)
    else:
        targets = args
    bad = [t for t in targets if t not in SERVICES]
    if bad:
        sys.exit(f"Unknown service(s): {bad}\nValid: {', '.join(SERVICES)} (or 'all')")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=15)

    def run(cmd, t=90):
        _, o, e = ssh.exec_command(f"cd {BASE} && {cmd}", timeout=t)
        o.channel.recv_exit_status()
        return (o.read().decode(errors="replace") + e.read().decode(errors="replace")).rstrip()

    def launch(cmd):
        ch = ssh.get_transport().open_session()
        ch.exec_command(f"cd {BASE} && {cmd}")
        ch.recv_exit_status()
        ch.close()

    # 1. Abort if the server has uncommitted drift (someone edited on the server)
    drift = run("git status --short | grep -vE '^\\?\\?' || true")
    if drift.strip():
        ssh.close()
        sys.exit("ABORT: server has uncommitted tracked drift — commit it to the repo "
                 "first, do not lose it:\n" + drift)

    # 2. Sync to origin/main
    before = run("git rev-parse HEAD")
    print(run("git fetch -q origin && git reset --hard origin/main 2>&1 | tail -1"))
    after = run("git rev-parse HEAD")
    if before == after:
        print(f"Already up to date at {after[:8]} (no code change).")
    else:
        print(f"Deployed {before[:8]} -> {after[:8]}")
        print("Changed files:")
        print(run(f"git diff --name-only {before} {after}"))

    if not targets:
        print("\nNo services named — code synced only. Restart manually if needed.")
        ssh.close()
        return

    # 3. Restart: stop watchdog -> restart each target -> restart watchdog.
    # Watchdog is owned by systemd (bot-watchdog.service, Restart=always); stop
    # the unit so it stays down during the restart instead of resurrecting and
    # respawning the services we are bouncing. pkill is the fallback for a box
    # without the unit installed.
    print("\n>> stop watchdog")
    run("systemctl stop bot-watchdog.service 2>/dev/null; "
        "pgrep -f 'scripts/watchdog.py' | xargs -r kill -9")
    for name in targets:
        pattern, start = SERVICES[name]
        print(f">> restart {name}")
        run(f"pgrep -f '{pattern}' | xargs -r kill -9")
        time.sleep(2)
        launch(start)
        time.sleep(3 if name != "auth" else 25)  # auth needs time for initial capture
    print(">> restart watchdog")
    # systemctl start is idempotent; fall back to nohup if the unit isn't installed.
    launch("systemctl start bot-watchdog.service 2>/dev/null || "
           "nohup venv/bin/python scripts/watchdog.py > /tmp/watchdog.log 2>&1 &")
    time.sleep(2)

    # 4. Report
    print("\n=== running services ===")
    print(run("pgrep -af 'auth.main|scanners.main|workers.|coordinator.main|"
              "dashboard.server|watchdog.py' | grep -v 'bash -c' | grep -v grep"))
    ssh.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
