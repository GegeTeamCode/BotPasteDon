"""Dashboard web server — service status, auth status, OTP relay, realtime logs.

Usage:
    python -m dashboard.server

Runs on port 8766 (configurable via DASHBOARD_PORT env).
"""

import asyncio
import json
import os
import signal
import time
import logging
from pathlib import Path

from aiohttp import web, ClientSession, ClientTimeout
from aiohttp_sse import sse_response

from shared.config import DATABASE_PATH, AUTH_SERVICE_URL, DASHBOARD_PORT
from shared.database import Database
from shared.logging_config import setup_logger

logger = setup_logger("dashboard")

TEMPLATE_DIR = Path(__file__).parent / "templates"

SERVICES = {
    "auth_service": {"name": "Auth Service", "tier": 0},
    "worker_eldo": {"name": "Eldorado Worker", "tier": 1},
    "worker_g2g": {"name": "G2G Worker", "tier": 1},
    "coordinator": {"name": "Coordinator", "tier": 2},
    "scanner_eldorado": {"name": "Eldorado Scanner", "tier": 3},
    "scanner_g2g": {"name": "G2G Scanner", "tier": 3},
    "dashboard": {"name": "Dashboard", "tier": 3},
}

LOG_FILES = {
    "auth": "/tmp/auth6.log",
    "g2g_worker": "/tmp/g2g_worker.log",
    "eldo_worker": "/tmp/eldo_worker.log",
    "coordinator": "/tmp/coordinator.log",
    "g2g_scanner": "/tmp/g2g_scanner.log",
    "eldo_scanner": "/tmp/eldo_scanner.log",
    "watchdog": "/tmp/watchdog.log",
    "dashboard": "/tmp/dashboard.log",
}

STALE_THRESHOLD = 90
_shutdown = asyncio.Event()
db: Database = None
http: ClientSession = None
_sse_clients: list = []


# ── Helpers ──

async def proxy_get(path: str):
    try:
        async with http.get(f"{AUTH_SERVICE_URL}{path}", timeout=ClientTimeout(total=10)) as resp:
            return await resp.json()
    except Exception as e:
        return {"error": str(e)}


async def proxy_post(path: str, json_body: dict = None):
    try:
        async with http.post(f"{AUTH_SERVICE_URL}{path}", json=json_body,
                             timeout=ClientTimeout(total=30)) as resp:
            return await resp.json(), resp.status
    except Exception as e:
        return {"error": str(e)}, 503


def _tail_file(path: str, n: int = 100) -> list:
    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()[-n:]
        return [l.rstrip() for l in lines]
    except Exception:
        return []


async def _broadcast_sse(event: str, data: dict):
    dead = []
    for i, resp in enumerate(_sse_clients):
        try:
            await resp.send(json.dumps(data), event=event)
        except Exception:
            dead.append(i)
    for i in reversed(dead):
        _sse_clients.pop(i)


# ── Handlers ──

async def handle_index(request: web.Request):
    html_path = TEMPLATE_DIR / "index.html"
    if not html_path.exists():
        return web.Response(text="Dashboard template not found", status=500)
    return web.Response(text=html_path.read_text(encoding="utf-8"), content_type="text/html")


async def handle_status(request: web.Request):
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT service_name, last_beat, pid FROM heartbeat"
        ).fetchall()

    now = time.time()
    heartbeat_map = {r["service_name"]: dict(r) for r in rows}
    services = []
    stale_count = 0

    for svc_id, svc_info in SERVICES.items():
        hb = heartbeat_map.get(svc_id)
        entry = {
            "id": svc_id,
            "name": svc_info["name"],
            "tier": svc_info["tier"],
            "pid": None,
            "last_beat": None,
            "status": "unknown",
            "age_seconds": None,
        }
        if hb and hb["last_beat"]:
            beat_str = hb["last_beat"]
            entry["pid"] = hb["pid"]
            entry["last_beat"] = beat_str
            try:
                from datetime import datetime
                beat_dt = datetime.fromisoformat(beat_str)
                age = now - beat_dt.timestamp()
                entry["age_seconds"] = int(age)
                entry["status"] = "healthy" if age < STALE_THRESHOLD else "stale"
                if entry["status"] == "stale":
                    stale_count += 1
            except Exception:
                entry["status"] = "unknown"
        services.append(entry)

    services.sort(key=lambda s: (s["tier"], s["name"]))
    return web.json_response({"services": services, "stale_count": stale_count})


