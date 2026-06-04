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

## AI Operator Notes

Nguyen tac vu khi mot AI khac van hanh server nay:

**SSH tu Windows host (no `sshpass`)**: Dung `paramiko` thay vi `ssh ... <<<password`. Pattern chuan:

```python
import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("192.168.2.220", username="root", password="123456", timeout=15)
_, stdout, _ = ssh.exec_command("ps aux | grep -v grep")
print(stdout.read().decode())
ssh.close()
```

**Launch background process qua paramiko**: `nohup ... &` co the lam paramiko channel hang. Dung `setsid + </dev/null + & disown` va goi qua `Transport.open_session()` thay vi `exec_command()`:

```python
chan = ssh.get_transport().open_session()
chan.exec_command("cd /opt/BotPasteDon && setsid venv/bin/python -m auth.main </dev/null >/tmp/auth.log 2>&1 & disown")
# poll exit_status_ready instead of stdout.read()
```

**`pkill -f` self-match trap (QUAN TRONG)**: Khi chay multi-command qua SSH:
```bash
# SAI - bash session co cmdline chua "chrome_profile_g2g",
# pkill -f match chinh bash dang chay -> bash chet truoc cau lenh tiep
pkill -f auth.main; pkill -f chrome_profile_g2g; pkill -f chromedriver
```
```bash
# DUNG - moi pkill mot ssh.exec_command rieng, hoac dung pgrep | xargs:
pgrep -f auth.main          | xargs -r kill -9
pgrep -f chrome_profile_g2g | xargs -r kill -9
pgrep -f chromedriver       | xargs -r kill -9
```
`pgrep -af` (in command line) + `xargs -r kill -9` (-r = skip neu input rong) la pattern an toan.

**Phan biet python process vs bash launcher**: Khi check duplicate, `pgrep -af 'auth.main'` tra **2 entry** cho mot service:
- `bash -c "cd /opt/BotPasteDon && nohup python -m auth.main..."` (launcher shell, vo hai)
- `python -u -m auth.main` (service that)

Loc bash wrapper bang `re.match(r"^bash\s+-c", cmd)` truoc khi dem instance. Xem [`scripts/check_all_processes.py`](../scripts/check_all_processes.py).

**Watchdog tu respawn**: Khi can stop hoan toan mot service de deploy/debug, **phai stop watchdog truoc**, neu khong watchdog se restart service ngay khi ban kill xong:
```bash
pgrep -f 'watchdog.py' | xargs -r kill -9   # luon stop dau tien
# ... do deploy / restart ...
nohup venv/bin/python scripts/watchdog.py > /tmp/watchdog.log 2>&1 &   # restart cuoi cung
```

**Khong commit nham work cua nguoi khac**: Khi mo session, kiem tra `git status` truoc. Neu thay `modified` files khong lien quan task hien tai, hoi user truoc khi `git add` — co the la work-in-progress chua xong cua user.

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
# Toan canh 8 service trong 1 lenh (chay tu Windows host):
python scripts/check_all_processes.py
# In bang services + PIDs + ports + heartbeat. Bao DUP/DOWN/NO-PORT neu sai.

# Kiem tra tat ca process truc tiep tren server
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

### `/health` Endpoint Schema

`GET http://localhost:8010/health` → JSON:

```json
{
  "status": "ok",
  "uptime": 1596,                            // giay tu khi start
  "g2g": {
    "has_jwt": true,                         // co JWT chua
    "jwt_expires_in": 649,                   // giay den khi het han (JWT song 15 phut)
    "fresh": true,                           // fresh = duoi 13 phut tu luc capture
    "active_profile": "chrome_profile_g2g",
    "cookies": 29
  },
  "eldo": {
    "has_cookies": true,
    "fresh": true,                           // fresh = duoi 13 phut
    "active_profile": "chrome_profile_eldo_bak1",  // dang dung profile nao
    "cookies": 126,
    "xsrf": true,                            // co XSRF token chua
    "logged_in": true                        // session da login chua
  }
}
```

**Co the gay nghi ngo**:
- `fresh=false` keo dai > 15 phut → capture bi fail, xem `/tmp/auth*.log` (file moi nhat).
- `logged_in=false` tren Eldo → profile mat session, can mo VNC re-login (xem muc "VNC inspection" duoi).
- `has_jwt=false` tren G2G → Chrome session khong tao duoc, thuong la profile lock hoac orphan chrome (xem muc "Chrome Profile Lock").

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

### Tra lai bang chung cho don da Completed (proof khong tu len marketplace)

