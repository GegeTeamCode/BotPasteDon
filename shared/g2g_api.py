"""G2G Order API client — replaces Selenium for scanner + worker.

Uses curl_cffi with browser impersonation for anti-fingerprinting.
On 401/403: forces auth service to re-capture JWT, then retries once.
"""

import asyncio
import json
import random
from typing import Dict, List, Optional, Tuple

from curl_cffi import requests as cffi

from shared.alerts import send_ops_alert, clear_ops_alert
from shared.g2g_auth import G2GAuthData, G2GAuthManager
from shared.logging_config import setup_logger

# setup_logger, không phải logging.getLogger: logger trần không có handler nên
# dòng INFO (Chat sent, Uploaded …) bị mất và WARNING ra stderr không có giờ.
logger = setup_logger("g2g.api")

BASE = "https://sls.g2g.com"

# Sendbird — chat G2G. Từ HAR trình duyệt / frontend G2G. Mọi lệnh gọi Sendbird
# chỉ cần header Session-Key (đo 22/10/2026: App-ID không bắt buộc, JWT G2G không
# dùng). Xem docs/marketplace_chat.md mục 2.
SENDBIRD_APP_ID = "34201740-152E-401E-AD8F-5C72EEABA386"
SENDBIRD_BASE = f"https://api-{SENDBIRD_APP_ID.lower()}.sendbird.com/v3"
SENDBIRD_KEY_ALERT = "g2g-sendbird-key"

# list_my_order returns only 10 rows by default; G2G honors `page_size` (verified
# live: page_size=100 -> 100 rows). Larger window so a burst of pending orders
# isn't truncated to the 10 newest (older ones would be invisible until some of
# the visible ones move out of the queried status). Sequential processing keeps
# the request rate low, so a big page does not cause a 429 burst.
LIST_PAGE_SIZE = 100

_IMPERSONATE_POOL = [
    "chrome120", "chrome119", "chrome116", "chrome110",
    "edge99", "edge101",
]


class APIError(Exception):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


class AuthError(APIError):
    pass


class RateLimitError(APIError):
    def __init__(self, msg, retry_after=60):
        super().__init__(msg, status=429)
        self.retry_after = retry_after


class SendbirdError(APIError):
    """Sendbird từ chối hoặc lỗi.

    Cố ý KHÔNG kế thừa AuthError: lỗi Sendbird không nói gì về JWT G2G, và
    AuthError làm call_with_retry vô hiệu cache JWT.
    """


class SendbirdKeyError(SendbirdError):
    """SENDBIRD_SESSION_KEY thiếu, sai hoặc bị thu hồi (mã 4003xx / 400401)."""


class ChatError(APIError):
    """Bước chat chưa xong.

    ``progress`` giữ những gì đã làm được — ``buyer_id``, ``channel_url``,
    ``card_sent`` — để lần gửi lại đi tiếp từ đó, khỏi gọi lại G2G và khỏi gửi
    trùng thẻ đơn.
    """

    def __init__(self, msg, progress: Optional[dict] = None, status=0):
        super().__init__(msg, status)
        self.progress = dict(progress or {})


def _is_sendbird_key_error(code) -> bool:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return False
    # 4003xx = nhóm lỗi xác thực của Sendbird (đã thấy 400303 "Session key is
    # invalid."); 400401 = không gửi Session-Key nào.
    return 400300 <= code < 400400 or code == 400401


# G2G order/upload_url rejects any other extension with HTTP 400
# "unsupported file type" (verified live 2026-06-14: webp/heic/no-ext fail).
_G2G_PROOF_EXTS = {"jpg", "jpeg", "png", "gif", "mp4", "mov"}


