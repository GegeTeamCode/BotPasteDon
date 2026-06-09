"""Probe candidate G2G backend-refresh endpoints.

Strategy:
  1. Fetch fresh G2G auth bundle from auth service on 220.
  2. For each candidate endpoint, send a GET first (safest — wrong
     endpoint returns 404/405, no harm). Inspect status, Set-Cookie,
     body preview.
  3. If any candidate looks alive (200/40x with auth-shaped body),
     follow up with a careful POST.
  4. Compare CURRENT JWT with any new Authorization-bearing JWT in
     Set-Cookie to detect actual refresh.

Risk mitigation:
  - Only one POST per candidate, no body unless GET hinted at one.
  - All probes use curl_cffi chrome120 impersonation (matches what the
    bot already does, so we don't poison the TLS fingerprint).
  - We do NOT call this in a loop. Single run.
"""
import json
import sys

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REMOTE_SCRIPT = r"""
import json, requests
from curl_cffi import requests as cffi

# 1. Pull fresh auth bundle
r = requests.get("http://localhost:8010/auth/g2g", timeout=10)
d = r.json()
cookies = d.get("cookies", {})
jwt = d.get("jwt_token", "")
ua = d.get("user_agent", "")

print("Have JWT prefix:", jwt[:30])
print("Have", len(cookies), "cookies; key auth cookies:")
for k in ("refresh_token", "long_lived_token", "active_device_token"):
    v = cookies.get(k, "")
    print("  ", k, "=", (v[:40] + "...") if len(v) > 40 else v)
print()

cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
base_headers = {
    "user-agent": ua,
    "accept": "application/json, text/plain, */*",
    "origin": "https://www.g2g.com",
    "referer": "https://www.g2g.com/g2g-user/sale?status=preparing",
    "cookie": cookie_header,
    "authorization": f"Bearer {jwt}",  # include current JWT — many refresh endpoints require it
}

CANDIDATES = [
    # sls subdomain (current API host)
    "https://sls.g2g.com/user/refresh_jwt",
    "https://sls.g2g.com/user/refresh",
    "https://sls.g2g.com/auth/refresh",
    "https://sls.g2g.com/auth/refresh_token",
    "https://sls.g2g.com/token/refresh",
    "https://sls.g2g.com/user/me",
    "https://sls.g2g.com/user/jwt",
    "https://sls.g2g.com/user/session",
    # www subdomain
    "https://www.g2g.com/api/refresh",
    "https://www.g2g.com/api/user/refresh",
    "https://www.g2g.com/api/auth/refresh",
    "https://www.g2g.com/api/token/refresh",
    # api subdomain (guess)
    "https://api.g2g.com/v1/auth/refresh",
    "https://api.g2g.com/auth/refresh",
]

def fmt_setcookie(resp):
    sc = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else None
    if not sc:
        sc_single = resp.headers.get("set-cookie", "")
        sc = [sc_single] if sc_single else []
    out = []
    for c in sc:
        head = c.split(";", 1)[0]
        out.append(head)
    return out

def shorten(s, n=180):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "...(len=" + str(len(s)) + ")"

print("=" * 78)
print("PROBE 1: GET each candidate (safest discovery)")
print("=" * 78)
get_findings = {}
for url in CANDIDATES:
    try:
        resp = cffi.get(url, headers=base_headers, timeout=10, impersonate="chrome120")
        sc = fmt_setcookie(resp)
        body = shorten(resp.text)
        new_refresh = any("refresh_token=" in c for c in sc)
        new_jwt_in_body = "jwt" in body.lower() or "access_token" in body.lower() or "id_token" in body.lower()
        flag = ""
        if resp.status_code == 200:
            flag = " <-- 200 OK"
        elif resp.status_code in (405,):
            flag = " (method not allowed, but endpoint EXISTS)"
        if new_refresh:
            flag += " <-- SET-COOKIE WITH refresh_token!"
        if new_jwt_in_body:
            flag += " <-- BODY mentions jwt/token!"
        print(f"GET  {url}")
        print(f"  -> {resp.status_code}{flag}")
        if sc:
            print(f"  set-cookie: {sc}")
        print(f"  body: {body}")
        get_findings[url] = (resp.status_code, sc, body)
    except Exception as e:
        print(f"GET  {url} -> EXC: {type(e).__name__}: {e}")
    print()

print("=" * 78)
print("PROBE 2: POST endpoints that returned 405 on GET (method exists, want POST)")
print("=" * 78)
for url, (status, sc, body) in get_findings.items():
    if status not in (405, 200):
        continue
    # 200 may already be the refresh — still try POST with refresh_token body
    if "/me" in url or "/session" in url:
        continue  # these are likely just info endpoints
    body_json = {"refresh_token": cookies.get("refresh_token", "")}
    try:
        resp = cffi.post(url, headers={**base_headers, "content-type": "application/json"},
                         json=body_json, timeout=10, impersonate="chrome120")
        sc = fmt_setcookie(resp)
        body = shorten(resp.text)
        print(f"POST {url}  body={{refresh_token: ...}}")
        print(f"  -> {resp.status_code}")
        if sc:
            print(f"  set-cookie: {sc}")
        print(f"  body: {body}")
        print()
    except Exception as e:
        print(f"POST {url} -> EXC: {type(e).__name__}: {e}")
        print()

print("=" * 78)
print("PROBE 3: empty-body POST (Eldo-style — server reads cookie)")
print("=" * 78)
for url in CANDIDATES:
    if "/me" in url or "/session" in url or "/jwt" == url[-4:]:
        continue
    try:
        resp = cffi.post(url, headers={**base_headers, "content-type": "application/json"},
                         data="{}", timeout=10, impersonate="chrome120")
        sc = fmt_setcookie(resp)
        body = shorten(resp.text)
        flag = ""
        if resp.status_code == 200:
            flag = " <-- 200 OK"
        if any("refresh_token=" in c or "long_lived_token=" in c for c in sc):
            flag += " <-- TOKEN ROTATED IN COOKIE!"
        if "jwt" in body.lower() or "access_token" in body.lower():
            flag += " <-- BODY contains jwt/token!"
        print(f"POST {url}  body={{}}")
        print(f"  -> {resp.status_code}{flag}")
        if sc:
            print(f"  set-cookie: {sc}")
        print(f"  body: {body}")
        print()
    except Exception as e:
        print(f"POST {url} -> EXC: {type(e).__name__}: {e}")
        print()
"""

s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.2.220", username="root", password="123456", timeout=15)

sftp = s.open_sftp()
with sftp.open("/tmp/_g2g_refresh_probe.py", "w") as f:
    f.write(REMOTE_SCRIPT)
sftp.close()

_, o, e = s.exec_command(
    "/opt/BotPasteDon/venv/bin/python /tmp/_g2g_refresh_probe.py",
    timeout=300,
)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("STDERR:")
    print(err[:3000])
s.close()