**Trieu chung**: Don da deliver xong (qty submitted, buyer da nhan), nhung phia
marketplace (G2G/Eldorado) khong nhan file bang chung → seller bi withhold
payment.

**Nguyen nhan thuong gap**:
- Worker xu ly lan dau bi auth error / JWT 401 / lock conflict roi mark FAILED
  truoc khi kip upload proof — case nay phai xay ra truoc 2026-06-04
  (truoc patch V1+V2+V3 auth + worker JWT-retry).
- Worker download file tu ERP fail → khong co file de upload.

**Tien quyet**:
- SO tren ERP phai co `Order Evidence` record voi file attachment hop le.
  Neu thieu, upload file qua ERP UI truoc, roi moi re-trigger.

**Cach lam (idempotent, an toan voi don da Completed)**:

```powershell
# 1 don:
python scripts/retry_post_evidence.py 1780330530899GUQE

# nhieu don cung lan:
python scripts/retry_post_evidence.py 1780330530899GUQE 1780327135934HNAR ...
```

Script:
1. SSH ERP (192.168.2.100), tim ten Sell Order tu `external_order_id`.
2. Tail log G2G + Eldo worker dang chay tren bot server.
3. Goi `post_evidence_to_marketplace(SO, skip_steps='["qty"]')` cho moi SO.
   `skip_steps=['qty']` bat buoc vi qty da submit roi — bo qua se kien G2G/Eldo
   tra `400: Cannot perform action when order item status is delivering`.
4. Cho `Completed: <order_id>` xuat hien trong worker log (~6s/don).
5. In bang summary: `completed` / `failed` / `erp_fail` / `no_so` / `timeout`.

**Cac verdict thuong gap**:

| Verdict | Y nghia | Hanh dong |
|---------|---------|-----------|
| `completed` | Worker da upload proof + chat thanh cong | Verify tren marketplace dashboard |
| `erp_fail` voi "Chua co bang chung de dang" | SO khong co `Order Evidence` | Upload file vao ERP truoc, chay lai |
| `no_so` | Khong tim thay SO voi `external_order_id` do | Don cu/khong qua ERP — xu ly thu cong tren marketplace |
| `failed` | Worker raise loi sau khi nhan task | Xem `/tmp/g2g_worker*.log` hoac `/tmp/eldo_worker*.log` quanh thoi diem do |
| `timeout` | Worker khong report Completed trong 90s | Don co the dang retry JWT — kiem tra log truc tiep |

**Luu y ERP-side workflow exception**:
`post_evidence_to_marketplace` raise `WorkflowTransitionError: Not a valid
Workflow Action` SAU khi worker accept (vi SO da o trang thai terminal nhu
`Completed`/`Delivered`, khong con transition `Deliver`). Day la **benign** —
proof da gui truoc khi exception fire. Script tu detect string nay va treat la
success.

### VNC Inspection — mo Camoufox visible de check session

Khi can xem profile Eldo dang trong trang thai gi (login con valid? bi captcha? trang nao?):

```python
# Tu Windows host:
python scripts/deploy_open_eldo.py
# Script SCP open_eldo_vnc.py len /tmp/, launch Camoufox visible
# tren Xvfb :99 voi profile chrome_profile_eldo (main).
```

Sau do connect VNC viewer (TightVNC/RealVNC/TigerVNC) tu may:
- Host: `192.168.2.220:5900`
- Password: `123456`

Khi xong, dong viewer:
```bash
ssh root@192.168.2.220 "pkill -f open_eldo_vnc.py ; pkill -f camoufox-bin"
```

**Quan trong**: Sau khi pkill camoufox-bin, profile co the de lai `parent.lock` → block lan auth capture ke tiep. Tu **2026-06-04** auth tu xoa lock khi startup/next-capture, khong can lo. Truoc do phai `rm -f chrome_profile_eldo/parent.lock chrome_profile_eldo/.parentlock chrome_profile_eldo/lock` thu cong.

**Conflict canh bao**: Auth service capture moi ~13 phut. Neu viewer dang mo va auth try capture cung profile → conflict lock, mot ben se fail. Hoac (a) stop auth tam thoi, hoac (b) chap nhan risk, hoac (c) copy profile sang folder tam de viewer khong dung profile that.

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

**Luu y**: LXC khong co `sqlite3` CLI — dung Python thay the. `pgrep` co san — uu tien `pgrep -af '<pattern>' | xargs -r kill -9` hon `ps aux | grep | awk | xargs kill` (xem AI Operator Notes).

## Scripts Catalog

