"""Base worker class with DeliveryView, shutdown handling, and shared helpers."""

import asyncio
import os
import re
import signal
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Set

import discord
from discord import NotFound, InteractionResponded

from shared.constants import PROOF_DIR
from shared.database import Database
from shared.logging_config import setup_logger


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="worker_")


@contextmanager
def implicit_wait_override(driver, temp_wait: int, default: int = 10):
    """Context manager to safely override and restore implicitly_wait."""
    try:
        driver.implicitly_wait(temp_wait)
        yield
    finally:
        driver.implicitly_wait(default)


def sanitize_filename(filename: str) -> str:
    """Remove path traversal characters from filenames."""
    filename = os.path.basename(filename)
    filename = re.sub(r'[^\w.\-]', '_', filename)
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:200] + ext
    return filename


class DeliveryView(discord.ui.View):
    def __init__(self, include_fast_button: bool = True):
        super().__init__(timeout=None)
        self.include_fast = include_fast_button
        if not include_fast_button:
            for child in list(self.children):
                if hasattr(child, "custom_id") and child.custom_id == "btn_guest_arrived":
                    self.remove_item(child)

    @discord.ui.button(label="⚡ Khách vào (Ưu tiên)", style=discord.ButtonStyle.red,
                       custom_id="btn_guest_arrived", row=0)
    async def guest_arrived(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_click(interaction, action_type="fast")

    @discord.ui.button(label="🚀 Đã giao (Gửi Proof)", style=discord.ButtonStyle.green,
                       custom_id="btn_delivered", row=1)
    async def confirm_delivery(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_click(interaction, action_type="normal")

    async def handle_click(self, interaction: discord.Interaction, action_type: str):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except (NotFound, InteractionResponded):
            return
        except Exception:
            return

        thread = interaction.channel
        order_id = thread.name
        bot_user = interaction.client.user

        # Platform-specific check for fast button
        if action_type == "fast" and not self.include_fast:
            await interaction.followup.send("Nút này không khả dụng cho platform này.", ephemeral=True)
            return

        found_url = None
        found_qty = "1"

        async for msg in thread.history(limit=10, oldest_first=True):
            if msg.author == bot_user:
                url_match = re.search(r"Link:\s*[`]?(http[^`\s]+)[`]?", msg.content)
                qty_match = re.search(r"(?:lượng|trả)[^:]*:\s*\*\*([0-9,]+)\*\*", msg.content)
                if url_match:
                    found_url = url_match.group(1)
                if qty_match:
                    found_qty = qty_match.group(1)
                if found_url:
                    break

        if not found_url:
            await interaction.followup.send("Lỗi: Không tìm thấy Link!", ephemeral=True)
            return

        task_data = {
            "ctx": interaction,
            "order_id": order_id,
            "order_url": found_url,
            "delivery_qty": found_qty,
            "files": [],
            "action": "normal_delivery",
        }

        if action_type == "fast":
            await interaction.followup.send("⚡ **FAST:** Đang mở Eldorado...", ephemeral=False)
            task_data["action"] = "fast_delivery"
            await self.queue.put(task_data)
        else:
            # Download proof files
            downloaded_files = []
            PROOF_DIR.mkdir(parents=True, exist_ok=True)

            async for msg in thread.history(limit=50):
                if msg.attachments:
                    for attachment in msg.attachments:
                        if attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".mp4")):
                            safe_name = sanitize_filename(attachment.filename)
                            save_path = PROOF_DIR / f"{order_id}_{attachment.id}_{safe_name}"
                            if not save_path.exists():
                                await attachment.save(str(save_path))
                            downloaded_files.append(str(save_path))

            if not downloaded_files:
                await interaction.followup.send("Thiếu ảnh bằng chứng!", ephemeral=True)
                return

            # Deduplicate
            seen = set()
            unique_files = []
            for f in downloaded_files:
                if f not in seen:
                    unique_files.append(f)
                    seen.add(f)

            task_data["files"] = unique_files
            await interaction.followup.send(
                f"Đã nhận {len(unique_files)} file bằng chứng...", ephemeral=False
            )
            await self.queue.put(task_data)


async def lock_thread(ctx, logger, platform: str = "", order_id: str = ""):
    try:
        channel = ctx.channel
        if isinstance(channel, discord.Thread):
            if platform and order_id:
                logger.info(f"Locking thread for {order_id}")
            await ctx.followup.send("Đang khóa hồ sơ...")
            await channel.edit(locked=True)
            await asyncio.sleep(1)
            await channel.edit(archived=True)
    except Exception as e:
        logger.warning(f"Lock thread error: {e}")


def cleanup_files(files):
    for f in files:
        try:
            p = Path(f)
            if p.exists():
                p.unlink()
        except:
            pass


async def send_error(ctx, e, driver, order_id: str, logger):
    if "Unknown interaction" in str(e) or "404 Not Found" in str(e):
        return
    logger.error(f"Error for {order_id}: {e}")
    try:
        await ctx.followup.send(f"Lỗi: {str(e)[:100]}")
        driver.save_screenshot(f"error_{order_id}.png")
    except:
        pass
