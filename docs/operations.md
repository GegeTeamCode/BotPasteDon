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
| Status Sync | – | `python -m status_sync` (no port — polls every 30m) |
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
| Status Sync | `/tmp/status_sync.log` |
| Dashboard | `/tmp/dashboard.log` |
| Watchdog | `/tmp/watchdog.log` |

```bash
# Xem log real-time
tail -f /tmp/eldo_scanner.log

# Tim loi gan day
grep -i "error\|failed\|traceback" /tmp/eldo_scanner.log | tail -20
```

## Deploy Code Changes

### Git-based deploy (CHUAN, tu 2026-06-12)

`/opt/BotPasteDon` la **git checkout cua `origin/main`**. Repo la single source of
truth — server luon khop `origin/main`. Runtime (`.env`, `data/`, `chrome_profile_*`,
`venv/`) bi gitignore nen `git reset` khong dung toi.

```powershell
# 1. Sua code tren LOCAL, commit, push
git commit -am "fix: ..." ; git push origin main

# 2. Deploy: server git pull (reset --hard origin/main) + restart service can thiet
python scripts/deploy_git.py scanner_g2g            # sync + restart 1 service
python scripts/deploy_git.py worker_g2g worker_eldo # nhieu service
python scripts/deploy_git.py                        # CHI sync code, khong restart
python scripts/deploy_git.py all                    # sync + restart tat ca (nang)
```

Service hop le: `auth, scanner_g2g, scanner_eldo, worker_g2g, worker_eldo,
coordinator, dashboard`. Script tu stop watchdog truoc, restart service, restart
watchdog cuoi. Repo `.env.example` → server `.env` van giu (gitignore).

**QUY TAC VANG — khong sua code TRUC TIEP tren server.** Lam vay tao "drift" (server
khac repo) → lan sau `git reset --hard` se **mat** thay doi do. `deploy_git.py` **tu
abort** neu phat hien tracked drift chua commit — luc do phai commit no vao repo truoc
(`git -C /opt/BotPasteDon diff` de xem). Neu buoc phai hotfix tren server, commit nguoc
lai repo ngay.

**Rollback**: `ssh root@192.168.2.220 'cd /opt/BotPasteDon && git reset --hard <commit>'`
roi restart service. (Hard-reset chi dung tracked code, runtime an toan.)

### SCP deploy (legacy — chi dung khi git khong kha dung)

```bash
# 1. Tu local machine (Windows)
scp -r scanners/ root@192.168.2.220:/opt/BotPasteDon/
scp -r shared/ root@192.168.2.220:/opt/BotPasteDon/

# 2. SSH vao server, restart service can thiet
ssh root@192.168.2.220

# Kill + restart chi scanner eldo
ps aux | grep 'scanners.main.*eldo' | grep -v grep | awk '{print $2}' | xargs kill -9
cd /opt/BotPasteDon && nohup venv/bin/python -u -m scanners.main --platform eldorado > /tmp/eldo_scanner.log 2>&1 &
```

**Canh bao SCP**: deploy tung file de tao drift va EOL-noise (CRLF/LF). Uu tien git.

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

### Duplicate ERP Sell Order (1 don marketplace -> 2 SO)

**Trieu chung**: Mot order_id tao **2 Sell Order** khac nhau tren ERP (vd. log
ca `g2g_scanner` lan `eldo_scanner` cung in `ERP accepted: <order_id> -> SO-...`
voi 2 ma SO khac nhau, trong cung 1 giay).

**Nguyen nhan (fixed 2026-06-12)**: Vong `erp_retry_loop` trong
[scanners/main.py](../scanners/main.py) chay trong **ca 2 tien trinh scanner**
(g2g + eldo) nhung `get_unsynced_orders()` **khong loc platform** → ca 2 loop
quet chung 1 bang `orders`, cung retry 1 don. Khi ERP 500 o lan POST dau (don ket
`erp_synced=0`), 2 loop tick cung luc → POST dong thoi → ERP dedup (check-then-insert,
khong atomic) tao 2 SO. Binh thuong ERP dedup bat duoc vi cac post cach nhau, chi
race khi 2 loop ban dong thoi.

