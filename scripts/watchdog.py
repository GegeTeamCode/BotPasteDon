"""Watchdog — monitors heartbeat table and auto-restarts stale services.

Usage:
    cd /opt/BotPasteDon
    nohup venv/bin/python scripts/watchdog.py > /tmp/watchdog.log 2>&1 &
"""

import os
import sys
import time
import signal
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from shared.database import Database
from shared.config import DATABASE_PATH
from shared.logging_config import setup_logger

logger = setup_logger("watchdog")

STALE_THRESHOLD = 90
CHECK_INTERVAL = 30
STARTUP_DELAY = 5

SERVICE_REGISTRY = [
    {
        "name": "auth_service",
        "cmd": "venv/bin/python -u -m auth.main",
        "log": "/tmp/auth6.log",
        "env": {"HEADLESS_MODE": "true"},
        "tier": 0,
    },
    {
        "name": "worker_eldo",
        "cmd": "venv/bin/python -u -m workers.eldorado_worker",
        "log": "/tmp/eldo_worker.log",
        "env": {"HEADLESS_MODE": "true"},
        "tier": 1,
    },
    {
        "name": "worker_g2g",
        "cmd": "venv/bin/python -u -m workers.g2g_worker",
        "log": "/tmp/g2g_worker.log",
        "env": {"HEADLESS_MODE": "true"},
        "tier": 1,
    },
    {
        "name": "coordinator",
        "cmd": "venv/bin/python -u -m coordinator.main",
        "log": "/tmp/coordinator.log",
        "env": {},
        "tier": 2,
    },
    {
        "name": "scanner_eldorado",
        "cmd": "venv/bin/python -u -m scanners.main --platform eldorado",
        "log": "/tmp/eldo_scanner.log",
        "env": {"HEADLESS_MODE": "true"},
        "tier": 3,
    },
    {
        "name": "scanner_g2g",
        "cmd": "venv/bin/python -u -m scanners.main --platform g2g",
        "log": "/tmp/g2g_scanner.log",
        "env": {"HEADLESS_MODE": "true"},
        "tier": 3,
    },
    {
        "name": "status_sync",
        "cmd": "venv/bin/python -u -m status_sync",
        "log": "/tmp/status_sync.log",
        "env": {},
        "tier": 3,
    },
    {
        "name": "dashboard",
        "cmd": "venv/bin/python -u -m dashboard.server",
        "log": "/tmp/dashboard.log",
        "env": {},
        "tier": 3,
    },
]

NAME_MAP = {s["name"]: s for s in SERVICE_REGISTRY}

_running = True


def handle_signal(sig, frame):
    global _running
    logger.info("Watchdog shutting down (signal %d)", sig)
    _running = False


def kill_process(pid: int, name: str):
    if not pid or pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        logger.info("Sent SIGTERM to %s (pid %d)", name, pid)
        time.sleep(3)
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
            logger.warning("Force-killed %s (pid %d)", name, pid)
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        logger.info("Process %s (pid %d) already gone", name, pid)
    except PermissionError:
        logger.error("No permission to kill %s (pid %d)", name, pid)


def find_running_pids(svc: dict) -> list:
    """Return list of live PIDs already running this service's command.

    Uses pgrep on the service's module path so it matches processes launched
    by either watchdog OR start.sh/systemd. Filters out bash launchers and the
    pgrep itself. This is the guard that prevents duplicate-spawn after a reset
    where start.sh and watchdog both boot services at the same time.
    """
    # Extract a stable match token from the cmd, e.g. "auth.main",
    # "workers.eldorado_worker", "status_sync.main".
    parts = svc["cmd"].split()
    token = parts[-1] if parts else ""
    # For scanners the platform flag is the last token; use the module instead.
    if "--platform" in svc["cmd"]:
        token = next((p for p in parts if p.startswith("-m") is False
                      and "." in p), token)
        token = "scanners.main"
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", token], stderr=subprocess.DEVNULL
        ).decode(errors="replace")
    except subprocess.CalledProcessError:
        return []  # pgrep exits 1 when no match
    pids = []
    my_pid = os.getpid()
    for line in out.splitlines():
        m = line.strip().split()
        if not m:
            continue
        pid = int(m[0])
        if pid == my_pid:
            continue
        cmdline = subprocess.check_output(
            ["ps", "-o", "args=", "-p", str(pid)], stderr=subprocess.DEVNULL
        ).decode(errors="replace")
        # Drop bash launcher wrappers and the pgrep self-match.
        if cmdline.startswith("bash -c") or cmdline.startswith("/bin/bash -c"):
            continue
        if "pgrep" in cmdline:
            continue
        pids.append(pid)
    return pids


def start_service(svc: dict) -> int:
    env = {**os.environ, **svc.get("env", {})}
    log_f = open(svc["log"], "a")
    proc = subprocess.Popen(
        svc["cmd"].split(),
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    return proc.pid


def restart_service(svc: dict, old_pid: int):
    name = svc["name"]
    kill_process(old_pid, name)
    time.sleep(1)
    # GUARD: before starting a new instance, check if one is already alive.
    # After a server reset, start.sh/systemd may have launched the service
    # while the heartbeat table still held the OLD (dead) PID — watchdog would
    # otherwise blindly spawn a duplicate. See operations.md "watchdog respawn
    # trap" and the 2026-06-13 duplicate-scanner incident.
    live = find_running_pids(svc)
    if live:
        logger.info("SKIP restart %s — already running pid=%s (heartbeat pid=%d was stale)",
                    name, live, old_pid)
        return
    new_pid = start_service(svc)
    logger.info("RESTARTED %s (old_pid=%d -> new_pid=%d)", name, old_pid, new_pid)


def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    db = Database(DATABASE_PATH)
    logger.info("Watchdog started (PID: %d, threshold: %ds, interval: %ds)",
                os.getpid(), STALE_THRESHOLD, CHECK_INTERVAL)

    while _running:
        try:
            stale = db.get_stale_services(STALE_THRESHOLD)
        except Exception as e:
            logger.error("DB query failed: %s", e)
            stale = []

        if stale:
            by_tier = {}
            for row in stale:
                name = row["service_name"]
                svc = NAME_MAP.get(name)
                if not svc:
                    logger.warning("Unknown stale service: %s", name)
                    continue
                by_tier.setdefault(svc["tier"], []).append((svc, row))

            for tier in sorted(by_tier.keys()):
                for svc, row in by_tier[tier]:
                    restart_service(svc, row.get("pid") or 0)
                if any(t > tier for t in by_tier.keys()):
                    logger.info("Waiting %ds before next tier...", STARTUP_DELAY)
                    time.sleep(STARTUP_DELAY)

        for _ in range(CHECK_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    logger.info("Watchdog stopped")


if __name__ == "__main__":
    main()
