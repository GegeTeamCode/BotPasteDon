"""Unit test cho chat G2G (Sendbird) và việc gửi lại lời nhắn CHAT_PENDING ở hai worker.

Không mạng: G2G, Sendbird, TalkJS đều giả; DB SQLite tạm; message.txt tạm.
Cần venv có requirements.txt (curl_cffi, aiohttp, selenium, webdriver-manager, websockets).

Run:  python tests/test_marketplace_chat.py
"""

import asyncio
import datetime
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from shared import alerts  # noqa: E402
from shared.constants import ORDER_COMPLETED  # noqa: E402
from shared.database import Database  # noqa: E402
from shared.g2g_api import (  # noqa: E402
    G2GAPIClient, AuthError, ChatError, SendbirdError, SendbirdKeyError, SENDBIRD_KEY_ALERT,
)
from shared.g2g_auth import G2GAuthData  # noqa: E402
from workers import base_worker  # noqa: E402
from workers import g2g_worker, eldorado_worker  # noqa: E402

SELLER = "1001273426"
BUYER = "555"
KEY = "k" * 40
MESSAGE = "Thank you for your purchase!"

_real_sleep = asyncio.sleep


async def _fast_sleep(_delay, *a, **kw):
    # _send_chat nghỉ 3s giữa thẻ đơn và lời nhắn — test không cần chờ thật.
    await _real_sleep(0)


asyncio.sleep = _fast_sleep


