# Operations Guide — BotPasteDon

## Server Info

| Item | Value |
|------|-------|
| Server | LXC on Proxmox, `192.168.2.220` |
| SSH | `root` / `123456` |
| VNC | `192.168.2.220:5900` / `123456` |
| App dir | `/opt/BotPasteDon` |
| Python | `/opt/BotPasteDon/venv/bin/python` |
| ERP | `192.168.2.100:80` (Frappe/ERPNext) |
| OS | Linux (LXC) |

## Service Ports

| Service | Port | Process |
|---------|------|---------|
| Auth | 8010 | `python -m auth.main` |
| Eldo Worker | 8001 | `python -m workers.eldorado_worker` |
| G2G Worker | 8002 | `python -m workers.g2g_worker` |
| Coordinator | 8030 | `python -m coordinator.main` |
| Dashboard | 8766 | `python -m dashboard.server` |

## Startup / Shutdown

```bash
# Start all (dung start.sh)
cd /opt/BotPasteDon && bash scripts/start.sh

# Stop all
bash scripts/stop.sh

# Start thu cong (thu tu quan trong)
HEADLESS_MODE=true nohup venv/bin/python -u -m auth.main > /tmp/auth.log 2>&1 &
sleep 5
nohup venv/bin/python -u -m workers.eldorado_worker > /tmp/eldo_worker.log 2>&1 &
nohup venv/bin/python -u -m workers.g2g_worker > /tmp/g2g_worker.log 2>&1 &
sleep 3
nohup venv/bin/python -u -m coordinator.main > /tmp/coordinator.log 2>&1 &
sleep 3
nohup venv/bin/python -u -m scanners.main --platform g2g > /tmp/g2g_scanner.log 2>&1 &
nohup venv/bin/python -u -m scanners.main --platform eldorado > /tmp/eldo_scanner.log 2>&1 &
nohup venv/bin/python scripts/watchdog.py > /tmp/watchdog.log 2>&1 &
nohup venv/bin/python -u -m dashboard.server > /tmp/dashboard.log 2>&1 &
```

## Health Check

```bash
# Kiem tra tat ca process
ps aux | grep -E "scanner|worker|auth|coordinator|dashboard|watchdog" | grep -v grep

# Kiem tra auth service
curl -s http://localhost:8010/health

# Kiem tra heartbeat
venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/orders.db')
for r in conn.execute('SELECT * FROM heartbeat').fetchall():
    print(r)
conn.close()
"

# Kiem tra don chua hoan thanh
venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/orders.db')
c = conn.cursor()
for status in ['DETECTED', 'FAILED', 'DELIVERING']:
    c.execute('SELECT count(*) FROM orders WHERE status=?', (status,))
    print(f'{status}: {c.fetchone()[0]}')
c.execute('SELECT count(*) FROM orders WHERE erp_synced=0 AND status NOT IN (\"DETECTED\",\"FAILED\")')
print(f'ERP unsynced: {c.fetchone()[0]}')
conn.close()
"
```

## Log Locations

Tat ca logs ra stdout, redirect vao `/tmp/`:

| Log | File |
|-----|------|
| Auth | `/tmp/auth6.log` |
| Eldo Scanner | `/tmp/eldo_scanner.log` |
| G2G Scanner | `/tmp/g2g_scanner.log` |
| Eldo Worker | `/tmp/eldo_worker.log` |
| G2G Worker | `/tmp/g2g_worker.log` |
| Coordinator | `/tmp/coordinator.log` |
| Dashboard | `/tmp/dashboard.log` |
| Watchdog | `/tmp/watchdog.log` |

```bash
# Xem log real-time
tail -f /tmp/eldo_scanner.log

# Tim loi gan day
grep -i "error\|failed\|traceback" /tmp/eldo_scanner.log | tail -20
```

## Deploy Code Changes

```bash
# 1. Tu local machine (Windows)
scp -r scanners/ root@192.168.2.220:/opt/BotPasteDon/
scp -r shared/ root@192.168.2.220:/opt/BotPasteDon/

# 2. SSH vao server, restart service can thiet
ssh root@192.168.2.220

# Kill + restart chi scanner eldo
ps aux | grep 'scanners.main.*eldo' | grep -v grep | awk '{print $2}' | xargs kill -9
cd /opt/BotPasteDon && nohup venv/bin/python -u -m scanners.main --platform eldorado > /tmp/eldo_scanner.log 2>&1 &

# Tuong tu cho cac service khac
```

