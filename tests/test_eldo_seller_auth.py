"""Eldorado Seller API auth (ELDO_AUTH_MODE=seller_api) — chay ma that cua auth/main.py va
shared/eldo_auth.py voi mot Eldorado GIA tren localhost.

Khong goi ra ngoai, khong mo trinh duyet, khong dung credential that, khong can server .220.
Can cac goi trong requirements.txt (aiohttp, selenium, curl_cffi, ...).

    python tests/test_eldo_seller_auth.py        # in PASS/FAIL tung phep kiem, exit 1 neu co FAIL
"""
import asyncio, base64, io, json, logging, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "S3CRET-khong-duoc-lo-ra-log-0123456789abcdef"
CID = "11111111-2222-3333-4444-555555555555"
os.environ.update(ELDO_AUTH_MODE="seller_api", ELDO_SELLER_CLIENT_ID=CID, ELDO_SELLER_CLIENT_SECRET=SECRET)
sys.path.insert(0, REPO)
os.chdir(REPO)

def b64(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
def make_jwt(): return "h." + b64({"exp": int(time.time()) + 900, "userId": "seller-uuid-1", "sub": CID}) + ".sig"

STATE = {"token": "ok", "probe": 200, "hits": [], "last_token": "", "bundle": {}}
class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{}")
        STATE["hits"].append(("POST", self.path))
        if self.path == "/api/authentication/seller/token":
            assert body == {"clientId": CID, "clientSecret": SECRET}, body
            if STATE["token"] == "ok":
                STATE["last_token"] = make_jwt()
                return self._send(200, {"accessToken": STATE["last_token"], "expiresIn": 899, "tokenType": "Bearer"})
            if STATE["token"] == 401: return self._send(401, {"code": 401, "messages": ["Authentication failed."], "correlationId": None})
            return self._send(int(STATE["token"]), {"code": STATE["token"]})
        self._send(404, {})
    def do_GET(self):
        STATE["hits"].append(("GET", self.path))
        authed = self.headers.get("Authorization") == "Bearer " + STATE["last_token"]
        if self.path == "/api/v1/orders/me/seller-api-eligibility":
            if STATE["probe"] != 200: return self._send(STATE["probe"], {})
            return self._send(200 if authed else 401, {"isEligible": True})
        if self.path == "/api/client-credentials":
            return self._send(200 if authed else 401, [
                {"clientId": "khac", "expirationDate": "2026-01-01T00:00:00.0000000Z"},
                {"clientId": CID.upper(), "expirationDate": "2027-10-14T07:14:23.0254447Z"}])
        if self.path == "/auth/eldo":
            return self._send(200, STATE["bundle"])
        self._send(404, {})
srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake); PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

logbuf = io.StringIO(); h = logging.StreamHandler(logbuf); h.setLevel(logging.DEBUG); logging.getLogger().addHandler(h)
import auth.main as am
for name in ("auth.service", "eldo.auth"): logging.getLogger(name).addHandler(h)
am.ELDO_SELLER_TOKEN_URL = BASE + "/api/authentication/seller/token"
am.ELDO_SELLER_PROBE_URL = BASE + "/api/v1/orders/me/seller-api-eligibility"
am.ELDO_SELLER_CREDENTIALS_URL = BASE + "/api/client-credentials"
alerts = []
am.send_ops_alert = lambda key, message, cooldown=0: alerts.append((key, message)) or True
am.clear_ops_alert = lambda key: alerts.append(("clear", key))
COOKIE = {"cookies": {"__Host-EldoradoIdToken": "id", "__Host-EldoradoRefreshToken": "rt"}, "xsrf_token": "x", "user_agent": "UA", "logged_in": True}
cookie_ok = [True]; cookie_calls = [0]
def _fake_refresh(self):
    cookie_calls[0] += 1
    return dict(COOKIE) if cookie_ok[0] else None
am.EldoAuth._try_backend_refresh = _fake_refresh
am._eldo_capture_isolated = lambda profile, timeout_sec=200: {}

n_ok = 0
def check(name, cond, extra=""):
    global n_ok
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra) if extra and not cond else ""))
    if cond: n_ok += 1
    else: FAILS.append(name)
FAILS = []
def fresh():
    STATE.update(token="ok", probe=200); STATE["hits"].clear(); alerts.clear(); cookie_ok[0] = True
    return am.EldoAuth()
def posts(): return sum(1 for m, p in STATE["hits"] if m == "POST")

# 1. che do cookie: khong dung cong token
am.ELDO_AUTH_MODE = "cookie"; a = fresh(); d = a.capture()
check("cookie mode: tra bundle cookie, 0 request toi cong token", d.get("xsrf_token") == "x" and not d.get("bearer_token") and not STATE["hits"], STATE["hits"])
check("cookie mode: is_fresh theo luat cu", a.is_fresh() is True)