**Fix da deploy**:
- `get_unsynced_orders(platform=...)` — moi scanner chi retry don cua platform minh.
- `claim_erp_order()` / `release_erp_order()` — atomic claim (`erp_synced` 0→2→1/0)
  bang conditional UPDATE; 2 tien trinh dua chi 1 thang. Reclaim claim "treo" sau 180s
  (crash-safe).
- Fix dead-code `_scanner_db` → initial-send mark synced ngay, bot 1 vong post thua.

**Don da bi nhan doi truoc fix**: fix chi chan phat sinh moi, KHONG xoa SO cu. Phai
huy thu cong 1 trong moi cap tren ERP.

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
**Workaround (capture)**: Retry logic + thread isolation cua `EldoAuth.capture()` tu retry profile khac → lan sau pass cho viec **lay cookies**.

**⚠️ Hau qua nghiem trong — thread spin 100% CPU (2026-06-12)**: Khi node driver
crash GIUA CHUNG (`Connection closed while reading from the driver`), khoi
`with Camoufox(...) as browser:` trong `_capture_single()` ([auth/main.py](../auth/main.py))
luc `__exit__` goi `browser.close()` tren connection da chet → Playwright sync
`_sync()` (`playwright/_impl/_sync_base.py`) **busy-loop vo han, giu GIL**. Thread
`ThreadPoolExecutor` do **khong bao gio ket thuc** → don 1 core lien tuc. Capture van
"pass" (profile khac), nhung thread don dep bi treo am tham. De 2 ngay → tich luy
14-17h CPU (chinh la su co "python kẹt 100%" goc).

**Chan doan**: `py-spy dump --pid <auth_pid>` → thread `active+gil` co stack
`_sync → close → __exit__ (camoufox/sync_api.py) → _capture_single (auth/main.py)`.
Thread spin **khong kill rieng duoc** (pure-Python deadlock-spin); kill chrome/camoufox
con KHONG dung lai. Mot instance spin DA xay ra (truoc fix) chi clear duoc bang restart auth.

**FIX da apply (2026-06-12)** — process isolation:
- `EldoAuth._capture_single` gio la `@staticmethod`; moi capture chay trong **subprocess
  spawn rieng** qua `_eldo_capture_isolated()` (auth/main.py) + worker o
  [`auth/_capture_proc.py`](../auth/_capture_proc.py).
- Worker `os.setsid()` → rieng process group. Parent doc ket qua qua `Queue` voi
  **timeout 200s**; neu capture treo (close-spin), parent `os.killpg(SIGKILL)` ca cay
  browser (node driver + camoufox-bin) roi rotate profile ke tiep. Hang gio bi **contain
  trong child killable**, khong con leak thread spin trong auth.
- `spawn` (khong phai fork) → khong fork service da-luong nay.
- Capture logic giu nguyen; chi boc subprocess. Verify: capture van ra cookies binh thuong,
  auth CPU=0 sau settle, `camoufox-bin` count ve 0 (khong leak). Mot proc
  `multiprocessing.resource_tracker` idle ton tai la BINH THUONG (helper cua spawn).

### G2G Scanner 401 During Extract

**Trieu chung**: Scanner mark don delivering roi bi 401 khi fetch detail → don stuck.
**Fix**: Smart retry — invalidates JWT cache, poll cho JWT moi (120s), retry 1 lan. Da implement trong `g2g_scanner_api.py`.

### Eldorado Auth — backend refresh + session re-login

**Bot dung 2-tier refresh** (Phase 4, 2026-06-08):

1. **Backend refresh (no browser, ~1s)** — `POST /api/authentication/refreshTokens`
   voi cached cookies + `x-xsrf-token` + `x-client-build-time` headers. Eldorado
   tra `Set-Cookie` chua IdToken moi. Auth thu cach nay TRUOC moi cycle (~13 min).
2. **Camoufox fallback (~30s, browser)** — chi khi backend refresh fail (vd. khi
   chua co IdToken vao session moi).