**Luu y**: Khi deploy config thay doi (`.env`, `shared/config.py`), can restart **tat ca** service doc config.

## Troubleshooting

### Scanner khong tim thay don

1. Kiem tra auth: `curl -s http://localhost:8010/auth/eldo` — co cookies khong?
2. Kiem tra API truc tiep:
   ```bash
   venv/bin/python -c "
   import requests, json
   r = requests.get('http://localhost:8010/auth/eldo', timeout=30)
   d = r.json()
   resp = requests.get('https://www.eldorado.gg/api/orders/me/seller/orders',
       params={'orderState': 'PendingDelivery', 'take': '20'},
       cookies=d['cookies'], headers={'X-XSRF-TOKEN': d['xsrf_token']})
   print(len(resp.json().get('results', [])), 'pending orders')
   "
   ```
3. Kiem tra don da co trong DB chua (co the da bi loc boi keyword filter)
4. Kiem tra keyword: don DETECTED co nghia bi whitelist/blacklist loai bo

### ERP 417 Error

Nguyen nhan: ERP tra `ValidationError` khi `orderId` hoac `platform` bi thieu.
- Don DETECTED (bi keyword filter) insert DB voi data toi thieu → ERP retry gui data khong du field
- **Fix**: Mark don DETECTED thanh `erp_synced=1`:
  ```bash
  venv/bin/python -c "
  import sqlite3
  conn = sqlite3.connect('data/orders.db')
  conn.execute('UPDATE orders SET erp_synced=1 WHERE status=\"DETECTED\"')
  conn.commit()
  print('Done')
  conn.close()
  "
  ```

### Auth Service Camoufox Error

**Trieu chung**: "Playwright Sync API inside the asyncio loop"
**Nguyen nhan**: Playwright sync API de lai asyncio state trong worker thread sau lan dung dau, nen profile thu 2/3 trong cung thread reuse cua ThreadPoolExecutor bi fail.
**Fix da co san trong code**:
1. `asyncio.set_event_loop(asyncio.new_event_loop())` dau `_capture_single()` — reset loop cho thread moi.
2. `EldoAuth.capture()` chay moi profile trong `ThreadPoolExecutor(max_workers=1)` rieng → thread luon fresh khi rotate profile.
**Khi van fail**: Restart auth service (auto-cleanup browsers + locks khi startup/shutdown, xem muc "Restart Auth Service" duoi).

### Camoufox Playwright TypeError (coreBundle.js url undefined)

**Trieu chung**:
```
TypeError: Cannot read properties of undefined (reading 'url')
  at FFBrowserContext.<anonymous> (.../coreBundle.js:49624:39)
```
**Nguyen nhan**: Bug trong Playwright bundle khi page error khong co `location`. Khong phai code minh.
**Workaround**: Retry logic + thread isolation cua `EldoAuth.capture()` tu retry profile khac → lan sau pass. Khong can lam gi them.
**Fix triet de**: Upgrade Camoufox/Playwright (chua lam).

### G2G Scanner 401 During Extract

**Trieu chung**: Scanner mark don delivering roi bi 401 khi fetch detail → don stuck.
**Fix**: Smart retry — invalidates JWT cache, poll cho JWT moi (120s), retry 1 lan. Da implement trong `g2g_scanner_api.py`.

### Restart Auth Service (chuan)

Auth service co auto-cleanup tu **2026-06-04**: startup va shutdown deu pkill browser con + xoa lock files (Firefox `parent.lock/.parentlock/lock` + Chrome `Singleton*`) tren toan bo 4 profile g2g/eldo/bak1/bak2. Atexit safety net them de phong SIGKILL.

→ Restart chuan chi can:
```bash
# Stop watchdog truoc (de tranh auto-respawn auth dang dung)
pgrep -f 'watchdog.py' | xargs -r kill -9
# Kill auth — atexit handler tu pkill browser con
pgrep -f 'auth.main' | xargs -r kill -9
sleep 2
# Start lai — startup tu pkill orphan + xoa lock
cd /opt/BotPasteDon && HEADLESS_MODE=true nohup venv/bin/python -u -m auth.main > /tmp/auth.log 2>&1 &
sleep 25
# Restart watchdog
nohup venv/bin/python scripts/watchdog.py > /tmp/watchdog.log 2>&1 &
```