# 2. seller_api thanh cong
am.ELDO_AUTH_MODE = "seller_api"; a = fresh(); t0 = time.time(); d = a.capture()
check("seller: bundle co bearer_token + auth_mode", d.get("bearer_token") == STATE["last_token"] and d.get("auth_mode") == "seller_api")
check("seller: expires_at ~ now+899", 890 <= d["expires_at"] - t0 <= 900, d["expires_at"] - t0)
check("seller: seller_id lay tu claim userId", d.get("seller_id") == "seller-uuid-1")
check("seller: logged_in + api_verified + cookies rong", d["logged_in"] and d["api_verified"] and d["cookies"] == {} and d["xsrf_token"] == "")
check("seller: goi dung 1 POST token", posts() == 1, STATE["hits"])
check("seller: han credential doc dung (so sanh clientId khong phan biet hoa thuong)", a._seller_cred_exp == 1823498063.0, a._seller_cred_exp)
check("seller: seller_credential_expires_in > 0", (a.seller_credential_expires_in() or 0) > 300 * 86400)
check("seller: alert duoc clear", ("clear", "eldo-seller-api") in alerts and not [k for k, _ in alerts if k != "clear"], alerts)
check("seller: is_fresh True khi token moi", a.is_fresh() is True)
a.data["expires_at"] = time.time() + 359; check("seller: is_fresh False khi con <360s", a.is_fresh() is False)
a.data["expires_at"] = time.time() + 365; check("seller: is_fresh True khi con >360s", a.is_fresh() is True)
a.capture(); check("seller: lan capture thu 2 khong doc lai han credential (6h)", sum(1 for m, p in STATE["hits"] if p == "/api/client-credentials") == 1)

# 3. credential bi tu choi (401) -> lui ve cookie
a = fresh(); STATE["token"] = 401; t0 = time.time(); d = a.capture()
check("401: lui ve bundle cookie", d.get("xsrf_token") == "x" and not d.get("bearer_token"))
check("401: alert eldo-seller-api noi ro credential bi tu choi", any(k == "eldo-seller-api" and "từ chối credential" in m for k, m in alerts), alerts)
check("401: cho 300s moi thu lai", 295 <= a._seller_retry_at - t0 <= 301, a._seller_retry_at - t0)
check("401: bundle cookie fresh theo luat 13 phut", a.is_fresh() is True)
a.data = None; a.capture(); check("401: trong luc cho KHONG goi lai cong token", posts() == 1, posts())

# 4. cong token loi 500 / khong ket noi duoc
a = fresh(); STATE["token"] = 500; t0 = time.time(); d = a.capture()
check("500: lui ve cookie, cho 60s", not d.get("bearer_token") and 55 <= a._seller_retry_at - t0 <= 61)
check("500: alert ghi HTTP 500", any("HTTP 500" in m for k, m in alerts if k == "eldo-seller-api"), alerts)
a = fresh(); real = am.ELDO_SELLER_TOKEN_URL; am.ELDO_SELLER_TOKEN_URL = "http://127.0.0.1:9/x"; d = a.capture(); am.ELDO_SELLER_TOKEN_URL = real
check("mat ket noi: lui ve cookie, khong nem loi", not d.get("bearer_token") and d.get("xsrf_token") == "x")

# 5. token cap duoc nhung probe 403 -> coi nhu bi tu choi
a = fresh(); STATE["probe"] = 403; d = a.capture()
check("probe 403: lui ve cookie", not d.get("bearer_token") and a._seller_retry_at - time.time() > 290)
# 6. probe 500 -> van nhan token, api_verified False
a = fresh(); STATE["probe"] = 500; d = a.capture()
check("probe 500: van nhan token, api_verified False", bool(d.get("bearer_token")) and d["api_verified"] is False)

# 7. token cu da het han + seller loi + cookie chet -> khong phat lai token chet
a = fresh(); a.capture(); a.data["expires_at"] = time.time() - 5; STATE["token"] = 401; cookie_ok[0] = False; d = a.capture()
check("token het han + moi thu deu loi: tra {} (handler se 503)", d == {} and a.data is None, d)
# 7b. token cu CON han + seller loi tam -> giu token dang song, KHONG dung toi duong cookie/Camoufox
a = fresh(); a.capture(); a.data["expires_at"] = time.time() + 200; STATE["token"] = 500; cookie_ok[0] = False; cookie_calls[0] = 0; d = a.capture()
check("token con han + cong token loi tam: giu token dang song, khong dung duong cookie", bool(d.get("bearer_token")) and cookie_calls[0] == 0, cookie_calls)
check("  ... va is_fresh van False de lan sau thu lai", a.is_fresh() is False)
d2 = a.capture(); check("  ... trong 60s cho: van tra token cu, khong goi lai cong token", bool(d2.get("bearer_token")) and posts() == 2, posts())
# 7c. token sap chet (<60s) + seller loi + cookie con song -> chuyen sang cookie
a = fresh(); a.capture(); a.data["expires_at"] = time.time() + 30; STATE["token"] = 500; cookie_calls[0] = 0; d = a.capture()
check("token con <60s + cong token loi: chuyen sang bundle cookie", not d.get("bearer_token") and d.get("xsrf_token") == "x" and cookie_calls[0] == 1, d)
# 7d. sau khi lui ve cookie, cong token song lai -> lan capture ke tiep quay ve token
STATE["token"] = "ok"; a._seller_retry_at = 0; d = a.capture()
check("cong token song lai: tu quay ve Bearer", bool(d.get("bearer_token")) and ("clear", "eldo-seller-api") in alerts)