**KHONG dung AWS Cognito truc tiep**: Eldorado configured client voi secret →
`cognito-idp.us-east-2.amazonaws.com` tra `NotAuthorizedException: SECRET_HASH
was not received`.

**Health check**:
```bash
# JWT/cookie fresh?
curl -s http://localhost:8010/health | python -m json.tool

# Log refresh attempts (last 10 cycles)
grep -E "\[ELDO\] (Trying backend|backend refresh|Capture:)" /tmp/auth*.log | tail -30

# Mong doi: moi ~13 min co dong
#   "Trying backend refresh (no browser)"
#   "backend refresh OK | N cookies updated (idToken refreshed)"
#   "Backend refresh OK (api_ok=True, M cookies)"
```

**Re-login (khi RefreshToken het han ~30 ngay HOAC backend refresh + Camoufox cung fail)**:

Cookies critical:
- `__Host-EldoradoIdToken` — TTL ~1h (auto refresh moi cycle)
- `__Host-EldoradoRefreshToken` — TTL ~30 ngay (sau khi het, phai re-login VNC)
- `__Host-XSRF-TOKEN` — verify cookie

Quy trinh re-login (Camoufox visible qua VNC):

```bash
# 1. Stop watchdog + auth (tranh xung dot profile)
ssh root@192.168.2.220 'pgrep -f "watchdog.py" | xargs -r kill -9; pgrep -f "python.*auth.main" | xargs -r kill -9'

# 2. Clear profile lock + launch Camoufox visible voi profile main
ssh root@192.168.2.220 'rm -f /opt/BotPasteDon/chrome_profile_eldo/{parent.lock,.parentlock,lock}'
# QUAN TRONG: mo Camoufox VISIBLE phai dung `nohup ... &` (nhu deploy_open_eldo.py),
# KHONG dung `setsid ... </dev/null & disown` — detach kieu do lam vo pipe IPC cua
# Playwright node driver -> browser crash ngay sau khi mo (EPIPE, node:events).
ssh root@192.168.2.220 'cd /opt/BotPasteDon && DISPLAY=:99 nohup venv/bin/python -u /tmp/open_eldo_vnc_profile.py chrome_profile_eldo >/tmp/vnc_main.log 2>&1 &'

# 3. Connect VNC 192.168.2.220:5900 (pwd 123456), login Google → Eldorado
# 4. SIGTERM viewer de Camoufox SDK flush cookies cleanly
ssh root@192.168.2.220 'pgrep -f open_eldo_vnc | xargs -r kill -TERM'

# 5. Lap lai cho bak1, bak2 (deploy script open_eldo_vnc_profile.py accepts <profile_name>)
# 6. Restart auth + watchdog
ssh root@192.168.2.220 'cd /opt/BotPasteDon && HEADLESS_MODE=true setsid venv/bin/python -u -m auth.main </dev/null >/tmp/auth.log 2>&1 & disown'
ssh root@192.168.2.220 'cd /opt/BotPasteDon && setsid venv/bin/python scripts/watchdog.py </dev/null >/tmp/watchdog.log 2>&1 & disown'
```

**Verify cookies sau re-login**:
```python
import sqlite3, datetime
c = sqlite3.connect('/opt/BotPasteDon/chrome_profile_eldo/cookies.sqlite')
for name, exp in c.execute("SELECT name, expiry FROM moz_cookies WHERE name LIKE '__Host-Eldorado%'"):
    print(name, datetime.datetime.utcfromtimestamp(exp))
# Mong doi:
#   __Host-EldoradoIdToken     2026-06-08 02:25 UTC  (~30-60 phut)
#   __Host-EldoradoRefreshToken 2026-07-08 01:55 UTC  (~30 ngay)
```

### G2G Auth — backend refresh

**Bot dung 2-tier refresh** (Phase 5, 2026-06-10):

