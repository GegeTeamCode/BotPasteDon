"""Confirm sls.g2g.com/user/refresh_access works as G2G's backend JWT refresh
endpoint. Probe with several body/header combos and report response body.
"""
import paramiko
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REMOTE = r"""
import json, base64, requests
from curl_cffi import requests as cffi

r = requests.get("http://localhost:8010/auth/g2g", timeout=10)
d = r.json()
cookies = d.get("cookies", {})
jwt = d.get("jwt_token", "")
ua = d.get("user_agent", "")

print("Current JWT prefix:", jwt[:30])
print()

def decode_jwt(t):
    if not t: return None
    parts = t.split(".")
    if len(parts) != 3: return None
    pad = lambda s: s + "=" * (-len(s) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(pad(parts[1])))
    except Exception:
        return None

cur = decode_jwt(jwt)
if cur:
    import time
    print(f"Current claims: iat={cur.get('iat')} exp={cur.get('exp')} sub={cur.get('sub')} ttl_left={cur.get('exp', 0) - int(time.time())}s")
print()

URL = "https://sls.g2g.com/user/refresh_access"
cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
base_h = {
    "user-agent": ua,
    "accept": "application/json, text/plain, */*",
    "origin": "https://www.g2g.com",
    "referer": "https://www.g2g.com/g2g-user/sale?status=preparing",
    "cookie": cookie_header,
    "authorization": f"Bearer {jwt}",
    "content-type": "application/json",
}

# Probe 1: empty body
print("=" * 78)
print("PROBE 1: POST refresh_access  body={}")
print("=" * 78)
try:
    resp = cffi.post(URL, headers=base_h, data="{}", timeout=15, impersonate="chrome120")
    print(f"  status: {resp.status_code}")
    body = resp.text
    print(f"  body (len={len(body)}): {body[:600]}")
    # Set-cookie?
    sc = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else []
    if not sc:
        sc1 = resp.headers.get("set-cookie")
        sc = [sc1] if sc1 else []
    if sc:
        print(f"  set-cookie: {[c.split(';', 1)[0] for c in sc]}")
    # Try parse JSON
    try:
        j = resp.json()
        print(f"  json keys: {list(j.keys())}")
        for k in ("jwt_token", "jwt", "access_token", "token", "id_token", "code", "payload", "result"):
            if k in j:
                v = j[k]
                if isinstance(v, str) and len(v) > 40:
                    print(f"    {k}: {v[:40]}...({len(v)})")
                else:
                    print(f"    {k}: {v}")
        # Check if nested response has token
        for k in ("payload", "data", "result"):
            if isinstance(j.get(k), dict):
                inner = j[k]
                print(f"    {k} keys: {list(inner.keys())}")
                for ik in ("jwt_token", "jwt", "access_token", "token"):
                    if ik in inner:
                        v = inner[ik]
                        new_claims = decode_jwt(v) if isinstance(v, str) else None
                        print(f"      {ik}: {(v[:40] + '...') if isinstance(v, str) and len(v) > 40 else v}")
                        if new_claims:
                            print(f"        new_claims iat={new_claims.get('iat')} exp={new_claims.get('exp')} sub={new_claims.get('sub')}")
                            print(f"        DIFFERENT FROM CURRENT? {new_claims.get('iat') != cur.get('iat')}")
    except Exception as e:
        print(f"  not json: {e}")
except Exception as e:
    print(f"  EXC: {e}")

# Probe 2: with refresh_token in body
print()
print("=" * 78)
print("PROBE 2: POST refresh_access  body={refresh_token: ...}")
print("=" * 78)
try:
    body = json.dumps({"refresh_token": cookies.get("refresh_token", "")})
    resp = cffi.post(URL, headers=base_h, data=body, timeout=15, impersonate="chrome120")
    print(f"  status: {resp.status_code}")
    print(f"  body: {resp.text[:600]}")
except Exception as e:
    print(f"  EXC: {e}")

# Probe 3: NO Authorization header — rely on cookies only
print()
print("=" * 78)
print("PROBE 3: POST refresh_access  no Authorization header")
print("=" * 78)
try:
    h = {k: v for k, v in base_h.items() if k != "authorization"}
    resp = cffi.post(URL, headers=h, data="{}", timeout=15, impersonate="chrome120")
    print(f"  status: {resp.status_code}")
    print(f"  body: {resp.text[:600]}")
except Exception as e:
    print(f"  EXC: {e}")
"""

s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.2.220", username="root", password="123456", timeout=15)

sftp = s.open_sftp()
with sftp.open("/tmp/_g2g_ra_probe.py", "w") as f:
    f.write(REMOTE)
sftp.close()

_, o, e = s.exec_command("/opt/BotPasteDon/venv/bin/python /tmp/_g2g_ra_probe.py", timeout=120)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("STDERR:")
    print(err[:2000])
s.close()