async def handle_auth_status(request: web.Request):
    data = await proxy_get("/health")
    return web.json_response(data)


async def handle_login_status(request: web.Request):
    data = await proxy_get("/auth/login-status")
    return web.json_response(data)


async def handle_otp(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    result, status = await proxy_post("/auth/otp", body)
    return web.json_response(result, status=status)


async def handle_auto_login(request: web.Request):
    result, status = await proxy_post("/auth/relogin/chrome_profile_g2g")
    return web.json_response(result, status=status)


async def handle_relogin_profile(request: web.Request):
    profile = request.match_info.get("profile", "")
    result, status = await proxy_post(f"/auth/relogin/{profile}")
    return web.json_response(result, status=status)


async def handle_profile_status(request: web.Request):
    data = await proxy_get("/auth/profile-status")
    return web.json_response(data)


async def handle_orders(request: web.Request):
    offset = int(request.query.get("offset", "0"))
    limit = int(request.query.get("limit", "10"))
    with db._get_conn() as conn:
        rows = conn.execute(
            """SELECT order_id, platform, status, item_name, quantity,
                      character, customer_name, created_at, updated_at
               FROM orders ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
    return web.json_response({
        "orders": [dict(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    })


async def handle_log(request: web.Request):
    name = request.match_info.get("name", "auth")
    n = int(request.query.get("n", "100"))
    path = LOG_FILES.get(name)
    if not path:
        return web.json_response({"error": f"Unknown log: {name}", "available": list(LOG_FILES.keys())}, status=404)
    return web.json_response({"name": name, "lines": _tail_file(path, n)})


async def handle_logs_all(request: web.Request):
    n = int(request.query.get("n", "50"))
    result = {}
    for name, path in LOG_FILES.items():
        result[name] = _tail_file(path, n)
    return web.json_response(result)


async def handle_sse(request: web.Request):
    resp = await sse_response(request)
    _sse_clients.append(resp)
    try:
        await resp.wait()
    finally:
        if resp in _sse_clients:
            _sse_clients.remove(resp)
    return resp


# ── SSE broadcaster task ──

async def _sse_broadcaster():
    while not _shutdown.is_set():
        # Broadcast service status every 5s
        try:
            with db._get_conn() as conn:
                rows = conn.execute("SELECT service_name, last_beat, pid FROM heartbeat").fetchall()
            now = time.time()
            svc_data = []
            for r in rows:
                rd = dict(r)
                try:
                    from datetime import datetime
                    age = now - datetime.fromisoformat(rd["last_beat"]).timestamp()
                    rd["age_seconds"] = int(age)
                    rd["status"] = "healthy" if age < STALE_THRESHOLD else "stale"
                except Exception:
                    rd["age_seconds"] = None
                    rd["status"] = "unknown"
                svc_data.append(rd)
            await _broadcast_sse("status", {"services": svc_data})
        except Exception:
            pass
        await asyncio.sleep(5)


# ── Main ──

async def run_dashboard():
    global db, http
    db = Database(DATABASE_PATH)
    http = ClientSession()

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/auth-status", handle_auth_status)
    app.router.add_get("/api/login-status", handle_login_status)
    app.router.add_post("/api/otp", handle_otp)
    app.router.add_post("/api/auth/g2g/auto-login", handle_auto_login)
    app.router.add_post("/api/auth/g2g/relogin/{profile}", handle_relogin_profile)
    app.router.add_get("/api/profile-status", handle_profile_status)
    app.router.add_get("/api/orders", handle_orders)
    app.router.add_get("/api/log/{name}", handle_log)
    app.router.add_get("/api/logs", handle_logs_all)
    app.router.add_get("/api/events", handle_sse)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", DASHBOARD_PORT)
    await site.start()
    logger.info("Dashboard listening on port %d (PID: %d)", DASHBOARD_PORT, os.getpid())

    # Heartbeat
    async def heartbeat():
        while not _shutdown.is_set():
            db.update_heartbeat("dashboard", os.getpid())
            await asyncio.sleep(30)

    asyncio.create_task(heartbeat())
    asyncio.create_task(_sse_broadcaster())
    await _shutdown.wait()

    logger.info("Shutting down...")
    await runner.cleanup()
    await http.close()
    logger.info("Dashboard stopped")


def main():
    def handle_signal(sig, frame):
        _shutdown.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        asyncio.run(run_dashboard())
    except KeyboardInterrupt:
        _shutdown.set()


if __name__ == "__main__":
    main()
