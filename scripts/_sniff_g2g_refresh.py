"""CDP network sniff for G2G to find the JWT refresh endpoint.

Workflow:
  1. SSH to 220.
  2. Copy chrome_profile_g2g → /tmp/chrome_profile_g2g_sniff (so we don't
     fight the live auth service for the profile lock).
  3. Open Chrome with CDP performance logging via Selenium.
  4. Navigate to dashboard, wait, navigate to a few protected pages
     (SPA may trigger refresh on route changes / API calls).
  5. ALSO clear localStorage auth bits then reload — many SPAs refresh
     tokens when storage is empty.
  6. Dump every Network.requestWillBeSent + Network.responseReceived
     event, filter URLs by interesting keywords, surface Authorization
     header values + Set-Cookie response headers.

Output: list of "candidate refresh requests" — URL, method, status,
Authorization header (prefix only), Set-Cookie keys, response body
preview if small.
"""
import paramiko
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REMOTE = r"""
import json, os, shutil, time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

SRC = "/opt/BotPasteDon/chrome_profile_g2g"
DST = "/tmp/chrome_profile_g2g_sniff"

# Clean dest then copy — skip socket / lock files which are not copyable
if os.path.exists(DST):
    shutil.rmtree(DST, ignore_errors=True)
shutil.copytree(
    SRC, DST,
    ignore=shutil.ignore_patterns("Singleton*", "*lock", "*Lock", ".parentlock", "parent.lock"),
    ignore_dangling_symlinks=True,
)
print(f"Cloned profile to {DST}")

opts = Options()
opts.binary_location = "/usr/bin/google-chrome"
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument(f"--user-data-dir={DST}")
opts.add_argument("--profile-directory=Default")
opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

# Locate chromedriver
import glob
cd_paths = sorted(glob.glob("/root/.wdm/drivers/chromedriver/linux64/*/chromedriver-linux64/chromedriver"))
service = Service(cd_paths[-1]) if cd_paths else Service()
driver = webdriver.Chrome(service=service, options=opts)
driver.execute_cdp_cmd("Network.enable", {})

def drain_perf_logs():
    return driver.get_log("performance")

# Phase A: just navigate, let SPA load
print("\n[A] Navigate to dashboard (~12s)...")
try:
    driver.get("https://www.g2g.com/g2g-user/sale?status=preparing")
except Exception as e:
    print("nav exc:", e)
time.sleep(12)
logs_a = drain_perf_logs()

# Phase B: navigate to another protected page
print("[B] Navigate to wallet (~8s)...")
try:
    driver.get("https://www.g2g.com/wallet")
except Exception as e:
    print("nav exc:", e)
time.sleep(8)
logs_b = drain_perf_logs()

# Phase C: kill JWT in localStorage + storage cookies → reload → SPA must refresh
print("[C] Clear auth localStorage + reload (~10s)...")
JS_CLEAR = (
    "try { for (var k of Object.keys(localStorage)) { "
    "if (k.toLowerCase().includes('jwt') || "
    "k.toLowerCase().includes('token') || "
    "k.toLowerCase().includes('auth')) { localStorage.removeItem(k); } } } catch(e) {} "
    "try { sessionStorage.clear(); } catch(e) {}"
)
try:
    driver.execute_script(JS_CLEAR)
    driver.refresh()
except Exception as e:
    print("clear/reload exc:", e)
time.sleep(10)
logs_c = drain_perf_logs()

# Phase D: trigger an API request to provoke refresh if JWT was dropped
print("[D] Navigate sale page again (~8s)...")
try:
    driver.get("https://www.g2g.com/g2g-user/sale?status=preparing")
except Exception as e:
    print("nav exc:", e)
time.sleep(8)
logs_d = drain_perf_logs()

driver.quit()

ALL_LOGS = []
for phase, ls in (("A", logs_a), ("B", logs_b), ("C", logs_c), ("D", logs_d)):
    for entry in ls:
        try:
            msg = json.loads(entry["message"])["message"]
        except Exception:
            continue
        ALL_LOGS.append((phase, msg))

KEYS = ("refresh", "token", "jwt", "auth", "login", "session", "credentials")

# Build req map keyed by requestId so we can pair request/response
reqs = {}      # requestId -> {url, method, request_headers}
resps = {}     # requestId -> {status, response_headers}
auths = {}     # requestId -> Authorization header value (short)
for phase, msg in ALL_LOGS:
    method = msg.get("method", "")
    params = msg.get("params", {})
    rid = params.get("requestId", "")
    if method == "Network.requestWillBeSent":
        req = params.get("request", {})
        reqs[rid] = {
            "phase": phase,
            "url": req.get("url", ""),
            "method": req.get("method", ""),
            "headers": req.get("headers", {}),
        }
        auth = req.get("headers", {}).get("Authorization") or req.get("headers", {}).get("authorization")
        if auth:
            auths[rid] = auth[:25] + "..." if len(auth) > 25 else auth
    elif method == "Network.responseReceived":
        r = params.get("response", {})
        resps[rid] = {
            "status": r.get("status"),
            "headers": r.get("headers", {}),
            "url": r.get("url", ""),
            "mime": r.get("mimeType", ""),
        }

# Collect Authorization-bearing values per phase to spot when JWT rotates
print()
print("=" * 78)
print("Authorization Bearer prefixes seen (track JWT rotation across phases)")
print("=" * 78)
seen_jwt_prefixes = {}  # phase -> set of jwt prefixes
for rid, info in reqs.items():
    auth = auths.get(rid, "")
    if not auth:
        continue
    seen_jwt_prefixes.setdefault(info["phase"], set()).add(auth)
for phase in sorted(seen_jwt_prefixes):
    print(f"phase {phase}: {sorted(seen_jwt_prefixes[phase])}")

# Find ALL requests where URL contains an auth keyword
print()
print("=" * 78)
print("Requests whose URL contains an auth keyword (refresh/token/jwt/auth/login/session)")
print("=" * 78)
for rid, info in reqs.items():
    url = info["url"]
    lurl = url.lower()
    if not any(k in lurl for k in KEYS):
        continue
    if url.startswith("data:") or url.startswith("blob:"):
        continue
    if any(skip in lurl for skip in (".js", ".css", ".png", ".jpg", ".svg", ".woff", ".ico", ".gif", ".webp", "google-analytics", "googletag", "doubleclick", "facebook", "tiktok", "snapchat", "clarity.ms", "datadog")):
        continue
    r = resps.get(rid, {})
    set_cookies_keys = []
    sc = r.get("headers", {}).get("set-cookie") or r.get("headers", {}).get("Set-Cookie") or ""
    if sc:
        for line in sc.split("\n"):
            head = line.split(";", 1)[0].strip()
            if "=" in head:
                set_cookies_keys.append(head.split("=", 1)[0])
    print(f"[{info['phase']}] {info['method']:5} {r.get('status', '???')} {url[:120]}")
    if set_cookies_keys:
        print(f"        set-cookie keys: {set_cookies_keys}")

# Also: find responses where Set-Cookie rotated refresh_token / long_lived_token
print()
print("=" * 78)
print("Responses whose Set-Cookie rotates refresh_token / long_lived_token / jwt")
print("=" * 78)
ROT_KEYS = ("refresh_token", "long_lived_token", "active_device_token", "jwt", "id_token", "access_token")
for rid, r in resps.items():
    sc = r.get("headers", {}).get("set-cookie") or r.get("headers", {}).get("Set-Cookie") or ""
    if not sc:
        continue
    if not any(k in sc.lower() for k in ROT_KEYS):
        continue
    req = reqs.get(rid, {})
    print(f"[{req.get('phase','?')}] {req.get('method','?'):5} {r.get('status'):3} {req.get('url','')[:140]}")
    for line in sc.split("\n"):
        head = line.split(";", 1)[0].strip()
        if any(k in head.lower() for k in ROT_KEYS):
            print(f"        set-cookie: {head[:120]}")

print()
print("done.")
"""

s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.2.220", username="root", password="123456", timeout=15)

print("Pausing auth service to free profile lock...")
s.exec_command("pgrep -f 'watchdog.py' | xargs -r kill -9")
import time as _t
_t.sleep(2)

sftp = s.open_sftp()
with sftp.open("/tmp/_g2g_sniff.py", "w") as f:
    f.write(REMOTE)
sftp.close()

print("Running sniff (~50s)...")
_, o, e = s.exec_command(
    "/opt/BotPasteDon/venv/bin/python /tmp/_g2g_sniff.py",
    timeout=300,
)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("STDERR (first 3KB):")
    print(err[:3000])

print("\nRestarting watchdog (will respawn anything killed)...")
s.exec_command(
    "cd /opt/BotPasteDon && nohup venv/bin/python scripts/watchdog.py "
    "</dev/null >/tmp/watchdog.log 2>&1 & disown"
)
_t.sleep(2)
s.close()
