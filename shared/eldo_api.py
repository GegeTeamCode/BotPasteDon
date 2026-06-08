"""Eldorado Order API client — replaces Selenium for scanner + worker.

Uses curl_cffi with browser impersonation for anti-fingerprinting.
Auth: Cookie-based session + x-xsrf-token header.
"""

import logging
import random
import base64
import json
from typing import Dict, List, Optional
from functools import lru_cache

from curl_cffi import requests as cffi

from shared.eldo_auth import EldoAuthData, EldoAuthManager

logger = logging.getLogger("eldo.api")

BASE = "https://www.eldorado.gg/api"

_IMPERSONATE_POOL = [
    "chrome120", "chrome119", "chrome116", "chrome110",
    "edge99", "edge101",
]

INITIAL_CURSOR = "9999-99-99 99:99:99.999999999999999-9999-9999-9999-999999999999"


class APIError(Exception):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


class AuthError(APIError):
    pass


class EldoradoAPIClient:
    """REST API client for Eldorado orders — no browser needed."""

    def __init__(self, auth_manager: EldoAuthManager):
        self.auth_mgr = auth_manager
        self._sess = cffi.Session(impersonate=random.choice(_IMPERSONATE_POOL))
        self._seller_id: Optional[str] = None
        self._game_library: Optional[Dict[str, str]] = None

    async def call_with_retry(self, func, *args, **kwargs):
        """Call API function. Invalidate cache on AuthError but do NOT retry."""
        try:
            return func(*args, **kwargs)
        except AuthError:
            logger.warning("Auth error — invalidating cache, NOT retrying")
            await self.auth_mgr.invalidate()
            raise

    def _parse(self, resp, context: str) -> dict:
        status = resp.status_code
        if status in (401, 403):
            raise AuthError(f"{context}: auth error HTTP {status}", status)
        try:
            j = resp.json()
        except Exception:
            if status != 200:
                raise APIError(f"{context}: HTTP {status}", status)
            raise APIError(f"{context}: invalid JSON", status)
        if status != 200:
            raise APIError(f"{context}: HTTP {status} | {str(j)[:200]}", status)
        return j

    # ── User APIs ──────────────────────────────────────────────────────────

    def get_user_profile(self, auth: EldoAuthData) -> dict:
        r = self._sess.get(f"{BASE}/users/me", headers=auth.build_headers(), timeout=30)
        return self._parse(r, "user_profile")

    def get_seller_id(self, auth: EldoAuthData) -> str:
        if self._seller_id:
            return self._seller_id
        profile = self.get_user_profile(auth)
        self._seller_id = profile.get("id", "")
        return self._seller_id

    # ── Game Library ───────────────────────────────────────────────────────

    def _fetch_game_library(self, auth: EldoAuthData) -> Dict[str, str]:
        r = self._sess.get(
            f"{BASE}/library", params={"locale": "en-US"},
            headers=auth.build_headers(), timeout=30,
        )
        j = self._parse(r, "game_library")
        mapping = {}
        if isinstance(j, list):
            for game in j:
                gid = str(game.get("gameId", ""))
                title = game.get("title", "")
                if gid and title:
                    mapping[gid] = title
        return mapping

    def get_game_name(self, game_id: str, auth: EldoAuthData) -> str:
        if not self._game_library:
            try:
                self._game_library = self._fetch_game_library(auth)
            except Exception as e:
                logger.warning("Failed to fetch game library: %s", e)
                self._game_library = {}
        return self._game_library.get(str(game_id), f"Game {game_id}")

    # ── Scanner APIs ───────────────────────────────────────────────────────

    def get_pending_orders(self, auth: EldoAuthData) -> list:
        params = {
            "cursorValue": INITIAL_CURSOR,
            "pageSize": "20",
            "pageDirection": "Next",
            "orderState": "PendingDelivery",
            "isAscendingDateOrder": "false",
            "ignorePendingReviewOrders": "true",
            "displayFilter": "DisplaySellingOrders",
            "orderGroup": "Regular",
        }
        r = self._sess.get(
            f"{BASE}/orders/me/seller/orders", params=params,
            headers=auth.build_headers(), timeout=30,
        )
        j = self._parse(r, "pending_orders")
        return j.get("results") or []

    def get_order_detail(self, order_id: str, auth: EldoAuthData) -> dict:
        r = self._sess.get(
            f"{BASE}/orders/me/{order_id}",
            headers=auth.build_headers(), timeout=30,
        )
        j = self._parse(r, "order_detail")
        return j

    # ── status_sync APIs ────────────────────────────────────────────────────

    def list_orders_by_state(self, order_state: str, auth: EldoAuthData,
                              cursor: str = "", page_size: int = 20) -> tuple:
        """Paginated order list for given state. Returns (results, next_cursor).
        Pass cursor='' (or default sentinel) for first page (newest)."""
        params = {
            "cursorValue": cursor or INITIAL_CURSOR,
            "pageSize": str(page_size),
            "pageDirection": "Next",
            "orderState": order_state,
            "isAscendingDateOrder": "false",
            "ignorePendingReviewOrders": "true",
            "displayFilter": "DisplaySellingOrders",
            "orderGroup": "Regular",
        }
        r = self._sess.get(
            f"{BASE}/orders/me/seller/orders", params=params,
            headers=auth.build_headers(), timeout=30,
        )
        j = self._parse(r, f"list_by_state_{order_state}")
        results = j.get("results") or []
        next_cursor = j.get("nextPageCursor") or ""
        return results, next_cursor

    def get_states_count(self, auth: EldoAuthData) -> dict:
        """Return Eldorado counts per state: {pendingDelivery, disputed, delivered,
        received, completed, canceled}. Cheap tripwire for changes."""
        params = {"displayFilter": "DisplaySellingOrders", "orderGroup": "Regular"}
        r = self._sess.get(
            f"{BASE}/orders/me/statesCount", params=params,
            headers=auth.build_headers(), timeout=30,
        )
        j = self._parse(r, "states_count")
        return j or {}

    # ── Worker APIs ────────────────────────────────────────────────────────

    def deliver_order(self, order_id: str, auth: EldoAuthData) -> dict:
        r = self._sess.put(
            f"{BASE}/orders/me/{order_id}/deliver",
            json={},
            headers=auth.build_headers(),
            timeout=30,
        )
        j = self._parse(r, "deliver_order")
        logger.info("[%s] Delivered via API", order_id)
        return j

    # ── TalkJS Auth ────────────────────────────────────────────────────────

    def get_talkjs_auth(self, auth: EldoAuthData) -> str:
        r = self._sess.get(
            f"{BASE}/conversations/me/authorize",
            headers=auth.build_headers(), timeout=30,
        )
        j = self._parse(r, "talkjs_auth")
        token = j.get("token", "")
        return token

    def get_talkjs_user_id(self, talkjs_jwt: str) -> str:
        try:
            payload = talkjs_jwt.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            return claims.get("sub", "")
        except Exception:
            return ""

    # ── Full delivery flow ─────────────────────────────────────────────────

    async def full_delivery(self, order_id: str, auth: EldoAuthData,
                            message: str = "", talkjs_client=None) -> dict:
        logger.info("[%s] API delivery start", order_id)

        # 1. Deliver order
        await self.call_with_retry(self.deliver_order, order_id, auth)
        logger.info("[%s] Delivered OK", order_id)

        # 2. Send chat message via TalkJS
        if message and talkjs_client:
            try:
                detail = await self.call_with_retry(
                    self.get_order_detail, order_id, auth)
                conv_id = detail.get("talkJsConversationId", "")
                if conv_id:
                    if not talkjs_client.auth_token:
                        jwt = await self.call_with_retry(
                            self.get_talkjs_auth, auth)
                        talkjs_client.auth_token = jwt
                        talkjs_client.user_id = self.get_talkjs_user_id(jwt)

                    if not talkjs_client.is_connected:
                        await talkjs_client.connect()

                    if talkjs_client.is_connected:
                        msg_id = await talkjs_client.send_text_message(conv_id, message)
                        if not msg_id:
                            await talkjs_client.send_text_message_rest(conv_id, message)
                    else:
                        await talkjs_client.send_text_message_rest(conv_id, message)
                    logger.info("[%s] Chat message sent", order_id)
                else:
                    logger.info("[%s] No TalkJS conversation ID", order_id)
            except Exception as e:
                logger.warning("[%s] Chat send failed: %s", order_id, e)

        return {"status": "completed", "order_id": order_id}

    def close(self):
        try:
            self._sess.close()
        except Exception:
            pass
