"""Unit test cho báo lỗi API G2G có đăng nhập (shared/api_health.py) và việc scanner
không còn nuốt 401/403 thành "không có đơn".

Bối cảnh: 23/10/2026 G2G (AWS WAF) chặn IP .220 ~48 phút, list_my_order trả 403
nhưng get_pending_orders bắt `except APIError: return []` (AuthError là lớp con)
nên scanner mù mà log không có dòng nào.

Không mạng: G2G giả, đồng hồ giả, file trạng thái trong thư mục tạm.
Cần venv có requirements.txt (curl_cffi, aiohttp, python-dotenv).

Run:  python tests/test_g2g_api_health.py
"""

import asyncio
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from shared import api_health  # noqa: E402
from shared.g2g_api import G2GAPIClient, AuthError  # noqa: E402
from shared.g2g_auth import G2GAuthData  # noqa: E402
from scanners import g2g_scanner_api  # noqa: E402
from scanners.g2g_scanner_api import G2GAPIScanner  # noqa: E402

SELLER = "1001273426"


# ── Đồ giả ──────────────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def time(self):
        return self.t


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.headers = {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        return self.resp


class FakeAuthMgr:
    def __init__(self, fail=None):
        self.fail = fail
        self.auth = G2GAuthData(jwt_token="h.e.s", user_agent="ua", seller_id=SELLER)

    async def get_auth(self):
        if self.fail:
            raise self.fail
        return self.auth

    async def invalidate(self):
        pass


class Recorder:
    def __init__(self):
        self.sent, self.cleared = [], []

    def send(self, key, message, cooldown=0):
        self.sent.append((key, message))
        return True

    def clear(self, key):
        self.cleared.append(key)


def fresh(tmp):
    """Đặt lại trạng thái module + đồng hồ + bộ ghi alert cho mỗi test."""
    clock = FakeClock()
    rec = Recorder()
    api_health.time = clock
    api_health.send_ops_alert = rec.send
    api_health.clear_ops_alert = rec.clear
    api_health._state.update(ok=True, since=None, status=None, error="", last_ok=None)
    api_health._alerted = False
    api_health._last_write = 0.0
    path = os.path.join(tmp, "g2g_api_health.json")
    if os.path.exists(path):
        os.remove(path)
    return clock, rec, path


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ok -", msg)


# ── api_health ─────────────────────────────────────────────────────────────

def test_missing_file_is_unknown(tmp):
    print("test_missing_file_is_unknown")
    _, _, path = fresh(tmp)
    h = api_health.read_g2g_api_health(path)
    check(h["state"] == "unknown", "chưa có file → unknown, không báo động giả")


def test_403_lifecycle(tmp):
    print("test_403_lifecycle")
    clock, rec, path = fresh(tmp)
    t0 = clock.t

    api_health.record_g2g_api(True, path=path)
    h = api_health.read_g2g_api_health(path)
    check(h["state"] == "ok" and h["last_ok"] == t0, "vòng OK ghi state=ok + last_ok")

    clock.t = t0 + 10
    api_health.record_g2g_api(False, 403, "list_my_order: auth error HTTP 403", path=path)
    h = api_health.read_g2g_api_health(path)
    check(h["state"] == "failing" and h["http_status"] == 403, "403 → failing, ghi ngay (đổi trạng thái)")
    check(h["since"] == t0 + 10 and h["failing_seconds"] == 0, "since = lần lỗi đầu")
    check(not rec.sent, "chưa đủ 180s thì chưa alert")

    clock.t = t0 + 10 + 179
    api_health.record_g2g_api(False, 403, "x", path=path)
    check(not rec.sent, "179s vẫn chưa alert")

    clock.t = t0 + 10 + 180
    api_health.record_g2g_api(False, 403, "x", path=path)
    check(len(rec.sent) == 1 and rec.sent[0][0] == api_health.ALERT_KEY, "đủ 180s → alert g2g-api-auth")
    check("HTTP 403" in rec.sent[0][1] and "chặn IP" in rec.sent[0][1], "lời alert nói rõ 403 = chặn IP")
    h = api_health.read_g2g_api_health(path)
    check(h["failing_seconds"] == 180, "failing_seconds đúng 180 cho Console")

    clock.t = t0 + 10 + 600
    api_health.record_g2g_api(False, 403, "x", path=path)
    check(len(rec.sent) == 1, "cả đợt chỉ alert 1 lần")

    clock.t = t0 + 700
    api_health.record_g2g_api(True, path=path)
    h = api_health.read_g2g_api_health(path)
    check(h["state"] == "ok" and h["failing_seconds"] == 0, "hết lỗi → ok")
    check(rec.cleared == [api_health.ALERT_KEY], "hết lỗi → gỡ alert")

    clock.t = t0 + 800
    api_health.record_g2g_api(False, 403, "x", path=path)
    clock.t = t0 + 800 + 200
    api_health.record_g2g_api(False, 403, "x", path=path)
    check(len(rec.sent) == 2, "đợt lỗi mới → alert lại")


def test_short_401_never_alerts(tmp):
    print("test_short_401_never_alerts")
    clock, rec, path = fresh(tmp)
    t0 = clock.t
    api_health.record_g2g_api(False, 401, "jwt vừa xoay", path=path)
    clock.t = t0 + 15
    api_health.record_g2g_api(True, path=path)
    check(not rec.sent and not rec.cleared, "401 một vòng rồi OK: không alert, không clear thừa")


def test_write_throttle_and_stale(tmp):
    print("test_write_throttle_and_stale")
    clock, _, path = fresh(tmp)
    t0 = clock.t
    api_health.record_g2g_api(True, path=path)
    clock.t = t0 + 30
    api_health.record_g2g_api(True, path=path)
    check(api_health.read_g2g_api_health(path)["updated_at"] == t0, "cùng trạng thái < 60s: không ghi lại")
    clock.t = t0 + 61
    api_health.record_g2g_api(True, path=path)
    check(api_health.read_g2g_api_health(path)["updated_at"] == t0 + 61, "≥ 60s: ghi lại làm nhịp sống")
    clock.t = t0 + 61 + 301
    check(api_health.read_g2g_api_health(path)["state"] == "stale", "file đứng im > 300s → stale")


def test_status_change_rewrites(tmp):
    print("test_status_change_rewrites")
    clock, _, path = fresh(tmp)
    t0 = clock.t
    api_health.record_g2g_api(False, 401, "a", path=path)
    clock.t = t0 + 20
    api_health.record_g2g_api(False, 403, "b", path=path)
    h = api_health.read_g2g_api_health(path)
    check(h["http_status"] == 403 and h["since"] == t0, "401 → 403: ghi mã mới, giữ mốc since")


def test_record_never_raises(tmp):
    print("test_record_never_raises")
    fresh(tmp)
    api_health.record_g2g_api(False, 403, "x", path=os.path.join(tmp, "khong-co", "f.json"))
    check(True, "thư mục không tồn tại: không raise")


# ── g2g_api.get_pending_orders ───────────────────────────────────────────────

def _client(resp):
    c = G2GAPIClient(FakeAuthMgr())
    c._sess = FakeSession(resp)
    return c


def test_get_pending_orders_raises_on_auth():
    print("test_get_pending_orders_raises_on_auth")
    auth = FakeAuthMgr().auth
    for code in (401, 403):
        c = _client(FakeResp(code, {"message": "Forbidden"}))
        try:
            c.get_pending_orders(auth, SELLER)
        except AuthError as e:
            check(e.status == code, f"HTTP {code} → AuthError (không còn trả rỗng)")
        else:
            raise AssertionError(f"HTTP {code} bị nuốt thành []")
    c = _client(FakeResp(404, {"code": 4041, "messages": [{"text": "Data was not found"}]}))
    check(c.get_pending_orders(auth, SELLER) == [], "4041 (không có đơn) vẫn trả []")
    c = _client(FakeResp(429, {"message": "slow"}))
    check(c.get_pending_orders(auth, SELLER) == [], "429 vẫn bỏ qua vòng")
    c = _client(FakeResp(200, {"code": 2000, "payload": {"results": [{"order_item_id": "A-1"}]}}))
    check(len(c.get_pending_orders(auth, SELLER)) == 1, "200 trả danh sách đơn")


# ── scanner.scan_order_list ───────────────────────────────────────────────────

def test_scanner_records_health():
    print("test_scanner_records_health")
    calls = []
    g2g_scanner_api.record_g2g_api = lambda ok, status=None, error="": calls.append((ok, status))

    s = G2GAPIScanner(FakeAuthMgr(), {}, db=None)
    s.api._sess = FakeSession(FakeResp(403, {"message": "Forbidden"}))
    out = asyncio.run(s.scan_order_list())
    check(out == [] and calls[-1] == (False, 403), "list_my_order 403 → record(False, 403)")

    s.api._sess = FakeSession(FakeResp(404, {"code": 4041, "messages": []}))
    asyncio.run(s.scan_order_list())
    check(calls[-1] == (True, None), "không có đơn → record(True)")

    s2 = G2GAPIScanner(FakeAuthMgr(fail=RuntimeError("HTTP 503")), {}, db=None)
    asyncio.run(s2.scan_order_list())
    check(calls[-1] == (False, None), "auth service lỗi → record(False, None)")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        test_missing_file_is_unknown(tmp)
        test_403_lifecycle(tmp)
        test_short_401_never_alerts(tmp)
        test_write_throttle_and_stale(tmp)
        test_status_change_rewrites(tmp)
        test_record_never_raises(tmp)
    test_get_pending_orders_raises_on_auth()
    test_scanner_records_health()
    print("\nTẤT CẢ ĐỀU XANH")
