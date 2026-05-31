"""G2G Order API client — replaces Selenium for scanner + worker.

Uses curl_cffi with browser impersonation for anti-fingerprinting.
On 401/403: forces auth service to re-capture JWT, then retries once.
"""

import asyncio
import json
import logging
import random
from typing import Dict, List, Optional, Tuple

from curl_cffi import requests as cffi

from shared.g2g_auth import G2GAuthData, G2GAuthManager

logger = logging.getLogger("g2g.api")

BASE = "https://sls.g2g.com"

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
            params={"seller_id": seller_id, "status": "preparing"},
            headers=auth.build_headers(),
            timeout=30,
        )
        try:
            j = self._parse(r, "list_my_order")
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
              "mp4": "video/mp4"}.get(ext, "image/png")

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

    # Sendbird config — from HAR 8 / G2G frontend
    _SENDBIRD_APP_ID = "34201740-152E-401E-AD8F-5C72EEABA386"

    def create_chat_channel(self, seller_id: str, buyer_id: str,
                            auth: G2GAuthData) -> str:
        """Create/get chat channel via G2G API, returns Sendbird channel_url."""
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
        details = j.get("payload", {}).get("channel_details", {})
        return details.get("channel_url", f"g2g_dm_{buyer_id}_{seller_id}")

    def send_chat_message(self, channel_url: str, message: str,
                          session_key: str, user_id: str = "",
                          data: str = "", custom_type: str = "") -> dict:
        app_id = self._SENDBIRD_APP_ID
        body = {"message_type": "MESG", "message": message, "user_id": user_id}
        if data:
            body["data"] = data
        if custom_type:
            body["custom_type"] = custom_type
        r = self._sess.post(
            f"https://api-{app_id.lower()}.sendbird.com/v3/group_channels/{channel_url}/messages",
            headers={
                "Session-Key": session_key,
                "App-ID": app_id,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        )
        return r.json()

    # ── Step helpers for per-step delivery ──────────────────────────────────

    async def _upload_proofs(self, order_item_id: str, file_paths: list,
                              auth: G2GAuthData, seller_id: str = ""):
        """Upload proof files as a single step."""
        upload_list = []
        for fp in file_paths:
            filename = fp.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            presigned = await self.call_with_retry(
                self.get_upload_url, filename, auth, seller_id)
            if not presigned.get("url"):
                logger.warning("[%s] No upload URL for %s", order_item_id, filename)
                continue
            new_name = presigned.get("new_filename", filename)
            s3_key = presigned.get("fields", {}).get("key", f"delivery_proof/{new_name}")
            ok = self.upload_to_s3(presigned, fp)
            if ok:
                upload_list.append(s3_key)
                logger.info("[%s] Uploaded %s → %s", order_item_id, filename, s3_key)

        if upload_list:
            await self.call_with_retry(
                self.submit_delivery_proof, order_item_id, upload_list, auth, seller_id)
            logger.info("[%s] delivery_proof submitted (%d files)",
                         order_item_id, len(upload_list))

    async def _send_chat(self, order_item_id: str, message: str,
                          auth: G2GAuthData, seller_id: str = ""):
        """Send chat messages as a single step."""
        detail = await self.call_with_retry(
            self.get_order_detail, order_item_id, auth, seller_id)
        buyer_id = str(detail.get("buyer_id", ""))

        if not buyer_id:
            logger.info("[%s] Chat: no buyer_id", order_item_id)
            return

        channel_url = await self.call_with_retry(
            self.create_chat_channel, seller_id, buyer_id, auth)
        session_key = auth.sendbird_session_key or ""

        if not channel_url or not session_key:
            logger.info("[%s] Chat: no session key or channel", order_item_id)
            return

        order_url = f"https://www.g2g.com/g2g-user/sale/order/item/{order_item_id}"
        order_msg = f"Sold Order Item {order_item_id} {order_url}"
        chat_data = json.dumps({
            "source": "api",
            "order_item_id": order_item_id,
            "buyer_id": buyer_id,
            "seller_id": seller_id,
        })
        self.send_chat_message(
            channel_url, order_msg, session_key, seller_id,
            data=chat_data, custom_type="order-item")

        await asyncio.sleep(3)

        self.send_chat_message(
            channel_url, message, session_key, seller_id)
        logger.info("[%s] Chat sent to buyer %s", order_item_id, buyer_id)

    # ── Full delivery flow with auto-retry ────────────────────────────────

    async def full_delivery(self, order_item_id: str, qty: int,
                            file_paths: list, message: str,
                            auth: G2GAuthData, seller_id: str = "",
                            skip_steps: set = None) -> dict:
        """Execute complete delivery via API.

        skip_steps: set of step names already completed, e.g. {"qty", "proof"}
        """
        if not seller_id:
            seller_id = auth.seller_id or self._seller_id or ""
        skip = skip_steps or set()

        logger.info("[%s] API delivery start | qty=%d | files=%d | skip=%s",
                     order_item_id, qty, len(file_paths), skip)

        # 1. Submit delivered qty
        if "qty" not in skip:
            await self.call_with_retry(
                self.submit_delivered_qty, order_item_id, qty, auth, seller_id)
            logger.info("[%s] delivered_qty=%d OK", order_item_id, qty)
        else:
            logger.info("[%s] Skipping qty (already done)", order_item_id)

        # 2. Upload proof files
        if "proof" not in skip and file_paths:
            upload_list = []
            for fp in file_paths:
                filename = fp.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                presigned = await self.call_with_retry(
                    self.get_upload_url, filename, auth, seller_id)
                if not presigned.get("url"):
                    logger.warning("[%s] No upload URL for %s", order_item_id, filename)
                    continue
                new_name = presigned.get("new_filename", filename)
                s3_key = presigned.get("fields", {}).get("key", f"delivery_proof/{new_name}")
                ok = self.upload_to_s3(presigned, fp)
                if ok:
                    upload_list.append(s3_key)
                    logger.info("[%s] Uploaded %s → %s", order_item_id, filename, s3_key)

            if upload_list:
                await self.call_with_retry(
                    self.submit_delivery_proof, order_item_id, upload_list, auth, seller_id)
                logger.info("[%s] delivery_proof submitted (%d files)",
                             order_item_id, len(upload_list))
        elif "proof" in skip:
            logger.info("[%s] Skipping proof upload (already done)", order_item_id)

        # 3. Send chat message via Sendbird REST
        if "chat" not in skip and message:
            try:
                detail = await self.call_with_retry(
                    self.get_order_detail, order_item_id, auth, seller_id)
                buyer_id = str(detail.get("buyer_id", ""))

                if buyer_id:
                    channel_url = await self.call_with_retry(
                        self.create_chat_channel, seller_id, buyer_id, auth)
                    session_key = auth.sendbird_session_key or ""
                    if channel_url and session_key:
                        order_url = f"https://www.g2g.com/g2g-user/sale/order/item/{order_item_id}"
                        order_msg = f"Sold Order Item {order_item_id} {order_url}"
                        chat_data = json.dumps({
                            "source": "api",
                            "order_item_id": order_item_id,
                            "buyer_id": buyer_id,
                            "seller_id": seller_id,
                        })
                        self.send_chat_message(
                            channel_url, order_msg, session_key, seller_id,
                            data=chat_data, custom_type="order-item")

                        await asyncio.sleep(1)

                        self.send_chat_message(
                            channel_url, message, session_key, seller_id)
                        logger.info("[%s] Chat sent (order card + message) to buyer %s",
                                     order_item_id, buyer_id)
                    else:
                        logger.info("[%s] Chat: no session key, skipping", order_item_id)
                else:
                    logger.info("[%s] Chat: no buyer_id in order detail", order_item_id)
            except Exception as e:
                logger.warning("[%s] Chat send failed: %s", order_item_id, e)
        elif "chat" in skip:
            logger.info("[%s] Skipping chat (already done)", order_item_id)

        return {"status": "completed", "order_item_id": order_item_id}

    def close(self):
        try:
            self._sess.close()
        except Exception:
            pass
