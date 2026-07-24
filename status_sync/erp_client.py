"""HTTP client for the ERP status_update / pending-orders endpoints.

Dual-server aware: each order is routed to the ERP that owns it. Routing per
call, in priority order:
  1. explicit ``payload["_erp_target"]`` (set by the ERP-driven reconcile from
     the pending list the order was fetched from), else
  2. the order's game (``payload["game"]`` or looked up via ``game_resolver``)
     mapped through ``erp_target_id_for_game`` (currency games → .102), else
  3. the main target (.100).

Only targets with a configured URL are used; an unset currency target simply
means everything stays on main.
"""

import asyncio
import json

import aiohttp

from shared.config import ERP_TARGETS, erp_target_id_for_game
from shared.logging_config import setup_logger

logger = setup_logger("status_sync.erp")


def _configured_targets():
    """Ordered list of targets that have a status_update URL (main first)."""
    out = []
    for tid in ("main", "currency"):
        t = ERP_TARGETS.get(tid) or {}
        if t.get("status_update_url") or t.get("pending_orders_url"):
            out.append(t)
    return out


class ERPClient:
    """Push marketplace state changes to ERP. Retries with backoff on transient failures.
    4xx errors (validation, auth) are not retried — they require a config fix."""

    def __init__(self, targets=None, timeout_sec: int = 30, game_resolver=None):
        self.targets = targets if targets is not None else _configured_targets()
        self.timeout = timeout_sec
        # Optional callable(order_id) -> game str, used to route a status push when
        # the payload carries no explicit target/game (status_sync passes db.get_order_game).
        self.game_resolver = game_resolver
        # Back-compat: main.py checks `erp.url` and logs it. Expose main's status url.
        main = ERP_TARGETS.get("main") or {}
        self.url = main.get("status_update_url", "")

    def _target_by_id(self, tid: str) -> dict:
        for t in self.targets:
            if t.get("id") == tid:
                return t
        # Fall back to first configured (main) so a push is never silently dropped.
        return self.targets[0] if self.targets else (ERP_TARGETS.get("main") or {})

    def _pick_target(self, payload: dict) -> dict:
        tid = payload.get("_erp_target")
        if not tid:
            game = payload.get("game")
            if not game and self.game_resolver:
                try:
                    game = self.game_resolver(payload.get("external_order_id", ""))
                except Exception:
                    game = ""
            tid = erp_target_id_for_game(game or "")
        return self._target_by_id(tid)

    @staticmethod
    def _key(target: dict, platform: str) -> str:
        return target.get("key_g2g" if (platform or "").lower() == "g2g" else "key_eldo", "")

    async def push_status_update(self, payload: dict, max_retries: int = 3) -> bool:
        """POST status_update to the ERP that owns this order. Returns True on 2xx.
        payload must include: platform, external_order_id, marketplace_state."""
        target = self._pick_target(payload)
        url = target.get("status_update_url", "")
        platform = payload.get("platform", "")
        ext_id = payload.get("external_order_id", "")
        state = payload.get("marketplace_state", "")
        if not url:
            logger.error("status_update url not configured for target %s (%s/%s)",
                         target.get("id"), platform, ext_id)
            return False
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self._key(target, platform),
        }
        # Strip internal routing hint before sending.
        body_payload = {k: v for k, v in payload.items() if k != "_erp_target"}
        last_err = ""
        for attempt in range(1, max_retries + 1):
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.post(
                        url, json=body_payload, headers=headers,
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
                                "ERP push OK [%s] %s/%s state=%s -> %s",
                                target.get("id"), platform, ext_id, state, msg,
                            )
                            return True
                        if 400 <= resp.status < 500:
                            # Validation / auth — don't retry
                            logger.warning(
                                "ERP push [%s] %s/%s rejected HTTP %d: %s",
                                target.get("id"), platform, ext_id, resp.status, body[:200],
                            )
                            return False
                        last_err = f"HTTP {resp.status}: {body[:150]}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"

            if attempt < max_retries:
                wait = 2 ** attempt
                logger.warning(
                    "ERP push [%s] %s/%s attempt %d failed (%s) — retry in %ds",
                    target.get("id"), platform, ext_id, attempt, last_err, wait,
                )
                await asyncio.sleep(wait)

        logger.error(
            "ERP push [%s] %s/%s GAVE UP after %d attempts: %s",
            target.get("id"), platform, ext_id, max_retries, last_err,
        )
        return False

    async def get_pending_orders(self, platform: str, limit: int = 200) -> list:
        """GET each ERP's non-terminal marketplace orders (ERP-driven reconcile).

        Fetches from every configured target and tags each order with
        ``_erp_target`` so the terminal-state push routes back to the ERP the
        order came from. Returns a merged list; empty on total failure.
        """
        merged = []
        for target in self.targets:
            url = target.get("pending_orders_url", "")
            if not url:
                continue
            headers = {"X-API-Key": self._key(target, platform)}
            params = {"platform": platform, "limit": str(limit)}
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(
                        url, params=params, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    ) as resp:
                        body = await resp.text()
                        if 200 <= resp.status < 300:
                            j = json.loads(body)
                            # Frappe wraps a whitelisted return in {"message": <value>}.
                            msg = j.get("message", j) if isinstance(j, dict) else j
                            orders = (msg or {}).get("orders", []) if isinstance(msg, dict) else []
                            for o in orders:
                                if isinstance(o, dict):
                                    o["_erp_target"] = target.get("id")
                            logger.info("ERP pending [%s] %s: %d orders",
                                        target.get("id"), platform, len(orders))
                            merged.extend(orders)
                        else:
                            logger.warning("get_pending_orders [%s] %s HTTP %d: %s",
                                           target.get("id"), platform, resp.status, body[:200])
            except Exception as e:
                logger.warning("get_pending_orders [%s] %s failed: %s",
                               target.get("id"), platform, e)
        return merged
