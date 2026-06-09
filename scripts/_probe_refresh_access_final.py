"""Final probe: POST sls.g2g.com/user/refresh_access with the EXACT body
the SPA constructs."""
import json
import sys

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

REMOTE = '''
import json, base64, time, requests
from curl_cffi import requests as cffi

r = requests.get("http://localhost:8010/auth/g2g", timeout=10)
d = r.json()
cookies = d.get("cookies", {})
jwt = d.get("jwt_token", "")
ua = d.get("user_agent", "")

# Decode JWT to get user_id (sub)
def decode_jwt(t):
    parts = t.split(".")
    if len(parts) != 3: return None
    pad = lambda s: s + "=" * (-len(s) % 4)
    return json.loads(base64.urlsafe_b64decode(pad(parts[1])))

claims = decode_jwt(jwt)
user_id = claims["sub"]
print(f"user_id={user_id}, current JWT iat={claims['iat']} exp={claims['exp']} ttl_left={claims['exp']-int(time.time())}s")
print()

URL = "https://sls.g2g.com/user/refresh_access"
cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
headers = {
    "user-agent": ua,
    "accept": "application/json, text/plain, */*",
    "origin": "https://www.g2g.com",
    "referer": "https://www.g2g.com/",
    "cookie": cookie_header,
    "authorization": f"Bearer {jwt}",
    "content-type": "application/json",
}

body = {
    "user_id": user_id,
    "refresh_token": cookies.get("refresh_token", ""),
    "active_device_token": cookies.get("active_device_token", ""),
    "long_lived_token": cookies.get("long_lived_token", ""),
}

print(f"POST {URL}")
print(f"body: {json.dumps(body, indent=2)}")
print()

resp = cffi.post(URL, headers=headers, json=body, timeout=15, impersonate="chrome120")
print(f"status: {resp.status_code}")
body_text = resp.text
print(f"response body (len={len(body_text)}):")
print(body_text[:2000])
print()

# If success, decode new JWT and compare
try:
    j = resp.json()
    new_jwt = ""
    payload = j.get("payload") or j
    if isinstance(payload, dict):
        new_jwt = payload.get("access_token") or payload.get("accessToken") or payload.get("jwt_token") or ""
    if new_jwt:
        new_claims = decode_jwt(new_jwt)
        print(f"NEW JWT iat={new_claims['iat']} exp={new_claims['exp']} sub={new_claims['sub']}")
        print(f"DIFF from current: iat_delta={new_claims['iat'] - claims['iat']}s exp_delta={new_claims['exp'] - claims['exp']}s")
        print()
        print(f"NEW JWT new ttl: {new_claims['exp'] - int(time.time())}s")
except Exception as e:
    print(f"json parse / extract err: {e}")
'''

s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.2.220", username="root", password="123456", timeout=15)

sftp = s.open_sftp()
with sftp.open("/tmp/_g2g_final.py", "w") as f:
    f.write(REMOTE)
sftp.close()

_, o, e = s.exec_command("/opt/BotPasteDon/venv/bin/python /tmp/_g2g_final.py", timeout=60)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("STDERR:")
    print(err[:2000])
s.close()
