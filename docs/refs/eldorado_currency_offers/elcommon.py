# elcommon.py — tro giup chung cho dieu tra Aion 2 Kinah tren Eldorado (22/10/2026).
# Chay tren .199 bang venv + PYTHONPATH cua repo bot dap gia. KHONG sua repo/.env.
#  - GET: >= GAP giay/luot, khoa file .last_get de moi script chung mot nhip.
#  - 429 dau tien -> dung han (GetStop).
#  - Token Seller API lay qua control/auth/el_seller_api.get_bundle (Redis el:seller_token).
#  - Khong bao gio in/ghi client secret hay token.
import asyncio
import fcntl
import json
import os
import sys
import time
from pathlib import Path

REPO = Path.home() / "Desktop/automation/G2G-AutomationBot-v4"
HERE = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dotenv import dotenv_values  # noqa: E402

_env = dotenv_values(REPO / ".env")
for _k in ("ELDO_AUTH_MODE", "ELDO_SELLER_CLIENT_ID", "ELDO_SELLER_CLIENT_SECRET",
           "ELDO_SELLER_USER_AGENT", "REDIS_URL"):
    if _env.get(_k):
        os.environ.setdefault(_k, _env[_k])

from curl_cffi import requests as cffi  # noqa: E402

BASE = "https://www.eldorado.gg/api"
UA = os.environ.get("ELDO_SELLER_USER_AGENT") or (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36")
GAP = 1.1
_STAMP = HERE / ".last_get"
_GETLOG = HERE / "gets.log"
_sess = cffi.Session(impersonate="chrome131")


class GetStop(Exception):
    pass


def _throttle():
    with open(_STAMP, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        s = fh.read().strip()
        last = float(s) if s else 0.0
        wait = last + GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        fh.seek(0)
        fh.truncate()
        fh.write(f"{time.time():.3f}")


_tok = {"t": None, "exp": 0.0}


def token() -> str:
    """Bearer token Seller API, cung cach control lam (cache Redis el:seller_token)."""
    if _tok["t"] and _tok["exp"] - time.time() > 90:
        return _tok["t"]
    import redis.asyncio as aioredis
    from control.auth import el_seller_api

    async def _go():
        r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                              decode_responses=True)
        try:
            b = await el_seller_api.get_bundle(r)
        finally:
            await r.aclose()
        return b, el_seller_api.last_error()

    b, err = asyncio.run(_go())
    if not b or not b.get("bearer_token"):
        raise RuntimeError(f"Khong lay duoc token Seller API: {err}")
    _tok["t"], _tok["exp"] = b["bearer_token"], float(b.get("expires_at", 0))
    return _tok["t"]


def headers(auth: bool) -> dict:
    h = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
    if auth:
        h.update({"Content-Type": "application/json", "User-Agent": UA,
                  "Authorization": f"Bearer {token()}"})
    return h


def get(path: str, params=None, auth: bool = False, body=None, allow=(200,)):
    """Mot luot GET co nhip. Tra (status, json|text). 429 -> GetStop."""
    url = path if path.startswith("http") else f"{BASE}/{path.lstrip('/')}"
    _throttle()
    t0 = time.time()
    kw = {"headers": headers(auth), "params": params, "timeout": 30}
    if body is not None:
        kw["json"] = body
    r = _sess.get(url, **kw)
    lat = int((time.time() - t0) * 1000)
    with open(_GETLOG, "a") as fh:
        fh.write(json.dumps({"ts": round(t0, 3), "url": url, "params": params,
                             "auth": auth, "status": r.status_code, "ms": lat}) + "\n")
    if r.status_code == 429:
        raise GetStop(f"GET 429 tai {url} – dung han")
    try:
        data = r.json()
    except Exception:
        data = r.text
    return r.status_code, data


def dump(name: str, obj):
    p = HERE / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    return p
