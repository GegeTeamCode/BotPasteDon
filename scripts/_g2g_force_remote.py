"""Remote-side script — runs on 220, opens Chrome, force-fetches refresh_access."""
import base64
import glob
import json
import os
import shutil
import time

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

cd = sorted(glob.glob("/root/.wdm/drivers/chromedriver/linux64/*/chromedriver-linux64/chromedriver"))
driver = webdriver.Chrome(service=Service(cd[-1]) if cd else Service(), options=opts)
driver.execute_cdp_cmd("Network.enable", {})

try:
    driver.get("https://www.g2g.com/g2g-user/sale?status=preparing")
except Exception as e:
    print("nav exc:", e)
time.sleep(10)

# Discover what's in storage (JWT might be there)
JS_DUMP = (
    "var stash = {ls:{}, ss:{}}; "
    "try { for (var k of Object.keys(localStorage)) { stash.ls[k] = (localStorage.getItem(k)||'').slice(0,300); } } catch(e) {} "
    "try { for (var k of Object.keys(sessionStorage)) { stash.ss[k] = (sessionStorage.getItem(k)||'').slice(0,300); } } catch(e) {} "
    "return JSON.stringify(stash);"
)
storage_dump = driver.execute_script(JS_DUMP)
print("Storage dump (truncated):")
print(storage_dump[:1500])
print()

# Force a fetch from page context — credentials:include auto-attaches cookies.
JS_FETCH = (
    "var done = arguments[arguments.length - 1]; "
    "(async () => { "
    "  var results = {}; "
    "  var jwt = ''; "
    "  try { "
    "    for (var k of Object.keys(localStorage)) { "
    "      var v = localStorage.getItem(k) || ''; "
    "      var m = v.match(/eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+/); "
    "      if (m) { jwt = m[0]; break; } "
    "    } "
    "  } catch(e) {} "
    "  results.jwt_found = jwt ? jwt.slice(0, 30) + '...' : ''; "
    "  var tests = [ "
    "    {name: 'A_empty', headers: {'content-type':'application/json'}, body: '{}'}, "
    "    {name: 'B_nobody', headers: {}, body: undefined}, "
    "    {name: 'C_auth', headers: {'content-type':'application/json', 'authorization':'Bearer '+jwt}, body: '{}'} "
    "  ]; "
    "  for (var t of tests) { "
    "    try { "
    "      var opt = {method:'POST', credentials:'include', headers:t.headers}; "
    "      if (t.body !== undefined) opt.body = t.body; "
    "      var r = await fetch('https://sls.g2g.com/user/refresh_access', opt); "
    "      var text = await r.text(); "
    "      results[t.name] = {status: r.status, body: text.slice(0, 800), len: text.length}; "
    "    } catch(e) { results[t.name] = {error: String(e)}; } "
    "  } "
    "  done(JSON.stringify(results)); "
    "})();"
)
driver.set_script_timeout(60)
try:
    fetch_result = driver.execute_async_script(JS_FETCH)
    print("Fetch results (from page context):")
    try:
        parsed = json.loads(fetch_result)
        print(json.dumps(parsed, indent=2)[:3500])
    except Exception:
        print(fetch_result[:3500])
except Exception as e:
    print("execute_async err:", e)
print()

time.sleep(2)
logs = driver.get_log("performance")

reqs = {}
resps = {}
for e in logs:
    try:
        m = json.loads(e["message"])["message"]
    except Exception:
        continue
    p = m.get("params", {})
    rid = p.get("requestId", "")
    if not rid:
        continue
    if m.get("method") == "Network.requestWillBeSent":
        req = p.get("request", {})
        reqs[rid] = {
            "url": req.get("url", ""), "method": req.get("method", ""),
            "headers": req.get("headers", {}), "postData": req.get("postData", ""),
        }
    elif m.get("method") == "Network.responseReceived":
        r = p.get("response", {})
        resps[rid] = {"status": r.get("status"), "headers": r.get("headers", {})}

found = [rid for rid, rdat in reqs.items() if "sls.g2g.com/user/refresh_access" in rdat["url"]]
print(f"=== CDP captured {len(found)} requests to /user/refresh_access ===")
for i, rid in enumerate(found):
    rdat = reqs[rid]
    print(f"\n--- Captured request {i+1} ---")
    print(f"method: {rdat['method']}")
    print(f"url:    {rdat['url']}")
    print(f"postData: {rdat['postData']!r}")
    print("headers:")
    for k, v in sorted(rdat["headers"].items()):
        if k.lower() == "cookie":
            print(f"  {k}: <{len(v)} chars>")
        else:
            print(f"  {k}: {v[:160]}")
    if rid in resps:
        print(f"response status: {resps[rid]['status']}")
        rh = resps[rid]['headers']
        for hk in ("set-cookie", "Set-Cookie", "content-type"):
            if rh.get(hk):
                print(f"  {hk}: {rh[hk][:200]}")
        try:
            body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid})
            b = body.get("body", "")
            if body.get("base64Encoded"):
                b = base64.b64decode(b).decode("utf-8", errors="replace")
            print(f"response body (len={len(b)}):")
            print(f"  {b[:1500]}")
        except Exception as e:
            print(f"  body err: {e}")

driver.quit()
