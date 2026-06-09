#!/bin/bash
# BotPasteDon — Stop all services gracefully
set -e
cd /opt/BotPasteDon

echo "[$(date +%H:%M:%S)] Stopping BotPasteDon services..."

# Reverse-dependency order: stop watchdog first so it doesn't respawn things
# we are about to kill, then dashboard / scanners / coordinator / workers,
# then auth last (its profile cleanup runs on SIGTERM).
ORDER=(
    "watchdog.py"
    "dashboard.server"
    "status_sync"
    "scanners.main"
    "coordinator.main"
    "workers.g2g_worker"
    "workers.eldorado_worker"
    "auth.main"
)

# Filter out the "bash -c" launcher line — otherwise pkill / kill -9 over SSH
# can self-match (see operations.md "pkill -f self-match trap").
get_pids() {
    pgrep -af "$1" 2>/dev/null \
        | grep -vE '^\s*[0-9]+\s+(/bin/)?bash\s+-c' \
        | awk '{print $1}'
}

# Graceful TERM first.
for SVC in "${ORDER[@]}"; do
    PIDS=$(get_pids "$SVC")
    if [ -n "$PIDS" ]; then
        echo "$PIDS" | xargs -r kill -TERM 2>/dev/null || true
        echo "  TERM $SVC: $(echo "$PIDS" | tr '\n' ' ')"
    fi
done

sleep 3

# Force-kill stragglers.
for SVC in "${ORDER[@]}"; do
    PIDS=$(get_pids "$SVC")
    if [ -n "$PIDS" ]; then
        echo "$PIDS" | xargs -r kill -9 2>/dev/null || true
        echo "  KILL $SVC: $(echo "$PIDS" | tr '\n' ' ')"
    fi
done

# Chrome / Camoufox children.
pkill -9 -f chromedriver 2>/dev/null || true
pkill -9 -f camoufox-bin 2>/dev/null || true

# Profile locks for all 4 profiles (g2g + 3 eldo).
for prof in chrome_profile_g2g chrome_profile_eldo \
            chrome_profile_eldo_bak1 chrome_profile_eldo_bak2; do
    rm -f "/opt/BotPasteDon/$prof"/{SingletonLock,SingletonCookie,SingletonSocket,parent.lock,.parentlock,lock} 2>/dev/null || true
done

# Free ports in case fds linger.
for PORT in 8010 8001 8002 8030 8766; do
    fuser -k ${PORT}/tcp 2>/dev/null || true
done

echo "[$(date +%H:%M:%S)] All services stopped"