1. **Backend refresh (no browser, ~1s)** — `POST https://sls.g2g.com/user/refresh_access`
   voi body `{user_id, refresh_token, active_device_token, long_lived_token}`.
   user_id la `sub` cua JWT hien tai (decode JWT payload), 3 token con lai lay
   tu cookies cua session truoc. Response 200 `{code:2000, payload:{access_token,
   refresh_token, ...}}` chua JWT moi (15 min) + 3 token rotated. Auth thu cach
   nay TRUOC moi cycle (~13 min). `curl_cffi` impersonate `chrome120` cho TLS.

2. **Selenium fallback (~30-60s, browser)** — chi khi backend refresh fail (vd.
   lan dau khoi tao chua co cookie bundle, hoac refresh_token het han ~12 ngay
   khi khong refresh duoc lien tuc).

**Token TTL** (sliding — moi call refresh thi exp keo dai):
- `access_token` (JWT): 15 phut
- `refresh_token`: ~12 ngay (sliding) → khong bao gio het neu auth chay binh thuong
- `long_lived_token`: ~10 thang (sliding)
- `active_device_token`: ~8 thang (sliding)

Xem [docs/marketplace_auth.md](marketplace_auth.md) cho endpoint contract + JS bundle reference.

**Health check**:
```bash
# JWT fresh?
curl -s http://localhost:8010/health | python -m json.tool

# Log refresh attempts (last 10 cycles)
grep -E "\[G2G\] (Trying backend|backend refresh|JWT captured)" /tmp/auth*.log | tail -30

# Mong doi: moi ~13 min co dong
#   "Trying backend refresh (no browser)"
#   "backend refresh OK | new JWT exp=15min | 29 cookies"
# Selenium fallback chi xuat hien khi backend refresh fail:
#   "Backend refresh failed — falling back to browser"
#   "[G2G] JWT captured: eyJ... | cookies: 29 | exp: 15min"
```

**Re-login (khi refresh_token het han hoac bot bi G2G kick session)**:

Hien tuong: backend refresh tra HTTP 401/403 hoac code != 2000, Selenium
capture cung redirect ve `/login`. Khi do can re-login qua VNC.

```bash
# 1. Stop watchdog + auth
ssh root@192.168.2.220 'pgrep -f "watchdog.py" | xargs -r kill -9; pgrep -f "python.*auth.main" | xargs -r kill -9'

# 2. Clear G2G profile lock
ssh root@192.168.2.220 'rm -f /opt/BotPasteDon/chrome_profile_g2g/{SingletonLock,SingletonCookie,SingletonSocket}'

# 3. Mo Chrome visible qua Xvfb :99 (auth-login se prompt OTP qua dashboard)
ssh root@192.168.2.220 'cd /opt/BotPasteDon && HEADLESS_MODE=false setsid venv/bin/python -u -m auth.main </dev/null >/tmp/auth.log 2>&1 & disown'

# 4. Connect VNC 192.168.2.220:5900 → login g2g.com voi credential. OTP nhap qua dashboard 8766
# 5. Sau khi /auth/g2g return JWT, switch back HEADLESS_MODE=true va restart watchdog
```

### G2G Scanner — Auth service unreachable (curl timeout 30s)

**Trieu chung**:
- `/tmp/g2g_scanner.log` lap di lap lai `Auth service unreachable (attempt N): curl: (28) Operation timed out`.
- `curl http://localhost:8010/health` van tra (cached state) nhung `curl /auth/g2g` hang vo han.
- `/health` co `has_jwt: true, jwt_expires_in: 0, fresh: false` keo dai.

**Nguyen nhan**: `webdriver_manager.ChromeDriverManager().install()` mo `FileLock`
tren `~/.wdm/.wdm-lock-chromedriver-linux64`. Mot lan goi nao do (thuong
khi process khac da chiem lock cu, hoac install bi crash giua chung) leak FD
vao auth process → cac lan `init_driver()` sau tu deadlock chinh lock cua minh.
Co the verify bang `lsof /root/.wdm/.wdm-lock-chromedriver-linux64` thay
chinh PID auth giu FD do.

**Fix triet de da apply 2026-06-06** trong [`auth/main.py`](../auth/main.py):
- Them `_find_local_chromedriver()`: glob `~/.wdm/drivers/chromedriver/.../chromedriver`,
  tra ban version cao nhat.
