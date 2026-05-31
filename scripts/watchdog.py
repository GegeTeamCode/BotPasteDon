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