Tat ca scripts trong `scripts/`. Chia 3 nhom: **server-resident** (chay tren server), **client-side ops** (chay tu Windows host qua paramiko), **helper modules** (script con duoc upload boi script khac).

### Server-resident — chay tren server, da co trong start.sh

| Script | Muc dich | Khi nao chay |
|--------|----------|--------------|
| [`start.sh`](../scripts/start.sh) | Start all 8 services theo thu tu phu thuoc (auth -> workers -> coordinator -> scanners -> dashboard -> watchdog) | Sau reboot server hoac sau full stop |
| [`stop.sh`](../scripts/stop.sh) | Stop all services | Truoc khi reboot hoac maintenance lon |
| [`watchdog.py`](../scripts/watchdog.py) | Long-running supervisor — check heartbeat moi 30s, restart service neu khong beat trong 90s | Luon chay (tu start.sh) |

### Client-side ops — chay tu Windows host, dung paramiko vao server

| Script | Muc dich | Output |
|--------|----------|--------|
| [`check_all_processes.py`](../scripts/check_all_processes.py) | Audit toan canh: liet ke 8 service, PID (chi count python, skip bash launcher), port, heartbeat HH:MM:SS. Bao `OK`/`DOWN`/`DUP xN`/`NO-PORT`. Cung in `/health` json. | Bang summary + verdict cuoi |
| [`deploy_auth_patch.py`](../scripts/deploy_auth_patch.py) | Deploy `auth/main.py`: upload via SFTP -> backup -> stop watchdog -> stop auth/browsers -> plant lock files de verify cleanup -> start auth -> trigger /auth/eldo + /auth/g2g -> in audit. | Step-by-step log |
| [`deploy_open_eldo.py`](../scripts/deploy_open_eldo.py) | Launch Camoufox visible voi profile `chrome_profile_eldo` (main) tren Xvfb :99. Dung de xem session qua VNC. | Path log + connect info |
| [`open_eldo_vnc.py`](../scripts/open_eldo_vnc.py) | (Helper) — Script chay tren server, mo Camoufox headless=False, persistent_context tren profile main, dieu huong eldorado.gg, ngu vo han. SCP-deployed bang `deploy_open_eldo.py`. Khong chay truc tiep tu host. | – |
| [`unlock_profiles.py`](../scripts/unlock_profiles.py) | Manual fallback khi auth tat hoan toan va profile co lock cu: pkill leftover camoufox + xoa Firefox/Chrome lock files cua all profiles -> trigger /auth/eldo. Sau **2026-06-04** it khi can vi auth co auto-cleanup. | Cleanup log |
| [`retry_post_evidence.py`](../scripts/retry_post_evidence.py) | Re-trigger ERP `post_evidence_to_marketplace` cho 1+ don da Completed nhung proof khong toi marketplace (worker fail truoc do). SSH ERP + tail worker log + goi voi `skip_steps=['qty']` per SO. Xem chi tiet o muc "Tra lai bang chung". | Bang verdict per order |
| [`deploy_workers.py`](../scripts/deploy_workers.py) | Deploy `workers/*.py` qua SFTP. Stop watchdog -> kill workers -> upload -> start workers -> restart watchdog -> in audit + reachability probe. | Step log + audit table |

### Khi nao dung script nao

```
Trien khai code moi cho auth        -> deploy_auth_patch.py
Trien khai code moi cho worker      -> (chua co script chuyen, tham khao deploy_auth_patch.py)
Kiem tra he thong dang on khong     -> check_all_processes.py
Auth Eldo bi 401 mai khong khoi     -> 1) check_all_processes.py 2) xem /tmp/auth*.log
                                       3) restart auth (xem "Restart Auth Service")
                                       4) neu van fail: unlock_profiles.py
Muon xem profile dang trong state gi -> deploy_open_eldo.py + VNC viewer
Scanner khong tim thay don           -> tail /tmp/eldo_scanner.log + Troubleshooting
Don da Completed nhung proof khong   -> retry_post_evidence.py <order_id> [<order_id>...]
  toi marketplace -> seller bi giu tien
```

### Server-only legacy scripts (khong trong repo)

Mot so script da co tren `/opt/BotPasteDon/scripts/` nhung khong commit vao repo (legacy debug tools):
`check_cookies.py`, `check_detail.py`, `check_game.py`, `check_order.py`, `check_pending.py`,
`debug_kw.py`, `debug_scan.py`, `delete_order.py`, `dump_order.py`, `test_mapping.py`.

Chay truc tiep tren server: `cd /opt/BotPasteDon && venv/bin/python scripts/<name>.py`. Cac script nay khong critical cho operations — chu yeu de adhoc inspection.