### Chrome Profile Lock (khi auth tat hoan toan)

**Trieu chung**: "session not created" / "Firefox is already running" khi start service.
**Nguyen nhan**: Auth service da tat hoan toan (qua atexit khong chay) → orphan chrome/camoufox + lock con sot lai.
**Fix nhanh**: Restart auth service (xem muc tren) — auto-cleanup chay khi startup.
**Fix thu cong** (khi can):
```bash
pgrep -f camoufox-bin | xargs -r kill -9
pgrep -f chromedriver | xargs -r kill -9
pgrep -f chrome_profile_g2g | xargs -r kill -9
pgrep -f chrome_profile_eldo | xargs -r kill -9
for p in chrome_profile_g2g chrome_profile_eldo chrome_profile_eldo_bak1 chrome_profile_eldo_bak2; do
  rm -f /opt/BotPasteDon/$p/{parent.lock,.parentlock,lock,SingletonLock,SingletonCookie,SingletonSocket}
done
```

**Luu y khi mo VNC viewer Camoufox cho profile bot**: Khi dong viewer, lock files se ton lai. Truoc 2026-06-04 phai rm thu cong; tu sau khi auth co auto-cleanup, lan capture ke tiep cua auth se tu xoa.

### Duplicate Process

```bash
# Tim duplicate
ps aux | grep -E "scanner|worker|auth|coordinator" | grep -v grep | sort

# Kill tat ca, restart lai
bash scripts/stop.sh
sleep 3
ps aux | grep -E "scanner|worker|auth" | grep -v grep | awk '{print $2}' | xargs kill -9 2>/dev/null
bash scripts/start.sh
```

## Sua Keyword Filter

Whitelist/blacklist nam trong `shared/config.py`:

```python
SCANNER_CONFIG = {
    "whitelist": "Divine Orb, Chaos Orb, Exalted Orb, ..., Currency, Gems, ...",
    "blacklist": "Boosting, Leveling, Account, Custom oder",
}
```

Co the override bang env vars: `SCANNER_WHITELIST`, `SCANNER_BLACKLIST` trong `.env`.

**Khi them item moi**: Them vao whitelist, restart scanner.

## Sua Webhook Routing

Trong `shared/config.py`, thu tu mappings quyet dinh priority:

```python
"mappings": [
    {"game": "Diablo 4", "keywords": ["diablo 4", "diablo iv", "d4"], "url": WEBHOOK_DIABLO4},
    {"game": "Path of Exile 2", "keywords": ["poe2", "path of exile 2", "poe 2", "fate of the vaal"], "url": WEBHOOK_POE2},
    {"game": "Path of Exile", "keywords": ["path of exile", "poe1", "poe 1"], "url": WEBHOOK_POE1},
]
```

**First match wins** — Diablo 4 phai dung truoc PoE1/PoE2 de tranh match nham.

## G2G Title Mapping

Khi G2G offer title co pattern dac biet, override itemName:

```python
"G2G_TITLE_MAP": [
    {"title_pattern": "Flawless Horadric", "display_name": "Custom - Flawless Horadric Gems"},
    {"title_pattern": "Corrupted Roots", "display_name": "Corrupted Roots"},
]
```

Them mapping moi vao `SCANNER_CONFIG["G2G_TITLE_MAP"]`, restart G2G scanner.

## Database Operations

```bash
# Xem don theo status
venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/orders.db')
for r in conn.execute('SELECT order_id, platform, status, created_at FROM orders ORDER BY created_at DESC LIMIT 20').fetchall():
    print(r)
conn.close()
"

# Reset don de re-process
venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/orders.db')
conn.execute('DELETE FROM orders WHERE order_id = ?', ('ORDER_ID_HERE',))
conn.commit()
conn.close()
"

# Reset ERP sync cho don cu
venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/orders.db')
conn.execute('UPDATE orders SET erp_synced = 1 WHERE status = \"DETECTED\"')
conn.commit()
conn.close()
"
```

**Luu y**: LXC khong co `sqlite3` CLI — dung Python thay the. Khong co `pgrep` — dung `ps aux | grep`.
