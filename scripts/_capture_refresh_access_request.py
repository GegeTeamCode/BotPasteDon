"""Re-run a focused CDP sniff and dump the FULL request that succeeds at
POST sls.g2g.com/user/refresh_access — body, all headers, response body.
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
if os.path.exists(DST):
    shutil.rmtree(DST, ignore_errors=True)
shutil.copytree(
    SRC, DST,
    ignore=shutil.ignore_patterns("Singleton*", "*lock", "*Lock", ".parentlock", "parent.lock"),
    ignore_dangling_symlinks=True,
)

opts = Options()
opts.binary_location = "/usr/bin/google-chrome"
opts.add_argument("--headless=new")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-dev-shm-usage")
opts.add_argument(f"--user-data-dir={DST}")
opts.add_argument("--profile-directory=Default")
opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

import glob
cd = sorted(glob.glob("/root/.wdm/drivers/chromedriver/linux64/*/chromedriver-linux64/chromedriver"))
driver = webdriver.Chrome(service=Service(cd[-1]) if cd else Service(), options=opts)
driver.execute_cdp_cmd("Network.enable", {})

try:
    driver.get("https://www.g2g.com/g2g-user/sale?status=preparing")
except Exception as e:
    print("nav exc:", e)
time.sleep(15)

# Pull logs
logs = driver.get_log("performance")

# Build maps
reqs = {}            # rid -> details
extra_headers = {}   # rid -> headers from requestWillBeSentExtraInfo
post_data = {}       # rid -> postData
resps = {}           # rid -> status / headers
resp_bodies = {}     # rid -> body via CDP Network.getResponseBody

for e in logs:
    try:
        m = json.loads(e["message"])["message"]
    except Exception:
        continue
    method = m.get("method", "")
    p = m.get("params", {})
    rid = p.get("requestId", "")
    if not rid:
        continue
    if method == "Network.requestWillBeSent":
        req = p.get("request", {})
        reqs[rid] = {
            "url": req.get("url", ""),
            "method": req.get("method", ""),
            "headers": req.get("headers", {}),
            "postData": req.get("postData", ""),
        }
    elif method == "Network.requestWillBeSentExtraInfo":
        # this contains the FULL headers including auto-set ones
        extra_headers[rid] = p.get("headers", {})
    elif method == "Network.responseReceived":
        r = p.get("response", {})
        resps[rid] = {
            "status": r.get("status"),
            "headers": r.get("headers", {}),
        }

# Find requests to /user/refresh_access
target = "sls.g2g.com/user/refresh_access"
found = [rid for rid, rdat in reqs.items() if target in rdat["url"]]
print(f"Found {len(found)} requests to {target}")
print()

for i, rid in enumerate(found):
    rdat = reqs[rid]
    print(f"=== Request {i+1} (requestId={rid}) ===")
    print(f"method: {rdat['method']}")
    print(f"url:    {rdat['url']}")
    print(f"postData (from requestWillBeSent): {rdat['postData']!r}")
    print(f"headers (basic):")
    for k, v in sorted(rdat["headers"].items()):
        print(f"  {k}: {v[:120]}")
    if rid in extra_headers:
        print(f"headers (full incl auto-set from extraInfo):")
        for k, v in sorted(extra_headers[rid].items()):
            if k.lower() == "cookie":
                print(f"  {k}: <{len(v)} chars>")
            else:
                print(f"  {k}: {v[:120]}")
    if rid in resps:
        print(f"response status: {resps[rid]['status']}")
        rh = resps[rid]['headers']
        for hk in ("set-cookie", "Set-Cookie", "content-type", "content-length"):
            if rh.get(hk):
                v = rh[hk][:200]
                print(f"  {hk}: {v}")
    # Pull body via CDP
    try:
        body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid})
        b = body.get("body", "")
        if body.get("base64Encoded"):
            import base64
            b = base64.b64decode(b).decode("utf-8", errors="replace")
        print(f"response body (len={len(b)}):")
        print(f"  {b[:2000]}")
    except Exception as e:
        print(f"  body fetch err: {e}")
    print()

driver.quit()
"""

s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.2.220", username="root", password="123456", timeout=15)

print("Pausing watchdog to free profile lock...")
s.exec_command("pgrep -f 'watchdog.py' | xargs -r kill -9")
import time as _t
_t.sleep(2)

sftp = s.open_sftp()
with sftp.open("/tmp/_g2g_capture.py", "w") as f:
    f.write(REMOTE)
sftp.close()

_, o, e = s.exec_command("/opt/BotPasteDon/venv/bin/python /tmp/_g2g_capture.py", timeout=180)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("STDERR:")
    print(err[:3000])

print("\nRestarting watchdog...")
s.exec_command(
    "cd /opt/BotPasteDon && nohup venv/bin/python scripts/watchdog.py "
    "</dev/null >/tmp/watchdog.log 2>&1 & disown"
)
_t.sleep(2)
s.close()
