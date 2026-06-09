"""Fetch the g2g.com SPA JS bundle and grep for refresh_access usage.

Runs on 220. Use chromedriver/Chrome to capture all JS bundle URLs from
g2g.com page load, then download each via requests and grep for the
key string. Print surrounding ~600 chars for hits.
"""
import glob
import json
import os
import re
import shutil
import time

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

SRC = "/opt/BotPasteDon/chrome_profile_g2g"
DST = "/tmp/chrome_profile_g2g_jsgrep"
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

driver.get("https://www.g2g.com/g2g-user/sale?status=preparing")
time.sleep(15)
logs = driver.get_log("performance")
driver.quit()

js_urls = set()
for e in logs:
    try:
        m = json.loads(e["message"])["message"]
    except Exception:
        continue
    if m.get("method") != "Network.responseReceived":
        continue
    r = m["params"].get("response", {})
    url = r.get("url", "")
    mime = r.get("mimeType", "")
    if ".js" not in url:
        continue
    if any(x in url for x in ("google", "doubleclick", "facebook", "tiktok", "snap",
                              "clarity.ms", "bing", "datadog", "youtube", "gtm",
                              "/recaptcha/", "twitter")):
        continue
    if mime and "javascript" not in mime and "ecmascript" not in mime:
        continue
    js_urls.add(url)

print(f"Captured {len(js_urls)} JS bundle URLs")
for u in sorted(js_urls)[:50]:
    print(" ", u[:120])
print()

# Download & grep
NEEDLES = (
    "refresh_access", "refresh_token", "long_lived_token",
    "active_device_token", "accessToken",
)
ua = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")
sess = requests.Session()
sess.headers.update({"user-agent": ua})

for url in sorted(js_urls):
    try:
        r = sess.get(url, timeout=20)
        if r.status_code != 200:
            continue
        text = r.text
    except Exception as ex:
        print(f"  fetch err {url[:80]}: {ex}")
        continue
    hits = []
    for needle in NEEDLES:
        for m in re.finditer(re.escape(needle), text):
            hits.append((m.start(), needle))
    if not hits:
        continue
    print()
    print(f"=== {url} ({len(text)} bytes, {len(hits)} hits) ===")
    seen_spans = []
    for pos, needle in sorted(hits)[:8]:
        # de-dup overlapping
        if any(abs(pos - sp) < 200 for sp in seen_spans):
            continue
        seen_spans.append(pos)
        start = max(0, pos - 250)
        end = min(len(text), pos + 350)
        snippet = text[start:end]
        snippet = re.sub(r"\s+", " ", snippet)
        print(f"  [+{pos}] needle={needle}")
        print(f"    ...{snippet}...")