class G2GAPIClient:
    """REST API client for G2G orders — no browser needed."""

    def __init__(self, auth_manager: G2GAuthManager):
        self.auth_mgr = auth_manager
        self._sess = cffi.Session(impersonate=random.choice(_IMPERSONATE_POOL))
        self._seller_id: Optional[str] = None

    async def call_with_retry(self, func, *args, **kwargs):
        """Call API function. Invalidate cache on AuthError but do NOT retry.

        Retrying with dead JWT causes G2G to kick the login session.
        Let auth service refresh JWT on its own schedule, then the next
        scan cycle / worker task will pick up the fresh token.
        """
        try:
            return func(*args, **kwargs)
        except AuthError:
            logger.warning("Auth error — invalidating JWT cache, NOT retrying (would kick G2G session)")
            await self.auth_mgr.invalidate()
            raise

    def _parse(self, resp, context: str) -> dict:
        status = resp.status_code
        if status == 429:
            retry_after = float(resp.headers.get("Retry-After", 60))
            raise RateLimitError(f"{context}: rate limited", retry_after)
        if status in (401, 403):
            raise AuthError(f"{context}: auth error HTTP {status}", status)
        try:
            j = resp.json()
        except Exception:
            if status != 200:
                raise APIError(f"{context}: HTTP {status}", status)
            raise APIError(f"{context}: invalid JSON", status)
        code = j.get("code")
        if status != 200 and code != 2000:
            msg = ""
            for m in (j.get("messages") or []):
                msg += m.get("text", "") + " "
            raise APIError(f"{context}: HTTP {status} | {msg.strip()}", status)
        code = j.get("code")
        if code and code != 2000:
            raise APIError(f"{context}: API code={code}", status)
        return j

    # ── Scanner APIs ──────────────────────────────────────────────────────

    def get_pending_orders(self, auth: G2GAuthData, seller_id: str = "") -> list:
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        r = self._sess.get(
            f"{BASE}/order/list_my_order",
            params={"seller_id": seller_id, "status": "preparing",
                    "page_size": LIST_PAGE_SIZE},
            headers=auth.build_headers(),
            timeout=30,
        )
        try:
            j = self._parse(r, "list_my_order")
        except RateLimitError as e:
            # Surface rate limiting (was hidden under the generic APIError below);
            # the scan loop's interval is the back-off — just skip this cycle.
            logger.warning("list_my_order rate limited (retry_after=%ss) — skipping cycle",
                           getattr(e, "retry_after", "?"))
            return []
        except APIError:
            # 4041 = no results (normal when no pending orders)
            return []
        payload = j.get("payload")
        if isinstance(payload, list):
            return payload
        return (payload or {}).get("results") or []

    def get_order_detail(self, order_item_id: str, auth: G2GAuthData,
                         seller_id: str = "") -> dict:
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        r = self._sess.get(
            f"{BASE}/order/item/{order_item_id}",
            params={"seller_id": seller_id},
            headers=auth.build_headers(),
            timeout=30,
        )
        j = self._parse(r, "order_detail")
        return j.get("payload", {})

    # ── status_sync APIs ────────────────────────────────────────────────────

    def list_orders_by_status(self, status: str, auth: G2GAuthData,
                               seller_id: str = "") -> list:
        """List orders filtered by G2G status (delivering / completed / cancelled / ...).
        Returns up to LIST_PAGE_SIZE (100) newest. For full history use the `next`
        cursor in the payload (not needed for top-of-list change detection)."""
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        r = self._sess.get(
            f"{BASE}/order/list_my_order",
            params={"seller_id": seller_id, "status": status,
                    "include_pending_proof_only": "0",
                    "page_size": LIST_PAGE_SIZE},
            headers=auth.build_headers(),
            timeout=30,
        )
        try:
            j = self._parse(r, f"list_my_order_{status}")
        except APIError:
            return []
        payload = j.get("payload")
        if isinstance(payload, list):
            return payload
        return (payload or {}).get("results") or []

    def count_my_orders(self, auth: G2GAuthData, seller_id: str = "") -> dict:
        """Return G2G counts: {to_pay, verifying_payment, preparing, delivering,
        issues, last_order_completed_at}. Cheap tripwire for changes."""
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        r = self._sess.get(
            f"{BASE}/order/count-my-orders",
            params={"seller_id": seller_id},
            headers=auth.build_headers(),
            timeout=30,
        )
        j = self._parse(r, "count_my_orders")
        return j.get("payload") or {}

    def list_my_cases(self, auth: G2GAuthData, seller_id: str = "",
                       next_key: str = "") -> tuple:
        """List dispute/resolution cases. Returns (results, next_key) for pagination.
        next_key is a string cursor; pass empty string for first page."""
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        params = {"seller_id": seller_id}
        if next_key:
            params["next_key"] = next_key
        r = self._sess.get(
            f"{BASE}/order/list_my_cases",
            params=params,
            headers=auth.build_headers(),
            timeout=30,
        )
        try:
            j = self._parse(r, "list_my_cases")
        except APIError:
            return [], ""
        payload = j.get("payload") or {}
        results = payload.get("results") or []
        nxt = payload.get("next_key") or ""
        return results, nxt

    # ── Worker APIs ───────────────────────────────────────────────────────

    def start_deliver(self, order_item_id: str, auth: G2GAuthData,
                      seller_id: str = "") -> dict:
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        r = self._sess.put(
            f"{BASE}/order/item/{order_item_id}/start_deliver",
            params={"seller_id": seller_id},
            headers=auth.build_headers(),
            timeout=30,
        )
        return self._parse(r, "start_deliver")

    def mark_as_delivering(self, order_item_id: str, auth: G2GAuthData,
                           seller_id: str = "") -> dict:
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        r = self._sess.put(
            f"{BASE}/order/item/{order_item_id}/mark_as_delivering",
            params={"seller_id": seller_id},
            headers=auth.build_headers(),
            timeout=30,
        )
        return self._parse(r, "mark_as_delivering")

    def submit_delivered_qty(self, order_item_id: str, qty: int,
                             auth: G2GAuthData, seller_id: str = "") -> dict:
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        r = self._sess.put(
            f"{BASE}/order/item/{order_item_id}/delivered_qty",
            params={"seller_id": seller_id},
            headers=auth.build_headers(),
            json={"qty": qty},
            timeout=30,
        )
        return self._parse(r, "delivered_qty")

    def get_upload_url(self, filename: str, auth: G2GAuthData,
                       seller_id: str = "") -> dict:
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        r = self._sess.get(
            f"{BASE}/order/upload_url",
            params={"name": filename, "upload_type": "delivery_proof",
                    "seller_id": seller_id},
            headers=auth.build_headers(),
            timeout=30,
        )
        j = self._parse(r, "upload_url")
        return j.get("payload") or {}

    def upload_to_s3(self, presigned: dict, file_path: str) -> bool:
        import requests
        url = presigned.get("url", "")
        fields = presigned.get("fields", {})
        new_filename = presigned.get("new_filename", "")
        with open(file_path, "rb") as f:
            file_data = f.read()

        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        ct = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "gif": "image/gif", "mp4": "video/mp4",
              "mov": "video/quicktime"}.get(ext, "image/png")

        # S3 presigned POST — standard requests library (no fingerprint needed)
        files = {"file": (new_filename or "proof.png", file_data, ct)}
        r = requests.post(url, data=fields, files=files, timeout=60)
        if r.status_code not in (200, 204):
            logger.warning("S3 upload failed: HTTP %d | body: %s",
                           r.status_code, r.text[:200] if r.text else "")
        return r.status_code in (200, 204)

    def submit_delivery_proof(self, order_item_id: str, upload_list: list,
                              auth: G2GAuthData, seller_id: str = "") -> dict:
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        r = self._sess.post(
            f"{BASE}/order/item/{order_item_id}/delivery_proof",
            params={"seller_id": seller_id},
            headers=auth.build_headers(),
            json={"upload_list": upload_list, "seller_id": seller_id},
            timeout=30,
        )
        return self._parse(r, "delivery_proof")

    # ── Chat APIs ─────────────────────────────────────────────────────────

    def create_chat_channel(self, seller_id: str, buyer_id: str,
                            auth: G2GAuthData) -> str:
        """Create/get chat channel via G2G API, returns Sendbird channel_url.

        Trả "" khi G2G không trả channel_url. Không tự dựng tên kênh: thứ tự id
        trong channel_url không cố định (22/10: 33/50 kênh buyer trước, 17/50
        seller trước), đoán sai là gửi vào kênh không tồn tại.
        """
        r = self._sess.post(
            f"{BASE}/chat/channel",
            headers=auth.build_headers(),
            json={
                "channel_id": f"{seller_id}_{buyer_id}",
                "channel_name": f"Direct Message Channel Between {seller_id} and {buyer_id}",
                "inviter_id": seller_id,
                "user_ids": [seller_id, buyer_id],
                "channel_type": "dm",
            },
            timeout=30,
        )
        j = self._parse(r, "chat_channel")
        details = (j.get("payload") or {}).get("channel_details") or {}
        return details.get("channel_url") or ""

    @staticmethod
    def _sendbird_headers(session_key: str) -> dict:
        return {
            "Session-Key": session_key,
            "App-ID": SENDBIRD_APP_ID,
            "Content-Type": "application/json",
        }

    def _parse_sendbird(self, resp, context: str) -> dict:
        """Trả JSON khi Sendbird nhận lệnh, ngược lại ném SendbirdError.

        Lỗi Sendbird là HTTP 4xx kèm body ``{"error": true, "code", "message"}``.
        Khoá hỏng thì bắn ops alert một lần mỗi 6 giờ; gọi được lại thì gỡ.
        """
        status = resp.status_code
        try:
            j = resp.json()
        except Exception:
            j = None
        if status == 200 and isinstance(j, dict) and not j.get("error"):
            clear_ops_alert(SENDBIRD_KEY_ALERT)
            return j
        j = j if isinstance(j, dict) else {}
        code = j.get("code")
        detail = (f"{context}: Sendbird HTTP {status} code={code} "
                  f"{str(j.get('message') or '')[:160]}").rstrip()
        if _is_sendbird_key_error(code):
            send_ops_alert(
                SENDBIRD_KEY_ALERT,
                f"🔴 [G2G] Sendbird từ chối SENDBIRD_SESSION_KEY (code={code}) — chat G2G "
                "ngừng, đơn vẫn giao. Lấy khoá mới từ HAR trình duyệt (trang chat G2G), "
                "sửa .env trên .220, restart worker_g2g (KHÔNG cần restart auth).",
            )
            raise SendbirdKeyError(detail, status)
        raise SendbirdError(detail, status)

    def find_chat_channel(self, seller_id: str, buyer_id: str,
                          session_key: str) -> str:
        """Tìm kênh DM có sẵn giữa seller và buyer chỉ bằng Sendbird (không JWT).

        Dùng khi ``chat/channel`` của G2G lỗi. Trả "" nếu hai bên chưa từng có
        kênh — trường hợp đó vẫn phải để G2G tạo kênh.
        """
        r = self._sess.get(
            f"{SENDBIRD_BASE}/users/{seller_id}/my_group_channels",
            params={"members_exactly_in": buyer_id, "limit": 5},
            headers=self._sendbird_headers(session_key),
            timeout=30,
        )
        j = self._parse_sendbird(r, "sendbird_find_channel")
        chans = [c for c in (j.get("channels") or []) if c.get("channel_url")]
        dm = [c for c in chans if c.get("custom_type") == "dm"]
        pick = dm or chans
        return pick[0]["channel_url"] if pick else ""

    def send_chat_message(self, channel_url: str, message: str,
                          session_key: str, user_id: str = "",
                          data: str = "", custom_type: str = "") -> dict:
        """Gửi một tin MESG. Ném SendbirdError nếu Sendbird không tạo được tin."""
        body = {"message_type": "MESG", "message": message, "user_id": user_id}
        if data:
            body["data"] = data
        if custom_type:
            body["custom_type"] = custom_type
        r = self._sess.post(
            f"{SENDBIRD_BASE}/group_channels/{channel_url}/messages",
            headers=self._sendbird_headers(session_key),
            json=body,
            timeout=30,
        )
        j = self._parse_sendbird(r, "sendbird_send")
        if not j.get("message_id"):
            raise SendbirdError("sendbird_send: HTTP 200 nhưng không có message_id", 200)
        return j

    # ── Step helpers for per-step delivery ──────────────────────────────────

    async def _upload_proofs(self, order_item_id: str, file_paths: list,
                              auth: G2GAuthData, seller_id: str = ""):
        """Upload proof files as a single step.

        G2G's upload_url endpoint rejects unsupported extensions (webp/heic/no-ext)
        with HTTP 400. Isolate each file so ONE bad file can't abort the whole step
        (that caused infinite retry — e.g. order LVB9 with .webp). Skip unsupported
        ext / per-file failures and submit whatever uploaded. If NOTHING uploads:
        - transient per-file failures (no presigned URL, S3/network error) raise a
          retry-classified APIError so the worker retries the step. Mislabeling
          these terminal stranded 7 orders 2026-08-27..31 (an S3 "Max retries
          exceeded" surfaced as "unsupported file type" → ORDER_FAILED, no retry).
        - only genuinely unsupported extensions raise the terminal message so it
          surfaces for manual handling instead of looping forever.
        """
        upload_list = []
        unsupported = []
        failed = []
        last_error = ""
        for fp in file_paths:
            filename = fp.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in _G2G_PROOF_EXTS:
                logger.warning("[%s] Skipping unsupported proof %s (ext=%s); G2G accepts %s",
                               order_item_id, filename, ext or "none", sorted(_G2G_PROOF_EXTS))
                unsupported.append(filename)
                continue
            try:
                presigned = await self.call_with_retry(
                    self.get_upload_url, filename, auth, seller_id)
                if not presigned.get("url"):
                    logger.warning("[%s] No upload URL for %s", order_item_id, filename)
                    failed.append(filename)
                    last_error = f"no upload URL returned for {filename}"
                    continue
                new_name = presigned.get("new_filename", filename)
                s3_key = presigned.get("fields", {}).get("key", f"delivery_proof/{new_name}")
                if self.upload_to_s3(presigned, fp):
                    upload_list.append(s3_key)
                    logger.info("[%s] Uploaded %s → %s", order_item_id, filename, s3_key)
                else:
                    failed.append(filename)
                    last_error = f"upload_to_s3 returned False for {filename}"
            except AuthError:
                raise  # auth must bubble up so the worker refreshes JWT
            except Exception as e:
                logger.warning("[%s] Proof upload failed for %s, skipping: %s",
                               order_item_id, filename, str(e)[:120])
                failed.append(filename)
                last_error = str(e)[:300]

        if upload_list:
            await self.call_with_retry(
                self.submit_delivery_proof, order_item_id, upload_list, auth, seller_id)
            logger.info("[%s] delivery_proof submitted (%d files, %d unsupported, %d failed)",
                         order_item_id, len(upload_list), len(unsupported), len(failed))
        elif failed:
            # Transient upload failure — embed the underlying error text verbatim so
            # the worker's _classify_error sees retry keywords ("max retries
            # exceeded", "connection refused", …). Must NOT contain "unsupported"
            # (terminal keyword) nor 401/403/jwt (auth keyword).
            raise APIError(
                f"delivery_proof: failed to upload {len(failed)} of "
                f"{len(file_paths)} proof file(s) ({', '.join(failed)}): {last_error}", 502)
        elif unsupported:
            # Nothing uploadable and every skip was a bad extension — terminal,
            # surface for manual handling (message kept verbatim for _classify_error).
            raise APIError(
                f"delivery_proof: all {len(unsupported)} proof file(s) unsupported "
                f"file type, manual upload needed: {unsupported}", 400)

    async def _resolve_channel(self, order_item_id: str, seller_id: str,
                               buyer_id: str, auth: G2GAuthData,
                               session_key: str) -> str:
        """channel_url của kênh seller–buyer: hỏi G2G trước, lỗi thì tìm qua Sendbird.

        ``chat/channel`` là lệnh duy nhất của bước chat phụ thuộc máy chủ G2G và
        đã làm mất chat của 4 đơn 16–17/10 (HTTP 500/502, timeout). Gặp AuthError
        cũng chỉ lùi sang Sendbird: call_with_retry đã vô hiệu cache JWT, gọi lại
        G2G ngay bằng JWT vừa chết làm G2G đá phiên.
        """
        try:
            url = await self.call_with_retry(
                self.create_chat_channel, seller_id, buyer_id, auth)
            if url:
                return url
            reason = "chat/channel không trả channel_url"
        except Exception as e:
            reason = str(e)[:160]
        logger.warning("[%s] Không mở được kênh qua G2G (%s) — tìm kênh có sẵn qua Sendbird",
                       order_item_id, reason)
        url = self.find_chat_channel(seller_id, buyer_id, session_key)
        if not url:
            raise APIError(f"chat: không mở được kênh ({reason}) và Sendbird chưa có "
                           f"kênh nào với buyer {buyer_id}")
        logger.info("[%s] Dùng kênh có sẵn trên Sendbird", order_item_id)
        return url

    async def _send_chat(self, order_item_id: str, message: str,
                          auth: G2GAuthData, seller_id: str = "",
                          progress: Optional[dict] = None) -> dict:
        """Gửi thẻ đơn + lời nhắn cho người mua. Trả ``progress`` khi xong.

        Lỗi gì cũng ném ``ChatError`` kèm ``progress`` (``buyer_id``,
        ``channel_url``, ``card_sent``); truyền lại ``progress`` đó ở lần gửi
        sau để đi tiếp — có đủ ``buyer_id`` và ``channel_url`` thì không gọi G2G
        lần nào, đã gửi thẻ thì không gửi trùng.
        """
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        p = dict(progress or {})
        p["card_sent"] = bool(p.get("card_sent"))
        session_key = auth.sendbird_session_key or ""
        try:
            if not session_key:
                send_ops_alert(
                    SENDBIRD_KEY_ALERT,
                    "🔴 [G2G] SENDBIRD_SESSION_KEY trống trong .env — chat G2G không gửi được.",
                )
                raise SendbirdKeyError("SENDBIRD_SESSION_KEY trống trong .env")

            if not p.get("buyer_id"):
                detail = await self.call_with_retry(
                    self.get_order_detail, order_item_id, auth, seller_id)
                p["buyer_id"] = str(detail.get("buyer_id") or "")
                if not p["buyer_id"]:
                    raise APIError("chat: chi tiết đơn không có buyer_id")

            if not p.get("channel_url"):
                p["channel_url"] = await self._resolve_channel(
                    order_item_id, seller_id, p["buyer_id"], auth, session_key)

            if not p["card_sent"]:
                order_url = f"https://www.g2g.com/g2g-user/sale/order/item/{order_item_id}"
                chat_data = json.dumps({
                    "source": "api",
                    "order_item_id": order_item_id,
                    "buyer_id": p["buyer_id"],
                    "seller_id": seller_id,
                })
                self.send_chat_message(
                    p["channel_url"], f"Sold Order Item {order_item_id} {order_url}",
                    session_key, seller_id, data=chat_data, custom_type="order-item")
                p["card_sent"] = True
                await asyncio.sleep(3)

            self.send_chat_message(p["channel_url"], message, session_key, seller_id)
        except ChatError:
            raise
        except Exception as e:
            raise ChatError(f"chat: {type(e).__name__}: {str(e)[:250]}", progress=p,
                            status=getattr(e, "status", 0)) from e
        logger.info("[%s] Chat sent to buyer %s", order_item_id, p["buyer_id"])
        return p

    def close(self):
        try:
            self._sess.close()
        except Exception:
            pass