- `_create_driver()` uu tien dung path local truc tiep → bo qua wdm hoan toan,
  khong tao file lock.
- Fallback `ChromeDriverManager().install()` chi chay khi chromedriver chua co
  (lan dau setup). Truoc fallback, defensive rm `.wdm-lock-chromedriver-linux64`.

**Fix nhanh (truoc khi fix code da apply, hoac neu fix mat hieu luc)**:
```bash
# stop watchdog + auth, kill chrome/driver, clean lock, restart
pgrep -f 'watchdog.py' | xargs -r kill -9
pgrep -f 'python.*auth.main' | xargs -r kill -9
pgrep -f camoufox-bin    | xargs -r kill -9
pgrep -f chromedriver    | xargs -r kill -9
pgrep -f chrome_profile  | xargs -r kill -9
rm -f /root/.wdm/.wdm-lock-chromedriver-linux64
# profile locks
rm -f /opt/BotPasteDon/chrome_profile_g2g/{SingletonLock,SingletonCookie,SingletonSocket}
for p in chrome_profile_eldo chrome_profile_eldo_bak1 chrome_profile_eldo_bak2; do
  rm -f /opt/BotPasteDon/$p/{parent.lock,.parentlock,lock}
done
cd /opt/BotPasteDon && HEADLESS_MODE=true setsid venv/bin/python -u -m auth.main </dev/null >/tmp/auth.log 2>&1 & disown
sleep 25
nohup venv/bin/python scripts/watchdog.py > /tmp/watchdog.log 2>&1 &
```

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

### status_sync — dong bo trang thai marketplace -> ERP

**Muc dich**: Poll trang thai don tu G2G + Eldorado moi 30 phut, push thay doi
state len ERP webhook `status_update` de cap nhat `workflow_state` cua Sell Order.
Xu ly cac case bot scanner/worker khong handle: auto-complete sau 3 ngay, dispute
sau khi complete, cancel trong khi delivering.

**Kien truc**:
- Poll cheap counts endpoint (G2G `count_my_orders`, Eldo `statesCount`).
- Khi counts thay doi (tripwire) -> fetch list don state tuong ung -> upsert vao
  `marketplace_status` (SQLite `data/orders.db`).
- Neu `prev_state != new_state` -> push len ERP `status_update`.
- Lan dau chay (DB rong) = **full backfill silent** — insert het state hien tai
  vao DB, KHONG push ERP (de tranh spam ~10k transitions gia).

**Tripwire chi tiet**:
- **G2G**: fetch `completed` + `cancelled` khi count `delivering` doi HOAC `last_order_completed_at` advance. `list_my_cases` chay **moi cycle** (khong gated, 20-page cap) → synthesize `disputed` push khi case moi mo (`prev != "open" → "open"`).
- **Eldo**: fetch state co count delta trong `{Delivered, Disputed, Completed, Canceled}`. Pagination: 1500 trang on backfill, 25 trang + early-exit sau 50 known-orders lien tiep tren incremental.

**State mapping (bot side -> ERP workflow_state)**:

| Platform state | ERP workflow_state |
|----------------|--------------------|
| g2g.completed | Completed |
| g2g.cancelled | Refunded |
| g2g.disputed (synthesize tu case open) | Disputed |
| eldo.Delivered | Delivered |
| eldo.Completed | Completed |
| eldo.Canceled | Refunded |
| eldo.Disputed | Disputed |
| eldo.Received | (ignored — same as Delivered) |
| eldo.PendingDelivery | (ignored — trader handles) |

**PROTECTED states** (ERP webhook KHONG override): `Refunded`, `Partially Refunded`,
`Cancellation Requested`, `Outstanding`, `Payment Pending`. Ly do: cac trang thai
nay do nhan vien tu set, marketplace state khong duoc de.

**ERP-side safety layers** (2026-06-10, `gege_custom/api/botpastedon.py::status_update`):

