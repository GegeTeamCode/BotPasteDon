#!/bin/bash
# BotPasteDon — Start all services with cleanup
# Usage: bash scripts/start.sh [--clean]

set -e
cd /opt/BotPasteDon

export HEADLESS_MODE=true
VENV="/opt/BotPasteDon/venv/bin/python"
LOG_DIR="/tmp"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $1"; }
warn() { echo -e "${YELLOW}[$(date +%H:%M:%S)]${NC} $1"; }
err()  { echo -e "${RED}[$(date +%H:%M:%S)]${NC} $1"; }

# ── Cleanup ──
cleanup() {
    log "Cleaning up old processes..."

    # Graceful shutdown first — let Chrome save cookies before killing
    pkill -f "auth.main" 2>/dev/null || true
    pkill -f "workers.g2g_worker" 2>/dev/null || true
    pkill -f "workers.eldorado_worker" 2>/dev/null || true
    pkill -f "coordinator.main" 2>/dev/null || true
    pkill -f "scanners.main" 2>/dev/null || true
    pkill -f "watchdog.py" 2>/dev/null || true
    pkill -f "dashboard.server" 2>/dev/null || true
    sleep 3

    # Force kill anything still running
    pkill -9 -f "auth.main" 2>/dev/null || true
    pkill -9 -f "workers.g2g_worker" 2>/dev/null || true
    pkill -9 -f "workers.eldorado_worker" 2>/dev/null || true
    pkill -9 -f "coordinator.main" 2>/dev/null || true
    pkill -9 -f "scanners.main" 2>/dev/null || true
    pkill -9 -f "watchdog.py" 2>/dev/null || true
    pkill -9 -f "dashboard.server" 2>/dev/null || true
    pkill -9 -f chromedriver 2>/dev/null || true
    pkill -9 -f chrome 2>/dev/null || true
    pkill -9 -f "playwright" 2>/dev/null || true
    sleep 2

    for PORT in 8010 8001 8002 8030 8766; do
        fuser -k ${PORT}/tcp 2>/dev/null || true
    done
    sleep 1

    rm -f /opt/BotPasteDon/chrome_profile_g2g/SingletonLock 2>/dev/null || true
    rm -f /opt/BotPasteDon/chrome_profile_eldo/SingletonLock 2>/dev/null || true

    # Clear stale heartbeat data so watchdog doesn't false-restart
    sqlite3 /opt/BotPasteDon/data/orders.db "DELETE FROM heartbeat" 2>/dev/null || true

    log "Cleanup done"
}

# ── Start services in order ──
start_services() {
    log "Starting BotPasteDon services..."

    # 1. Auth (must start first — provides JWT + cookies)
    log "[1/8] Starting auth service..."
    nohup $VENV -u -m auth.main > $LOG_DIR/auth6.log 2>&1 &
    AUTH_PID=$!
    sleep 10
    for i in 1 2 3; do
        if curl -sf http://localhost:8010/health > /dev/null 2>&1; then
            log "[1/8] Auth OK (PID: $AUTH_PID)"
            break
        fi
        warn "Auth not ready, waiting... (attempt $i)"
        sleep 5
    done

    # 2. G2G Worker
    log "[2/8] Starting G2G worker..."
    nohup $VENV -u -m workers.g2g_worker > $LOG_DIR/g2g_worker.log 2>&1 &
    log "[2/8] G2G worker started (PID: $!)"

    # 3. Eldorado Worker
    log "[3/8] Starting Eldorado worker..."
    nohup $VENV -u -m workers.eldorado_worker > $LOG_DIR/eldo_worker.log 2>&1 &
    log "[3/8] Eldorado worker started (PID: $!)"

    # 4. Coordinator
    log "[4/8] Starting coordinator..."
    nohup $VENV -u -m coordinator.main > $LOG_DIR/coordinator.log 2>&1 &
    log "[4/8] Coordinator started (PID: $!)"

    # 5. G2G Scanner
    log "[5/8] Starting G2G scanner..."
    nohup $VENV -u -m scanners.main --platform g2g > $LOG_DIR/g2g_scanner.log 2>&1 &
    log "[5/8] G2G scanner started (PID: $!)"

    # 6. Eldorado Scanner
    log "[6/8] Starting Eldorado scanner..."
    nohup $VENV -u -m scanners.main --platform eldorado > $LOG_DIR/eldo_scanner.log 2>&1 &
    log "[6/8] Eldorado scanner started (PID: $!)"

    # 7. Watchdog
    log "[7/8] Starting watchdog..."
    nohup $VENV scripts/watchdog.py > $LOG_DIR/watchdog.log 2>&1 &
    log "[7/8] Watchdog started (PID: $!)"

    # 8. Dashboard
    log "[8/8] Starting dashboard..."
    nohup $VENV -u -m dashboard.server > $LOG_DIR/dashboard.log 2>&1 &
    log "[8/8] Dashboard started (PID: $!)"

    log "All services started"
    log "Logs: /tmp/{auth6,g2g_worker,eldo_worker,coordinator,g2g_scanner,eldo_scanner,watchdog,dashboard}.log"
}

# ── Status check ──
show_status() {
    echo ""
    log "=== Service Status ==="
    for SVC in "auth.main" "workers.g2g_worker" "workers.eldorado_worker" "coordinator.main" "scanners.main" "watchdog.py" "dashboard.server"; do
        PID=$(pgrep -f "$SVC" 2>/dev/null || echo "DOWN")
        printf "  %-25s %s\n" "$SVC" "PID: $PID"
    done
    echo ""
    log "=== Ports ==="
    ss -tlnp 2>/dev/null | grep -E "8010|8001|8002|8030|8766" || echo "  No ports bound"
}

# ── Main ──
if [ "$1" = "--clean" ] || [ "$1" = "clean" ]; then
    cleanup
fi

start_services
sleep 3
show_status
