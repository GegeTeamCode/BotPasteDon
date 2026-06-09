#!/bin/bash
# BotPasteDon — Start all services with cleanup
# Usage: bash scripts/start.sh [--no-clean]
#   --no-clean   Skip pre-start cleanup (only use when you KNOW nothing is running)
set -e
cd /opt/BotPasteDon

export HEADLESS_MODE=true
VENV="/opt/BotPasteDon/venv/bin/python"
LOG_DIR="/tmp"

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $1"; }
warn() { echo -e "${YELLOW}[$(date +%H:%M:%S)]${NC} $1"; }

# Safely kill all python processes matching a pattern. Filters bash launcher
# (cmdline starts with "bash -c") so we don't accidentally kill the shell
# running this script when invoked over SSH (see operations.md "pkill -f
# self-match trap").
kill_pattern() {
    local pat="$1"
    pgrep -af "$pat" 2>/dev/null \
        | grep -vE '^\s*[0-9]+\s+(/bin/)?bash\s+-c' \
        | awk '{print $1}' \
        | xargs -r kill -9 2>/dev/null || true
}

# ── Cleanup ──
cleanup() {
    log "Cleaning up old processes..."

    # Graceful first — let Chrome flush cookies before SIGKILL.
    for pat in "auth.main" "workers.g2g_worker" "workers.eldorado_worker" \
               "coordinator.main" "scanners.main" "status_sync" \
               "watchdog.py" "dashboard.server"; do
        pgrep -f "$pat" 2>/dev/null \
            | xargs -r kill -TERM 2>/dev/null || true
    done
    sleep 3

    # Force kill anything still hanging (uses bash-filter helper).
    for pat in "auth.main" "workers.g2g_worker" "workers.eldorado_worker" \
               "coordinator.main" "scanners.main" "status_sync" \
               "watchdog.py" "dashboard.server" \
               "chromedriver" "camoufox-bin" "playwright"; do
        kill_pattern "$pat"
    done
    sleep 2

    # Free ports in case any straggler holds them.
    for PORT in 8010 8001 8002 8030 8766; do
        fuser -k ${PORT}/tcp 2>/dev/null || true
    done
    sleep 1

    # Profile locks — covers all 4 (main + 3 eldo backups + g2g).
    for prof in chrome_profile_g2g chrome_profile_eldo \
                chrome_profile_eldo_bak1 chrome_profile_eldo_bak2; do
        rm -f "/opt/BotPasteDon/$prof"/{SingletonLock,SingletonCookie,SingletonSocket,parent.lock,.parentlock,lock} 2>/dev/null || true
    done

    # Clear heartbeat so watchdog doesn't see stale rows on startup.
    sqlite3 /opt/BotPasteDon/data/orders.db "DELETE FROM heartbeat" 2>/dev/null || true

    log "Cleanup done"
}

# ── Start services in order ──
start_services() {
    log "Starting BotPasteDon services..."

    # 1. Auth (must start first — provides JWT + cookies)
    log "[1/9] Starting auth service..."
    nohup $VENV -u -m auth.main > $LOG_DIR/auth.log 2>&1 &
    AUTH_PID=$!
    sleep 10
    for i in 1 2 3; do
        if curl -sf http://localhost:8010/health > /dev/null 2>&1; then
            log "[1/9] Auth OK (PID: $AUTH_PID)"
            break
        fi
        warn "Auth not ready, waiting... (attempt $i)"
        sleep 5
    done

    # 2-3. Workers (need auth)
    log "[2/9] Starting G2G worker..."
    nohup $VENV -u -m workers.g2g_worker > $LOG_DIR/g2g_worker.log 2>&1 &
    log "[2/9] G2G worker started (PID: $!)"

    log "[3/9] Starting Eldorado worker..."
    nohup $VENV -u -m workers.eldorado_worker > $LOG_DIR/eldo_worker.log 2>&1 &
    log "[3/9] Eldorado worker started (PID: $!)"

    # 4. Coordinator (dispatches to workers)
    log "[4/9] Starting coordinator..."
    nohup $VENV -u -m coordinator.main > $LOG_DIR/coordinator.log 2>&1 &
    log "[4/9] Coordinator started (PID: $!)"

    # 5-6. Scanners (feed coordinator via Discord webhook)
    log "[5/9] Starting G2G scanner..."
    nohup $VENV -u -m scanners.main --platform g2g > $LOG_DIR/g2g_scanner.log 2>&1 &
    log "[5/9] G2G scanner started (PID: $!)"

    log "[6/9] Starting Eldorado scanner..."
    nohup $VENV -u -m scanners.main --platform eldorado > $LOG_DIR/eldo_scanner.log 2>&1 &
    log "[6/9] Eldorado scanner started (PID: $!)"

    # 7. Status sync (marketplace state → ERP webhook every 30m)
    log "[7/9] Starting status_sync..."
    nohup $VENV -u -m status_sync > $LOG_DIR/status_sync.log 2>&1 &
    log "[7/9] Status sync started (PID: $!)"

    # 8. Watchdog (must start AFTER everything else — otherwise it might
    #    interpret missing heartbeats as crashes and respawn duplicates)
    log "[8/9] Starting watchdog..."
    nohup $VENV scripts/watchdog.py > $LOG_DIR/watchdog.log 2>&1 &
    log "[8/9] Watchdog started (PID: $!)"

    # 9. Dashboard (web UI; no dependencies)
    log "[9/9] Starting dashboard..."
    nohup $VENV -u -m dashboard.server > $LOG_DIR/dashboard.log 2>&1 &
    log "[9/9] Dashboard started (PID: $!)"

    log "All services started"
    log "Logs: /tmp/{auth,g2g_worker,eldo_worker,coordinator,g2g_scanner,eldo_scanner,status_sync,watchdog,dashboard}.log"
}

# ── Status check ──
show_status() {
    echo ""
    log "=== Service Status ==="
    for SVC in "auth.main" "workers.g2g_worker" "workers.eldorado_worker" \
               "coordinator.main" "scanners.main" "status_sync" \
               "watchdog.py" "dashboard.server"; do
        # Filter bash launchers from PID list so the column shows real python PIDs.
        PIDS=$(pgrep -af "$SVC" 2>/dev/null \
            | grep -vE '^\s*[0-9]+\s+(/bin/)?bash\s+-c' \
            | awk '{print $1}' | paste -sd, -)
        [ -z "$PIDS" ] && PIDS="DOWN"
        printf "  %-25s PID(s): %s\n" "$SVC" "$PIDS"
    done
    echo ""
    log "=== Ports ==="
    ss -tlnp 2>/dev/null | grep -E "8010|8001|8002|8030|8766" || echo "  No ports bound"
}

# ── Main ──
# Default: always clean before start. Override with --no-clean only when you're
# certain no stale processes are around (uncommon).
if [ "$1" != "--no-clean" ]; then
    cleanup
fi

start_services
sleep 3
show_status
