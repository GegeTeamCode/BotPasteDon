"""HTTP client for ERP status_update endpoint."""

import asyncio
import json
from typing import Optional

import aiohttp

from shared.config import ERP_API_KEY_ELDO, ERP_API_KEY_G2G, ERP_STATUS_UPDATE_URL
from shared.logging_config import setup_logger

logger = setup_logger("status_sync.erp")


class ERPClient:
    """Push marketplace state changes to ERP. Retries with backoff on transient failures.
    4xx errors (validation, auth) are not retried — they require a config fix."""

    def __init__(self, url: str = "", timeout_sec: int = 30):
        self.url = url or ERP_STATUS_UPDATE_URL
        self.timeout = timeout_sec

    def _key(self, platform: str) -> str:
        return ERP_API_KEY_G2G if (platform or "").lower() == "g2g" else ERP_API_KEY_ELDO

    async def push_status_update(self, payload: dict, max_retries: int = 3) -> bool:
        """POST status_update. Returns True on 2xx, False otherwise (and logs reason).
        payload must include: platform, external_order_id, marketplace_state."""
        if not self.url:
            logger.error("ERP_STATUS_UPDATE_URL not configured")
            return False
        platform = payload.get("platform", "")
        ext_id = payload.get("external_order_id", "")
        state = payload.get("marketplace_state", "")
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self._key(platform),
        }
        last_err = ""
        for attempt in range(1, max_retries + 1):
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(
                        self.url, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as resp:
                        body = await resp.text()
                        if 200 <= resp.status < 300:
                            try:
                                j = json.loads(body)
                                msg = j.get("message", j)
                            except Exception:
                                msg = body[:100]
                            logger.info(
                                "ERP push OK %s/%s state=%s -> %s",
                                platform, ext_id, state, msg,
                            )
                            return True
                        if 400 <= resp.status < 500:
                            # Validation / auth — don't retry
                            logger.warning(
                                "ERP push %s/%s rejected HTTP %d: %s",
                                platform, ext_id, resp.status, body[:200],
                            )
                            return False
                        last_err = f"HTTP {resp.status}: {body[:150]}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"

            if attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(
                    "ERP push %s/%s attempt %d failed (%s) — retry in %ds",
                    platform, ext_id, attempt, last_err, wait,
                )
                await asyncio.sleep(wait)

        logger.error(
            "ERP push %s/%s GAVE UP after %d attempts: %s",
            platform, ext_id, max_retries, last_err,
        )
        return False
