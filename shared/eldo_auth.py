"""Eldorado auth client — fetches cookies + XSRF from auth service.

Auth service runs as a separate process (python -m auth.main).
This module is a thin client that calls its HTTP API.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Dict

from shared.logging_config import setup_logger

logger = setup_logger("eldo.auth")


@dataclass
class EldoAuthData:
    cookies: Dict[str, str] = field(default_factory=dict)
    xsrf_token: str = ""
    user_agent: str = ""
    seller_id: str = ""
    updated_at: float = 0.0

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def build_headers(self) -> dict:
        headers = {
            "user-agent": self.user_agent,
            "accept": "application/json, text/plain, */*",
            "origin": "https://www.eldorado.gg",
            "referer": "https://www.eldorado.gg/",
        }
        if self.xsrf_token:
            headers["x-xsrf-token"] = self.xsrf_token
        if self.cookies:
            headers["Cookie"] = self.cookie_header()
        return headers


class EldoAuthManager:
    """Client for auth service. Fetches Eldorado auth from http://localhost:8010/auth/eldo."""

    TTL = 300  # 5 min local cache

    def __init__(self, auth_url: str = "http://localhost:8010"):
        self.auth_url = auth_url
        self._cache: Optional[EldoAuthData] = None
        self._expires_at: float = 0
        self._lock = asyncio.Lock()

    async def get_auth(self) -> EldoAuthData:
        async with self._lock:
            if self._cache and time.time() < self._expires_at:
                return self._cache
            return await self._fetch()

    async def invalidate(self):
        async with self._lock:
            self._cache = None

    def get_cached(self) -> Optional[EldoAuthData]:
        if self._cache and time.time() < self._expires_at:
            return self._cache
        return None

    async def _fetch(self) -> EldoAuthData:
        import asyncio as _aio

        def _sync_fetch():
            from curl_cffi import requests as cffi

            resp = None
            for attempt in range(6):
                try:
                    resp = cffi.get(f"{self.auth_url}/auth/eldo", timeout=30)
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
                    f"Auth service error: HTTP {resp.status_code if resp else 'unreachable'}"
                )

            return resp.json()

        loop = _aio.get_running_loop()
        j = await loop.run_in_executor(None, _sync_fetch)

        cookies = j.get("cookies") or {}
        xsrf = j.get("xsrf_token", "")
        seller_id = j.get("seller_id", "") or j.get("user_id", "")

        self._cache = EldoAuthData(
            cookies=cookies,
            xsrf_token=xsrf,
            user_agent=j.get("user_agent", ""),
            seller_id=seller_id,
            updated_at=time.time(),
        )
        self._expires_at = time.time() + self.TTL
        logger.info("Eldorado auth fetched: cookies=%d | xsrf=%s | seller=%s",
                     len(cookies), "yes" if xsrf else "no",
                     seller_id[:8] + "..." if seller_id else "none")
        return self._cache
