"""Discord webhook utilities — shared between scanners and workers."""

import re
import asyncio
from typing import Optional, Tuple

import aiohttp
from shared.logging_config import setup_logger

logger = setup_logger("discord_utils")


def format_order_message(order_data: dict, show_labels: bool = False) -> str:
    lines = []

    # Platform + Customer
    meta = []
    if order_data.get("platform"):
        platform = order_data["platform"]
        if platform.lower() == "eldorado":
            platform = f"⚡ {platform}"
        meta.append(platform)
    if order_data.get("customerName"):
        meta.append(order_data["customerName"])
    if meta:
        lines.append(f"**{' | '.join(meta)}**")

    # Order ID with link
    if order_data.get("orderId"):
        url = order_data.get("url", "")
        if url:
            lines.append(f"[{order_data['orderId']}](<{url}>)")
        else:
            lines.append(f"{order_data['orderId']}")

    # Item + Quantity
    item_info = []
    if order_data.get("itemName"):
        item_info.append(order_data["itemName"])
    if order_data.get("quantity"):
        item_info.append(order_data["quantity"])
    if item_info:
        lines.append(" | ".join(item_info))

    # Character
    if order_data.get("character"):
        lines.append(order_data["character"])

    return "\n".join(lines)


def match_webhook(
    game_name: str, item_name: str, webhook_config: dict
) -> Tuple[Optional[str], str]:
    """Match order to the correct webhook URL. Returns (url, game_label)."""
    mappings = webhook_config.get("mappings", [])
    default_webhook = webhook_config.get("default", "")

    order_text = f"{game_name or ''} {item_name or ''}".lower()

    for mapping in mappings:
        keywords = mapping.get("keywords", [])
        for keyword in keywords:
            if keyword.lower() in order_text:
                return mapping.get("url", ""), mapping.get("game", "Unknown")

    if default_webhook:
        return default_webhook, "Default"

    return None, ""


async def send_discord_webhook(
    webhook_url: str,
    content: str,
    order_data: dict = None,
    max_retries: int = 3,
) -> bool:
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                payload = {"content": content}
                async with session.post(webhook_url, json=payload) as resp:
                    if resp.status in (200, 204):
                        return True
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", "2"))
                        logger.warning(f"Rate limited, retrying after {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue
                    logger.error(f"Webhook error: {resp.status}")
        except Exception as e:
            logger.error(f"Webhook exception (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

    return False


async def send_erp_webhook(
    order_data: dict,
    webhook_url: str,
    api_key: str,
    max_retries: int = 2,
) -> bool:
    """Send order data to ERP webhook. Non-blocking — failures don't affect scanner."""
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
                        body = await resp.json()
                        msg = body.get("message", body)
                        status = msg.get("status", "")
                        if status == "ok":
                            logger.info(f"ERP accepted: {order_data.get('orderId')} -> {msg.get('sell_order', '')}")
                        elif status == "duplicate":
                            logger.debug(f"ERP duplicate: {order_data.get('orderId')}")
                        return True
                    if resp.status in (401, 412):
                        logger.error("ERP auth failed: check API key")
                        return False
                    logger.warning(f"ERP error {resp.status} (attempt {attempt + 1})")
        except Exception as e:
            logger.warning(f"ERP webhook failed (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
    return False


def extract_order_id_from_message(content: str) -> Optional[str]:
    match = re.search(r"\[([A-Za-z0-9\-]+)\]\(", content)
    return match.group(1) if match else None


def extract_order_url_from_message(content: str) -> Optional[str]:
    match = re.search(r"\((?:<)?(https?://[^\)>]+)(?:>)?\)", content)
    return match.group(1) if match else None


def extract_qty_from_message(content: str) -> str:
    match = re.search(r"\|\s*([0-9,]+)", content)
    if match:
        return match.group(1).replace(",", "").replace(".", "")
    return "1"