| Layer | Check | Verdict | Log level |
|-------|-------|---------|-----------|
| 1 | current ∈ PROTECTED | `protected` | Info |
| 2 | current ∈ `{"In Delivery"}` (BLOCK) | `manual_required` | **Warning** |
| 3 | (current, target) ∉ `_SAFE_TRANSITIONS` whitelist | `unsafe_transition` | **Warning** |
| 4 | All checks pass | `updated` via `db.set_value` | Info |

`_SAFE_TRANSITIONS`: `Delivered → {Completed, Disputed, Refunded}`, `Outstanding → {Completed, Refunded}`, `Completed → {Disputed}`, `Disputed → {Refunded, Completed}`. Anything else (Queued/Claimed/Evidence Uploaded/etc.) as source → `unsafe_transition`.

**Why `db.set_value` not `save()`**: webhook runs as Guest (allow_guest=True). `save()` triggers `validate_workflow → get_transitions → check_permission("read")` on the pre-save snapshot which doesn't carry `flags.ignore_permissions`, so it raises PermissionError. Safe to skip `save()` because the 4 whitelisted targets (Completed/Disputed/Refunded) have NO branch in `Sell Order.before_save`.

**Monitor operator-required cases**:

In ERP UI → WS Activity Log → filter `action = "status_update"` `status = "Warning"`. Each row carries the SO name, current→would_be transition, and the marketplace payload that triggered it.

```sql
-- Server-side equivalent
SELECT name, bot_id, detail, reference_sell_order, creation
FROM `tabWS Activity Log`
WHERE action = 'status_update' AND status = 'Warning'
ORDER BY creation DESC LIMIT 50;
```

**Health check**:
```bash
# Counts snapshot da luu chua?
venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/orders.db')
print('counts:')
for r in conn.execute('SELECT platform, state, count FROM marketplace_state_counts ORDER BY platform, state'):
    print(' ', r)
print('total statuses:', conn.execute('SELECT count(*) FROM marketplace_status').fetchone()[0])
print('pushed (last 24h):', conn.execute(\"SELECT count(*) FROM marketplace_status WHERE last_pushed_at > datetime('now', '-1 day')\").fetchone()[0])
print('disputes open:', conn.execute(\"SELECT count(*) FROM marketplace_disputes WHERE case_status='open'\").fetchone()[0])
conn.close()"

# Heartbeat
venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/orders.db')
for r in conn.execute(\"SELECT service_name, last_beat FROM heartbeat WHERE service_name='status_sync'\"):
    print(r)
conn.close()"

# Force run 1 cycle (--once) — useful sau khi sua code
cd /opt/BotPasteDon && venv/bin/python -m status_sync --once
```

**Reset / re-backfill (only if necessary)**:
```bash
# Xoa snapshot counts -> next cycle se treat la first_run = silent backfill
venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('data/orders.db')
conn.execute('DELETE FROM marketplace_state_counts')
conn.execute('DELETE FROM marketplace_status')
conn.commit()
conn.close()"
```

**Khi push ERP fail**: `push_attempts` tang, `last_pushed_at` van NULL. Next
cycle khi state van la state moi -> retry (vi prev_state trong DB van la state
cu, no thay state cu != state moi -> push lai). 5xx retries voi backoff
2/4/8s (default 3 attempts trong `ERPClient.push_status_update`); 4xx (vd. 403
Guest perm, 417 MandatoryError) bi log Warning va KHONG retry — can fix ERP
config / data quality.

**Config knobs** (`.env`):
- `STATUS_SYNC_INTERVAL_SEC` — cycle interval seconds (default 1800)
- `ERP_STATUS_UPDATE_URL` — full endpoint URL. Khong set → auto-derive tu
  `ERP_WEBHOOK_URL` bang cach thay `.new_order` cuoi cung thanh `.status_update`
- `ERP_API_KEY_G2G` / `ERP_API_KEY_ELDO` — per-platform API key, fallback ve
  `ERP_API_KEY` neu thieu

**CLI flags**:
- `python -m status_sync --interval 600` — override cycle seconds
- `python -m status_sync --once` — chay 1 cycle roi exit (testing post-deploy)

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