# 8. thieu credential trong .env
a = fresh(); cid = am.ELDO_SELLER_CLIENT_ID; am.ELDO_SELLER_CLIENT_ID = ""; d = a.capture(); am.ELDO_SELLER_CLIENT_ID = cid
check("thieu client id: 0 request, alert, lui ve cookie", not STATE["hits"] and any("thiếu" in m for k, m in alerts) and d.get("xsrf_token") == "x")

# 9. /health
a = fresh(); a.capture(); am.eldo_auth = a
resp = asyncio.run(am.handle_health(None)); hj = json.loads(resp.text); e = hj["eldo"]
check("health: co 4 truong moi", e["auth_mode"] == "seller_api" and e["bearer"] is True and 880 <= e["bearer_expires_in"] <= 900 and e["seller_credential_expires_in"] > 0, e)
check("health: giu du truong cu", all(k in e for k in ("has_cookies", "fresh", "active_profile", "cookies", "xsrf", "logged_in", "refresh_token_expires_in", "refresh_token_profile")), list(e))
check("health: fresh True, logged_in True", e["fresh"] is True and e["logged_in"] is True)
am.ELDO_AUTH_MODE = "cookie"; a = fresh(); a.capture(); am.eldo_auth = a
e = json.loads(asyncio.run(am.handle_health(None)).text)["eldo"]
check("health cookie mode: bearer False, bearer_expires_in None, has_cookies True", e["bearer"] is False and e["bearer_expires_in"] is None and e["has_cookies"] is True and e["auth_mode"] == "cookie", e)
resp = asyncio.run(am.handle_auth_eldo(None)); check("handler /auth/eldo cookie mode: tra bundle cookie nhu cu", json.loads(resp.text).get("xsrf_token") == "x")

# 10. client
from shared.eldo_auth import EldoAuthData, EldoAuthManager
hd = EldoAuthData(user_agent="UA", bearer_token="TOK", cookies={"a": "b"}, xsrf_token="x", nsure_device_id="n").build_headers()
check("client: header Bearer, khong Cookie/XSRF/nsure", hd == {"user-agent": "UA", "accept": "application/json, text/plain, */*", "authorization": "Bearer TOK"}, hd)
hd = EldoAuthData(user_agent="UA", cookies={"a": "b"}, xsrf_token="x", nsure_device_id="n", x_client_build_time="t").build_headers()
check("client: che do cookie giu nguyen bo header cu", hd == {"user-agent": "UA", "accept": "application/json, text/plain, */*", "origin": "https://www.eldorado.gg", "referer": "https://www.eldorado.gg/", "x-xsrf-token": "x", "nsure-device-id": "n", "x-client-build-time": "t", "Cookie": "a=b"}, hd)
mgr = EldoAuthManager(auth_url=BASE)
STATE["bundle"] = {"auth_mode": "seller_api", "bearer_token": "TOK", "expires_at": time.time() + 100, "user_agent": "UA", "seller_id": "sid", "cookies": {}, "xsrf_token": ""}
ad = asyncio.run(mgr.get_auth()); left = mgr._expires_at - time.time()
check("client: doc bearer tu auth service, cache khong qua han token (100s-30)", ad.bearer_token == "TOK" and ad.seller_id == "sid" and 60 <= left <= 71, left)
STATE["bundle"] = {"auth_mode": "seller_api", "bearer_token": "TOK", "expires_at": time.time() + 900, "user_agent": "UA"}
mgr = EldoAuthManager(auth_url=BASE); asyncio.run(mgr.get_auth()); left = mgr._expires_at - time.time()
check("client: token con dai thi cache du 300s", 295 <= left <= 300, left)
STATE["bundle"] = dict(COOKIE); mgr = EldoAuthManager(auth_url=BASE); ad = asyncio.run(mgr.get_auth())
check("client: bundle cookie cu van doc dung, cache 300s", ad.bearer_token == "" and ad.xsrf_token == "x" and 295 <= mgr._expires_at - time.time() <= 300)

# 11. secret khong lo
log = logbuf.getvalue()
check("secret KHONG xuat hien trong log", SECRET not in log)
check("secret KHONG xuat hien trong /health hay alert", SECRET not in json.dumps(hj) and all(SECRET not in m for _, m in alerts if isinstance(m, str)))
check("log co dong 'Seller API token OK'", "Seller API token OK" in log)
print(f"\n{n_ok} PASS, {len(FAILS)} FAIL", FAILS or "")
print("--- vai dong log mau:"); print("\n".join([l for l in log.splitlines() if "ELDO" in l or "Eldorado auth fetched" in l][:8]))
sys.exit(1 if FAILS else 0)
