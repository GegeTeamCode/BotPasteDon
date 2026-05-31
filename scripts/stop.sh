#!/bin/bash
# BotPasteDon — Stop all services gracefully
set -e
cd /opt/BotPasteDon

echo "[$(date +%H:%M:%S)] Stopping BotPasteDon services..."

# Stop in reverse order
for SVC in "watchdog.py" "scanners.main" "coordinator.main" "workers.g2g_worker" "workers.eldorado_worker" "auth.main"; do
    PID=$(pgrep -f "$SVC" 2>/dev/null || true)
    if [ -n "$PID" ]; then
        kill $PID 2>/dev/null || true
        echo "  Stopped $SVC (PID: $PID)"
    fi
done

sleep 3

# Force kill anything still running
for SVC in "watchdog.py" "scanners.main" "coordinator.main" "workers.g2g_worker" "workers.eldorado_worker" "auth.main"; do
    PID=$(pgrep -f "$SVC" 2>/dev/null || true)
    if [ -n "$PID" ]; then
        kill -9 $PID 2>/dev/null || true
        echo "  Force-killed $SVC (PID: $PID)"
    fi
done

# Clean Chrome
pkill -9 -f chromedriver 2>/dev/null || true
rm -f /opt/BotPasteDon/chrome_profile_g2g/SingletonLock 2>/dev/null || true

echo "[$(date +%H:%M:%S)] All services stopped"