Tat ca scripts trong `scripts/`. Naming convention:
- **Khong prefix** = production / ops tool, run frequently.
- **Prefix `_`** = throwaway-style helper: smoke test, diagnostic, discovery template. Giu de re-use khi can.

### Server-resident — chay tren server, da co trong start.sh

| Script | Muc dich | Khi nao chay |
|--------|----------|--------------|
| [`start.sh`](../scripts/start.sh) | Start all 9 services theo thu tu phu thuoc (auth → workers → coordinator → scanners → status_sync → watchdog → dashboard). Mac dinh chay `cleanup()` truoc khi start; pass `--no-clean` de skip. | Sau reboot server hoac sau full stop |
| [`stop.sh`](../scripts/stop.sh) | Stop all 9 services in reverse order (watchdog truoc, auth cuoi). Clean chromedriver/camoufox + 4 profile locks + free 5 ports. Filter bash launcher de tranh self-match. | Truoc khi reboot hoac maintenance lon |
| [`watchdog.py`](../scripts/watchdog.py) | Long-running supervisor — check heartbeat moi 30s, restart service neu khong beat trong 90s | Luon chay (tu start.sh) |

### Client-side ops — chay tu Windows host, dung paramiko vao server

| Script | Muc dich | Output |
|--------|----------|--------|
| [`check_all_processes.py`](../scripts/check_all_processes.py) | Audit toan canh: liet ke 8 service, PID (chi count python, skip bash launcher), port, heartbeat. Bao `OK`/`DOWN`/`DUP xN`/`NO-PORT`. Cung in `/health` json. | Bang summary + verdict cuoi |
| [`deploy_auth_patch.py`](../scripts/deploy_auth_patch.py) | Deploy `auth/main.py`: upload via SFTP → backup → stop watchdog → stop auth/browsers → plant lock files de verify cleanup → start auth → trigger /auth/eldo + /auth/g2g → in audit. | Step-by-step log |
| [`deploy_workers.py`](../scripts/deploy_workers.py) | Deploy `workers/*.py` qua SFTP. Stop watchdog → kill workers → upload → start workers → restart watchdog → in audit + reachability probe. | Step log + audit table |
| [`deploy_open_eldo.py`](../scripts/deploy_open_eldo.py) | Launch Camoufox visible voi profile `chrome_profile_eldo` (main) tren Xvfb :99. Dung de xem session qua VNC. | Path log + connect info |
| [`unlock_profiles.py`](../scripts/unlock_profiles.py) | Manual fallback khi auth tat hoan toan va profile co lock cu: pkill leftover camoufox + xoa Firefox/Chrome lock files cua all profiles → trigger /auth/eldo. Sau **2026-06-04** it khi can vi auth co auto-cleanup. | Cleanup log |
| [`retry_post_evidence.py`](../scripts/retry_post_evidence.py) | Re-trigger ERP `post_evidence_to_marketplace` cho 1+ don da Completed nhung proof khong toi marketplace. Goi voi `skip_steps=['qty']` per SO. Xem muc "Tra lai bang chung". | Bang verdict per order |

### Server helpers — SCP'd len /tmp truoc khi run

| Script | Muc dich |
|--------|----------|
| [`open_eldo_vnc.py`](../scripts/open_eldo_vnc.py) | Mo Camoufox headless=False, persistent_context tren profile main, ngu vo han. SCP-deploy bang `deploy_open_eldo.py`. |
| [`open_eldo_vnc_profile.py`](../scripts/open_eldo_vnc_profile.py) | Bien the cua tren — accept profile name CLI arg (e.g. `bak1`, `bak2`). Dung trong quy trinh re-login VNC sau khi refresh_token het han. |

### Smoke tests — chay sau khi sua code de regression

