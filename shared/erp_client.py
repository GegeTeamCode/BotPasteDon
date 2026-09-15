"""ERP webhook client — pushes scanned orders to the ERP new_order endpoint."""

import asyncio

import aiohttp
from shared.logging_config import setup_logger

logger = setup_logger("erp_client")


async def send_erp_webhook(
    order_data: dict,
    webhook_url: str,
    api_key: str,
    max_retries: int = 3,
) -> bool:
    """Send order data to ERP webhook with retry and exponential backoff."""
    if not webhook_url or not api_key:
        return False
    # Debug: log exact pricing values
    logger.info(f"ERP payload prices: orderId={order_data.get('orderId')} "
                f"unit_price={order_data.get('unit_price')} "
                f"total_price={order_data.get('total_price')} "
                f"earning={order_data.get('earning')} "
                f"channel_fee={order_data.get('channel_fee')} "
                f"channel_fee_rate={order_data.get('channel_fee_rate')}")
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url,
                    json=order_data,
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key": api_key,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        try:
                            body = await resp.json()
                            msg = body.get("message", body)
                            if not isinstance(msg, dict):
                                msg = {}
                            status = msg.get("status", "")
                        except Exception:
                            msg, status = {}, ""
                        if status == "ok":
                            logger.info(f"ERP accepted: {order_data.get('orderId')} -> {msg.get('sell_order', '')}")
                            return True
                        if status == "duplicate":
                            logger.debug(f"ERP duplicate: {order_data.get('orderId')}")
                            return True
                        # 200 but ERP did NOT create/accept the order (e.g. an
                        # unexpected skipped/error/ignored status). Do NOT mark
                        # synced — treating ANY 200 as success was the false-sync
                        # bug (erp_synced=1 for an order ERP never created). Surface
                        # it and leave it unsynced so erp_retry_loop re-attempts.
                        logger.warning(
                            f"ERP 200 non-success status={status!r} for "
                            f"{order_data.get('orderId')} — NOT marking synced")
                        return False
                    if resp.status in (401, 412):
                        logger.error("ERP auth failed: check API key")
                        return False
                    logger.warning(f"ERP error {resp.status} (attempt {attempt + 1})")
        except Exception as e:
            logger.warning(f"ERP webhook failed (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** (attempt + 1))
    return False
