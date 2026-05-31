"""G2G auth client — fetches JWT from auth service.

Auth service runs as a separate process (python -m auth.main).
This module is a thin client that calls its HTTP API.
"""

import asyncio
import base64
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict

logger = logging.getLogger("g2g.auth")


@dataclass
class G2GAuthData:
    jwt_token: str
    user_agent: str
    cookies: Dict[str, str] = field(default_factory=dict)
    seller_id: str = ""
    sendbird_session_key: str = ""
    updated_at: float = 0.0

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def build_headers(self) -> dict:
        headers = {
            "authorization": self.jwt_token,
            "user-agent": self.user_agent,
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://www.g2g.com",
            "referer": "https://www.g2g.com/",
        }
        if self.cookies:
            headers["Cookie"] = self.cookie_header()
        return headers


class G2GAuthManager:
    """Client for auth service. Fetches JWT from http://localhost:8010/auth/g2g."""

    TTL = 300  # 5 min local cache

    def __init__(self, auth_url: str = "http://localhost:8010"):
        self.auth_url = auth_url
        self._cache: Optional[G2GAuthData] = None
        self._expires_at: float = 0
        self._lock = asyncio.Lock()

    async def get_auth(self) -> G2GAuthData:
        async with self._lock:
            if self._cache and time.time() < self._expires_at:
                return self._cache
            return await self._fetch()

    async def invalidate(self):
        async with self._lock:
            self._cache = None

    async def force_refresh(self):
        """Tell auth service to re-capture JWT from browser, then fetch it."""
        from curl_cffi import requests as cffi
        try:
            cffi.post(f"{self.auth_url}/auth/g2g/refresh", timeout=30)
            logger.info("Triggered auth service JWT re-capture")
        except Exception as e:
            logger.warning("Auth service refresh request failed: %s", e)

    def get_cached(self) -> Optional[G2GAuthData]:
        if self._cache and time.time() < self._expires_at:
            return self._cache
        return None

    async def _fetch(self) -> G2GAuthData:
        from shared.config import SENDBIRD_SESSION_KEY

        def _sync_fetch():
            from curl_cffi import requests as cffi

            resp = None
            for attempt in range(6):
                try:
                    resp = cffi.get(f"{self.auth_url}/auth/g2g", timeout=30)
                    if resp.status_code == 200:
                        break
                    logger.warning("Auth service returned %d (attempt %d)",
                                   resp.status_code, attempt + 1)
                except Exception as e:
                    logger.warning("Auth service unreachable (attempt %d): %s",
                                   attempt + 1, e)
                if attempt < 5:
                    import time as _t
                    _t.sleep(10)

            if not resp or resp.status_code != 200:
                raise RuntimeError(
                    f"Auth service error: HTTP {resp.status_code if resp else 'unreachable'}")

            return resp.json()

        loop = asyncio.get_running_loop()
        j = await loop.run_in_executor(None, _sync_fetch)
        jwt_token = j["jwt_token"]

        # Extract seller_id from JWT sub claim
        seller_id = ""
        try:
            payload = jwt_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            seller_id = claims.get("sub", "")
        except Exception:
            pass

        self._cache = G2GAuthData(
            jwt_token=jwt_token,
            user_agent=j.get("user_agent", ""),
            cookies=j.get("cookies") or {},
            seller_id=seller_id,
            sendbird_session_key=j.get("sendbird_session_key", "") or SENDBIRD_SESSION_KEY,
            updated_at=time.time(),
        )
        self._expires_at = time.time() + self.TTL
        short = self._cache.jwt_token[:20] + "..." if self._cache.jwt_token else "None"
        logger.info("JWT fetched from auth service: %s", short)
        return self._cache