| Script | Test |
|--------|------|
| [`_smoke_retry_pending.py`](../scripts/_smoke_retry_pending.py) | PR1: error classifier (auth/network/terminal/unknown), backoff schedule, DB roundtrip (mark_retry_attempt + cleanup_old_orders exemption), state transitions. |
| [`_smoke_dispatch_queue.py`](../scripts/_smoke_dispatch_queue.py) | PR2: coordinator dispatch retry queue — INSERT OR REPLACE reset, due-poll, mark_dispatch_attempt, backoff cap. |
| [`_smoke_g2g_refresh.py`](../scripts/_smoke_g2g_refresh.py) | PR3: G2G backend refresh — `_g2g_backend_refresh` guards, `_jwt_claim` decode, `G2GAuth._try_backend_refresh` no-data path. |

Chay tu Windows host: `python scripts/_smoke_<name>.py`. Khong can server, khong can network.

### Discovery templates — re-use khi can reverse-engineer marketplace moi

Su dung theo flow trong [`docs/marketplace_auth.md`](marketplace_auth.md) muc "Methodology cho marketplace moi".

| Script | Vai tro |
|--------|---------|
| [`_diag_missing_evidence.py`](../scripts/_diag_missing_evidence.py) | Pattern: query DB orders + grep `/tmp/*.log` cho 1 hoac nhieu order_id. Adapt cho debug stuck orders bat ky. |
| [`_sniff_g2g_refresh.py`](../scripts/_sniff_g2g_refresh.py) | CDP Network sniff template: clone profile, mo Chrome, navigate page authenticated, dump all request URL chua keyword auth/refresh/token. Reuse cho marketplace moi de find endpoint. |
| [`_g2g_js_grep.py`](../scripts/_g2g_js_grep.py) + [`_g2g_js_grep_remote.py`](../scripts/_g2g_js_grep_remote.py) | JS bundle decompile template: fetch tat ca `.js` cua marketplace, grep keyword. Tim exact body schema cua endpoint. |
| [`_probe_g2g_refresh.py`](../scripts/_probe_g2g_refresh.py) | Blind probe template: send GET/POST den candidate URLs voi cookies hien tai, phan biet 404/401/403/500. |
| [`_probe_refresh_access_final.py`](../scripts/_probe_refresh_access_final.py) | Confirm-endpoint template: POST voi body day du, decode response, compare JWT iat/exp tu confirm refresh thanh cong. |

### Khi nao dung script nao

```
Trien khai code moi cho auth        → deploy_auth_patch.py
Trien khai code moi cho worker      → deploy_workers.py
Kiem tra he thong dang on khong     → check_all_processes.py
Auth Eldo bi 401 mai khong khoi     → 1) check_all_processes.py
                                      2) xem /tmp/auth*.log
                                      3) restart auth (muc "Restart Auth Service")
                                      4) neu van fail: unlock_profiles.py
Muon xem profile dang trong state gi → deploy_open_eldo.py + VNC viewer
Re-login Eldo (VNC, 3 profile)        → deploy_open_eldo.py (main),
                                       open_eldo_vnc_profile.py bak1/bak2
Scanner khong tim thay don           → tail /tmp/{platform}_scanner.log + Troubleshooting
Don da Completed nhung proof khong   → retry_post_evidence.py <order_id> [...]
  toi marketplace → seller bi giu tien
Workflow_state SO khong khop         → tail /tmp/status_sync.log + muc "status_sync".
  marketplace                          Force 1 cycle: venv/bin/python -m status_sync --once
Sua code: regression check truoc khi → _smoke_retry_pending.py / _smoke_dispatch_queue.py
  deploy                               / _smoke_g2g_refresh.py
Reverse-engineer marketplace moi     → docs/marketplace_auth.md "Methodology"
                                       + _sniff_*.py + _g2g_js_grep.py + _probe_*.py
```

### Server-only legacy scripts (khong trong repo)

Mot so script da co tren `/opt/BotPasteDon/scripts/` nhung khong commit vao repo (legacy debug tools):
`check_cookies.py`, `check_detail.py`, `check_game.py`, `check_order.py`, `check_pending.py`,
`debug_kw.py`, `debug_scan.py`, `delete_order.py`, `dump_order.py`, `test_mapping.py`.

Chay truc tiep tren server: `cd /opt/BotPasteDon && venv/bin/python scripts/<name>.py`. Cac script nay khong critical cho operations — chu yeu de adhoc inspection.