# ── Đồ giả ──────────────────────────────────────────────────────────────────

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
    """Trả phản hồi theo (method, chuỗi con của URL); phản hồi cuối của mỗi
    hàng đợi được dùng lại. Ghi mọi lệnh gọi vào ``calls``."""

    def __init__(self):
        self.routes = []
        self.calls = []

    def on(self, method, needle, *responses):
        self.routes.insert(0, [method, needle, list(responses)])

    def _do(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for m, needle, resps in self.routes:
            if m == method and needle in url and resps:
                r = resps.pop(0) if len(resps) > 1 else resps[0]
                if isinstance(r, Exception):
                    raise r
                return r
        raise AssertionError(f"lệnh gọi không mong đợi: {method} {url}")

    def get(self, url, **kw):
        return self._do("GET", url, **kw)

    def post(self, url, **kw):
        return self._do("POST", url, **kw)

    def put(self, url, **kw):
        return self._do("PUT", url, **kw)

    def close(self):
        pass

    def count(self, method, needle):
        return sum(1 for m, u, _ in self.calls if m == method and needle in u)


class FakeAuthMgr:
    def __init__(self, key=KEY):
        self.invalidated = 0
        self.auth = G2GAuthData(jwt_token="h.e.s", user_agent="ua", seller_id=SELLER,
                                sendbird_session_key=key)

    async def get_auth(self):
        return self.auth

    async def invalidate(self):
        self.invalidated += 1


def ok_g2g(payload=None):
    return FakeResp(200, {"code": 2000, "payload": payload or {}})


def sb_msg():
    return FakeResp(200, {"message_id": 1, "type": "MESG"})


def sb_err(code, status=400):
    return FakeResp(status, {"error": True, "code": code, "message": f"err {code}"})


def make_client(key=KEY):
    mgr = FakeAuthMgr(key)
    c = G2GAPIClient(mgr)
    c._sess.close()
    c._sess = FakeSession()
    return c, mgr


def g2g_routes(sess, channel=None):
    sess.on("GET", "/order/item/ORD-1", ok_g2g({"buyer_id": BUYER}))
    sess.on("POST", "/chat/channel", channel or ok_g2g(
        {"channel_details": {"channel_url": f"g2g_dm_{BUYER}_{SELLER}"}}))
    sess.on("POST", "sendbird.com/v3/group_channels/", sb_msg())
    sess.on("GET", "sendbird.com/v3/users/", FakeResp(200, {"channels": [
        {"channel_url": "nhom-khac", "custom_type": "group"},
        {"channel_url": f"g2g_dm_{SELLER}_{BUYER}", "custom_type": "dm"},
    ]}))


def run(coro):
    return asyncio.run(coro)


def sendbird_posts(sess):
    return [kw.get("json") or {} for m, u, kw in sess.calls
            if m == "POST" and "sendbird.com" in u]


# ── Sendbird: đọc phản hồi ──────────────────────────────────────────────────

def test_sendbird_error_not_autherror():
    # AuthError làm call_with_retry vô hiệu JWT G2G — lỗi Sendbird không được thế.
    assert not issubclass(SendbirdError, AuthError)
    assert issubclass(SendbirdKeyError, SendbirdError)
    print("✓ SendbirdError không phải AuthError")


def test_send_ok_and_key_error_alert():
    c, _ = make_client()
    alerts.clear_ops_alert(SENDBIRD_KEY_ALERT)
    c._sess.on("POST", "sendbird.com", sb_err(400303))
    try:
        c.send_chat_message("ch", "hi", KEY, SELLER)
        raise AssertionError("phải ném SendbirdKeyError")
    except SendbirdKeyError as e:
        assert "400303" in str(e)
    assert SENDBIRD_KEY_ALERT in alerts._last_sent, "phải bắn ops alert khi khoá hỏng"

    c._sess.on("POST", "sendbird.com", sb_msg())
    assert c.send_chat_message("ch", "hi", KEY, SELLER)["message_id"] == 1
    assert SENDBIRD_KEY_ALERT not in alerts._last_sent, "gọi được lại thì phải gỡ alert"
    print("✓ send_chat_message: 400303 → SendbirdKeyError + alert, gọi lại được thì gỡ")


def test_send_other_errors():
    c, _ = make_client()
    c._sess.on("POST", "sendbird.com", sb_err(500901, status=500))
    try:
        c.send_chat_message("ch", "hi", KEY, SELLER)
        raise AssertionError("phải ném SendbirdError")
    except SendbirdKeyError:
        raise AssertionError("500901 không phải lỗi khoá")
    except SendbirdError:
        pass
    c._sess.on("POST", "sendbird.com", FakeResp(200, {"type": "MESG"}))
    try:
        c.send_chat_message("ch", "hi", KEY, SELLER)
        raise AssertionError("200 không có message_id phải là lỗi")
    except SendbirdError:
        pass
    print("✓ send_chat_message: lỗi khác và 200 thiếu message_id đều ném SendbirdError")


def test_create_channel_never_guesses():
    c, _ = make_client()
    c._sess.on("POST", "/chat/channel", ok_g2g({"channel_details": {}}))
    assert c.create_chat_channel(SELLER, BUYER, c.auth_mgr.auth) == ""
    print("✓ create_chat_channel: thiếu channel_url thì trả rỗng, không tự dựng tên")


def test_find_channel_prefers_dm():
    c, _ = make_client()
    g2g_routes(c._sess)
    assert c.find_chat_channel(SELLER, BUYER, KEY) == f"g2g_dm_{SELLER}_{BUYER}"
    _, url, kw = c._sess.calls[-1]
    assert f"/users/{SELLER}/my_group_channels" in url
    assert kw["params"]["members_exactly_in"] == BUYER
    assert kw["headers"]["Session-Key"] == KEY
    print("✓ find_chat_channel: members_exactly_in, chọn kênh dm")


# ── _send_chat ───────────────────────────────────────────────────────────────

def test_send_chat_happy():
    c, mgr = make_client()
    g2g_routes(c._sess)
    p = run(c._send_chat("ORD-1", MESSAGE, mgr.auth, SELLER))
    assert p == {"buyer_id": BUYER, "channel_url": f"g2g_dm_{BUYER}_{SELLER}", "card_sent": True}, p
    posts = sendbird_posts(c._sess)
    assert len(posts) == 2
    assert posts[0]["custom_type"] == "order-item"
    assert json.loads(posts[0]["data"])["source"] == "api"
    assert posts[1]["message"] == MESSAGE and "custom_type" not in posts[1]
    print("✓ _send_chat: thẻ đơn + lời nhắn")


def test_send_chat_channel_502_falls_back_to_sendbird():
    c, mgr = make_client()
    g2g_routes(c._sess, channel=FakeResp(502, None))
    p = run(c._send_chat("ORD-1", MESSAGE, mgr.auth, SELLER))
    assert p["channel_url"] == f"g2g_dm_{SELLER}_{BUYER}"
    assert len(sendbird_posts(c._sess)) == 2
    print("✓ _send_chat: chat/channel 502 → tìm kênh qua Sendbird")


def test_send_chat_channel_401_no_second_g2g_call():
    c, mgr = make_client()
    g2g_routes(c._sess, channel=FakeResp(401, {"code": 4010}))
    p = run(c._send_chat("ORD-1", MESSAGE, mgr.auth, SELLER))
    assert mgr.invalidated == 1, "AuthError phải vô hiệu cache JWT đúng một lần"
    assert c._sess.count("POST", "/chat/channel") == 1, "không được gọi lại G2G bằng JWT vừa chết"
    assert p["channel_url"] == f"g2g_dm_{SELLER}_{BUYER}"
    print("✓ _send_chat: chat/channel 401 → không gọi lại G2G, lùi sang Sendbird")


def test_send_chat_resume_after_text_failure():
    c, mgr = make_client()
    g2g_routes(c._sess)
    c._sess.on("POST", "sendbird.com/v3/group_channels/", sb_msg(), sb_err(500901, status=500))
    try:
        run(c._send_chat("ORD-1", MESSAGE, mgr.auth, SELLER))
        raise AssertionError("phải ném ChatError")
    except ChatError as e:
        progress = e.progress
    assert progress == {"buyer_id": BUYER, "channel_url": f"g2g_dm_{BUYER}_{SELLER}",
                        "card_sent": True}, progress

    c2, mgr2 = make_client()
    c2._sess.on("POST", "sendbird.com/v3/group_channels/", sb_msg())
    run(c2._send_chat("ORD-1", MESSAGE, mgr2.auth, SELLER, progress=progress))
    posts = sendbird_posts(c2._sess)
    assert len(posts) == 1 and posts[0]["message"] == MESSAGE, "chỉ gửi lại lời nhắn"
    assert not any("g2g.com" in u for _, u, _ in c2._sess.calls), "có progress thì không gọi G2G"
    print("✓ _send_chat: hỏng ở lời nhắn → lần sau chỉ gửi lời nhắn, không gọi G2G")


def test_send_chat_without_key():
    c, mgr = make_client(key="")
    alerts.clear_ops_alert(SENDBIRD_KEY_ALERT)
    try:
        run(c._send_chat("ORD-1", MESSAGE, mgr.auth, SELLER))
        raise AssertionError("phải ném ChatError")
    except ChatError as e:
        assert isinstance(e.__cause__, SendbirdKeyError)
    assert c._sess.calls == [], "không có khoá thì không gọi mạng"
    assert SENDBIRD_KEY_ALERT in alerts._last_sent
    print("✓ _send_chat: thiếu Session-Key → ChatError + alert, không gọi mạng")


def test_send_chat_no_channel_anywhere():
    c, mgr = make_client()
    g2g_routes(c._sess, channel=FakeResp(502, None))
    c._sess.on("GET", "sendbird.com/v3/users/", FakeResp(200, {"channels": []}))
    try:
        run(c._send_chat("ORD-1", MESSAGE, mgr.auth, SELLER))
        raise AssertionError("phải ném ChatError")
    except ChatError as e:
        assert e.progress.get("buyer_id") == BUYER and not e.progress.get("channel_url")
    print("✓ _send_chat: G2G lỗi và Sendbird chưa có kênh → ChatError")


# ── base_worker: CHAT_PENDING ────────────────────────────────────────────────

def _fresh_db(tmp):
    db = Database(os.path.join(tmp, f"orders_{datetime.datetime.now().timestamp()}.db"))
    return db


def _past(db, order_id):
    order = db.get_order(order_id)
    data = json.loads(order["retry_data"])
    data["chat"]["next_retry_at"] = "2000-01-01T00:00:00"
    db.update_order_status(order_id, order["status"], retry_data=json.dumps(data))


def test_chat_pending_lifecycle(tmp):
    db = _fresh_db(tmp)
    db.insert_order("g2g", "O1", {"url": "u"})
    assert base_worker.mark_chat_pending(db, "g2g", "O1", {"api_id": "O1-1"}, "boom")
    o = db.get_order("O1")
    assert o["status"] == ORDER_COMPLETED and o["error_message"].startswith("CHAT_PENDING:")
    payload = base_worker.load_chat_payload(o)
    nxt = datetime.datetime.fromisoformat(payload["next_retry_at"])
    wait = (nxt - base_worker._utcnow()).total_seconds()
    assert 55 <= wait <= 61, wait
    assert base_worker.due_chat_retries(db, "g2g") == [], "chưa tới hạn"
    _past(db, "O1")
    assert [o["order_id"] for o, _ in base_worker.due_chat_retries(db, "g2g")] == ["O1"]
    assert base_worker.due_chat_retries(db, "eldorado") == [], "lọc theo sàn"

    for tries, expect in ((2, 300), (3, 1800), (4, 3600)):
        assert base_worker.mark_chat_pending(db, "g2g", "O1", payload, "boom", tries)
        nxt = base_worker.load_chat_payload(db.get_order("O1"))["next_retry_at"]
        wait = (datetime.datetime.fromisoformat(nxt) - base_worker._utcnow()).total_seconds()
        assert expect - 5 <= wait <= expect + 1, (tries, wait)

    alerts._last_sent.pop("chat-stuck:g2g:O1", None)
    assert not base_worker.mark_chat_pending(db, "g2g", "O1", payload, "boom", 5)
    o = db.get_order("O1")
    assert o["status"] == ORDER_COMPLETED and o["error_message"].startswith("CHAT_STUCK:")
    assert "chat-stuck:g2g:O1" in alerts._last_sent
    assert base_worker.due_chat_retries(db, "g2g") == [], "CHAT_STUCK không được quét lại"

    base_worker.complete_order(db, "O1")
    o = db.get_order("O1")
    assert o["error_message"] == "" and o["retry_data"] is None, "complete_order xoá dấu CHAT_*"

    db.update_order_status("O1", "DELIVERING", error_message="NETWORK:x")
    base_worker.complete_order(db, "O1")
    assert db.get_order("O1")["error_message"] == "NETWORK:x", "lỗi khác giữ nguyên như cũ"
    print("✓ CHAT_PENDING: giãn 60/300/1800/3600s, lượt 5 → CHAT_STUCK + alert, complete_order dọn dấu")


# ── Worker G2G: đơn hoàn tất + gửi lại chat ──────────────────────────────────

def test_g2g_worker_chat_pending_then_resend(tmp):
    db = _fresh_db(tmp)
    db.insert_order("g2g", "ORD-1", {"url": "https://www.g2g.com/g2g-user/sale/order/item/ORD-1"})
    client, mgr = make_client()
    g2g_routes(client._sess)
    client._sess.on("PUT", "/delivered_qty", ok_g2g())
    client._sess.on("POST", "sendbird.com/v3/group_channels/", sb_msg(), sb_err(500901, status=500))
    g2g_worker.db, g2g_worker.api_client, g2g_worker.auth_manager = db, client, mgr
    g2g_worker.G2G_USE_API = True

    run(g2g_worker.process_task({"order_id": "ORD-1", "delivery_qty": "2", "files": [],
                                 "order_url": "https://www.g2g.com/g2g-user/sale/order/item/ORD-1"}))
    o = db.get_order("ORD-1")
    assert o["status"] == ORDER_COMPLETED, o["status"]
    assert o["error_message"].startswith("CHAT_PENDING:"), o["error_message"]
    payload = base_worker.load_chat_payload(o)
    assert payload["api_id"] == "ORD-1" and payload["progress"]["card_sent"] is True

    client2, mgr2 = make_client()
    client2._sess.on("POST", "sendbird.com/v3/group_channels/", sb_msg())
    g2g_worker.api_client, g2g_worker.auth_manager = client2, mgr2
    _past(db, "ORD-1")
    for order, pl in base_worker.due_chat_retries(db, "g2g"):
        run(g2g_worker._resend_g2g_chat(order, pl))
    o = db.get_order("ORD-1")
    assert o["status"] == ORDER_COMPLETED and o["error_message"] == "", o["error_message"]
    assert not any("g2g.com" in u for _, u, _ in client2._sess.calls), "gửi lại không gọi G2G"
    assert len(sendbird_posts(client2._sess)) == 1
    print("✓ worker G2G: chat hỏng → COMPLETED + CHAT_PENDING; gửi lại chỉ lời nhắn, dọn dấu")


def test_g2g_resend_stops_sweep_on_jwt_rejection(tmp):
    # Lượt gửi lại cần buyer_id (chưa có trong progress) → gọi G2G → 401. Vòng quét
    # phải dừng: đơn kế tiếp sẽ lấy lại đúng JWT đó, gọi G2G nữa là có thể bị đá phiên.
    db = _fresh_db(tmp)
    for oid in ("ORD-1", "ORD-2"):
        db.insert_order("g2g", oid, {"url": "u"})
        base_worker.mark_chat_pending(db, "g2g", oid, {"api_id": oid, "progress": {}}, "cu")
        _past(db, oid)
    client, mgr = make_client()
    client._sess.on("GET", "/order/item/", FakeResp(401, {"code": 4010}))
    g2g_worker.db, g2g_worker.api_client, g2g_worker.auth_manager = db, client, mgr

    stopped = False
    try:  # cùng khuôn với vòng quét trong recover_pending_retries
        for order, pl in base_worker.due_chat_retries(db, "g2g"):
            run(g2g_worker._resend_g2g_chat(order, pl))
    except RuntimeError:
        stopped = True
    assert stopped, "401 từ G2G phải dừng lượt quét"
    assert client._sess.count("GET", "/order/item/") == 1, "chỉ được gọi G2G một lần"
    assert mgr.invalidated == 1
    states = sorted(base_worker.load_chat_payload(db.get_order(o)).get("tries") for o in ("ORD-1", "ORD-2"))
    assert states == [1, 2], states
    print("✓ worker G2G: G2G từ chối JWT khi gửi lại → dừng lượt quét, không gọi G2G lần hai")


# ── Worker Eldorado ─────────────────────────────────────────────────────────

class FakeEldoAPI:
    def __init__(self):
        self.delivered = 0

    async def call_with_retry(self, func, *args, **kwargs):
        return func(*args, **kwargs)

    def get_order_detail(self, order_id, auth):
        return {"talkJsConversationId": "conv-1", "state": {"state": "Paid"}}

    def deliver_order(self, order_id, auth):
        self.delivered += 1
        return {}

    def get_talkjs_auth(self, auth):
        return "talkjs.jwt.token"

    def get_talkjs_user_id(self, jwt):
        return "user-1"


class FakeTalkJS:
    def __init__(self, results):
        self.results = list(results)
        self.auth_token = "talkjs.jwt.token"
        self.user_id = "user-1"
        self._is_connected = True
        self.sent = []

    @property
    def is_connected(self):
        return self._is_connected

    async def connect(self):
        self._is_connected = True
        return True

    async def close(self):
        self._is_connected = False

    async def send_text_message(self, conv_id, text):
        self.sent.append((conv_id, text))
        return self.results.pop(0) if self.results else None


class FakeEldoAuthMgr:
    async def get_auth(self):
        return object()


def test_eldo_worker_chat_pending_then_resend(tmp):
    db = _fresh_db(tmp)
    db.insert_order("eldorado", "ABC", {"url": "u"})
    api = FakeEldoAPI()
    eldorado_worker.db, eldorado_worker.api_client = db, api
    eldorado_worker.auth_manager = FakeEldoAuthMgr()
    eldorado_worker.talkjs_client = FakeTalkJS([None, None, None])
    eldorado_worker.ELDO_USE_API = True

    run(eldorado_worker.process_task({"order_id": "abc", "action": "normal_delivery", "files": []}))
    o = db.get_order("ABC")
    assert api.delivered == 1
    assert o["status"] == ORDER_COMPLETED, o["status"]
    assert o["error_message"].startswith("CHAT_PENDING:"), o["error_message"]
    assert len(eldorado_worker.talkjs_client.sent) == 3, "3 lần thử trong một lượt"

    eldorado_worker.talkjs_client = FakeTalkJS(["msg_1"])
    _past(db, "ABC")
    for order, pl in base_worker.due_chat_retries(db, "eldorado"):
        run(eldorado_worker._resend_eldo_chat(order, pl))
    o = db.get_order("ABC")
    assert o["error_message"] == "", o["error_message"]
    assert eldorado_worker.talkjs_client.sent == [("conv-1", MESSAGE)]
    assert api.delivered == 1, "gửi lại chat không được bấm deliver lần nữa"
    print("✓ worker Eldorado: chat hỏng → COMPLETED + CHAT_PENDING; gửi lại chỉ lời nhắn")


def test_eldo_worker_chat_ok_completes_clean(tmp):
    db = _fresh_db(tmp)
    db.insert_order("eldorado", "DEF", {"url": "u"})
    db.update_order_status("DEF", "DELIVERING", error_message="CHAT_PENDING:luot 1: cu")
    eldorado_worker.db, eldorado_worker.api_client = db, FakeEldoAPI()
    eldorado_worker.auth_manager = FakeEldoAuthMgr()
    eldorado_worker.talkjs_client = FakeTalkJS(["msg_1"])
    run(eldorado_worker.process_task({"order_id": "def", "action": "normal_delivery", "files": []}))
    o = db.get_order("DEF")
    assert o["status"] == ORDER_COMPLETED and o["error_message"] == "", o["error_message"]
    print("✓ worker Eldorado: giao lại xong thì dọn dấu CHAT_PENDING cũ")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        # Worker đọc message.txt theo cwd.
        with open(os.path.join(tmp, "message.txt"), "w", encoding="utf-8") as f:
            f.write(MESSAGE)
        os.chdir(tmp)
        test_sendbird_error_not_autherror()
        test_send_ok_and_key_error_alert()
        test_send_other_errors()
        test_create_channel_never_guesses()
        test_find_channel_prefers_dm()
        test_send_chat_happy()
        test_send_chat_channel_502_falls_back_to_sendbird()
        test_send_chat_channel_401_no_second_g2g_call()
        test_send_chat_resume_after_text_failure()
        test_send_chat_without_key()
        test_send_chat_no_channel_anywhere()
        test_chat_pending_lifecycle(tmp)
        test_g2g_worker_chat_pending_then_resend(tmp)
        test_g2g_resend_stops_sweep_on_jwt_rejection(tmp)
        test_eldo_worker_chat_pending_then_resend(tmp)
        test_eldo_worker_chat_ok_completes_clean(tmp)
    print("\nTẤT CẢ ĐỀU XANH")
