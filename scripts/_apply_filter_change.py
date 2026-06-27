"""Apply new scanner filter config to bot prod (.env) + restart scanners.

Decision (owner 2026-06-27): INVERT keyword filter.
  - Before: whitelist (allow only currency/gold/gems/...) + blacklist (Boosting/.../Any Items).
    Effect: gear orders (Mageblood, Headhunter...) were dropped as DETECTED, never pasted to ERP.
  - After:  empty whitelist (allow everything) + blacklist = Any Gears, Any Items - Aspects,
    Boosting, Leveling, Account.
    Effect: only the two "any/bulk gear" listing types + service spam are dropped;
    specific gear (Mageblood...) gets pasted to ERP for hand delivery.

`.env` is gitignored runtime state — editing it on the server is the correct path
(deploy_git.py git reset never touches it). Backups go to .env.bak-<timestamp>.

Restart is watchdog-safe (same recipe as deploy_git.py): stop watchdog -> kill+relaunch
scanners -> restart watchdog.
"""
import sys
import time
import paramiko
from datetime import datetime, timezone

HOST = "192.168.2.220"
USER = "root"
PWD = "123456"
BASE = "/opt/BotPasteDon"

NEW_WHITELIST = ""  # empty -> allow-all (check_keywords skips whitelist block)
NEW_BLACKLIST = "Any Gears, Any Items - Aspects, Boosting, Leveling, Account, Custom oder"

# service -> (pgrep pattern, start command)
SCANNERS = {
    "scanner_g2g": (
        "scanners.main --platform g2g",
        "nohup venv/bin/python -u -m scanners.main --platform g2g > /tmp/g2g_scanner.log 2>&1 &",
    ),
    "scanner_eldo": (
        "scanners.main --platform eldorado",
        "nohup venv/bin/python -u -m scanners.main --platform eldorado > /tmp/eldo_scanner.log 2>&1 &",
    ),
}


def main():
    dry = "--dry-run" in sys.argv
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PWD, allow_agent=False, look_for_keys=False, timeout=15)

    def run(cmd, t=60):
        _, o, e = ssh.exec_command(f"cd {BASE} && {cmd}", timeout=t)
        o.channel.recv_exit_status()
        return (o.read().decode(errors="replace") + e.read().decode(errors="replace")).rstrip()

    def launch(cmd):
        ch = ssh.get_transport().open_session()
        ch.exec_command(f"cd {BASE} && {cmd}")
        ch.recv_exit_status()
        ch.close()

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    # 1. Show BEFORE
    print("=== BEFORE (.env scanner filter) ===")
    print(run("grep -E 'SCANNER_(WHITE|BLACK)LIST' .env || echo '(none set)'"))

    if dry:
        print("\n[DRY-RUN] Would write:")
        print(f"  SCANNER_WHITELIST={NEW_WHITELIST or '(empty)'}")
        print(f"  SCANNER_BLACKLIST={NEW_BLACKLIST}")
        print("[DRY-RUN] Would restart: scanner_g2g, scanner_eldo (watchdog-safe)")
        ssh.close()
        return

    # 2. Backup + edit .env on the server with sed (in-place, idempotent).
    print(f"\n>> backup .env -> .env.bak-{ts}")
    print(run(f"cp -a .env .env.bak-{ts}"))

    # Rewrite the two lines. sed -i replaces matching lines; if missing, append.
    # Using | as regex delimiter to avoid clashing with values.
    for var, val in (("SCANNER_WHITELIST", NEW_WHITELIST), ("SCANNER_BLACKLIST", NEW_BLACKLIST)):
        # Escape & and | in replacement value for sed (only & is special in replacement)
        esc = val.replace("&", "\\&")
        # Delete existing line(s) for this var, then append the new one.
        run(f"sed -i '/^{var}=/d' .env")
        run(f"printf '%s\\n' '{var}={esc}' >> .env")

    # 3. Show AFTER
    print("\n=== AFTER (.env scanner filter) ===")
    print(run("grep -E 'SCANNER_(WHITE|BLACK)LIST' .env"))

    # 4. Restart scanners (watchdog-safe)
    print("\n>> stop watchdog")
    run("systemctl stop bot-watchdog.service 2>/dev/null; "
        "pgrep -f 'scripts/watchdog.py' | xargs -r kill -9")
    for name, (pattern, start) in SCANNERS.items():
        print(f">> restart {name}")
        run(f"pgrep -f '{pattern}' | xargs -r kill -9")
        time.sleep(2)
        launch(start)
        time.sleep(3)
    print(">> restart watchdog")
    launch("systemctl start bot-watchdog.service 2>/dev/null || "
           "nohup venv/bin/python scripts/watchdog.py > /tmp/watchdog.log 2>&1 &")
    time.sleep(2)

    # 5. Confirm scanners up
    print("\n=== scanners running ===")
    print(run("pgrep -af 'scanners.main' | grep -v 'bash -c' | grep -v grep"))

    ssh.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
