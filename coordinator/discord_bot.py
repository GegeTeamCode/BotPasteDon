"""Coordinator — Discord bot that receives webhooks, creates threads, dispatches tasks to Workers.

In the future, replace this module with a Web API (FastAPI/Flask) without changing Scanners or Workers.
"""

import datetime
import json
import os
import re
import asyncio
import signal

import discord
from discord.ext import commands
import aiohttp

from shared.config import (
    BOT_TOKEN, CHANNEL_IDS,
    DATABASE_PATH,
)
from shared.database import Database
from shared.constants import ORDER_NOTIFIED, ORDER_THREAD_CREATED, ORDER_DELIVERING
from shared.logging_config import setup_logger
from shared.discord_utils import (
    extract_order_id_from_message,
    extract_order_url_from_message,
    extract_qty_from_message,
)

logger = setup_logger("coordinator")

WORKER_ELDO_URL = os.getenv("WORKER_ELDO_URL", "http://localhost:8001")
WORKER_G2G_URL = os.getenv("WORKER_G2G_URL", "http://localhost:8002")

# Dispatch retry policy: aligns with workers/g2g_worker.py PR1 schedule.
MAX_DISPATCH_ATTEMPTS = 100
DISPATCH_BACKOFF_SECONDS = (
    60, 60, 60, 60, 60,
    300, 300, 300, 300, 300,
    1800, 1800, 1800, 1800, 1800,
    3600,
)

_shutdown_event = asyncio.Event()
_bot_instance = None  # Global ref for lock_thread callback


def _dispatch_backoff(attempt_count: int) -> int:
    """attempt_count is 1-based: 1 = first retry after initial failure."""
    idx = max(0, attempt_count - 1)
    if idx >= len(DISPATCH_BACKOFF_SECONDS):
        return DISPATCH_BACKOFF_SECONDS[-1]
    return DISPATCH_BACKOFF_SECONDS[idx]


# ── DeliveryView (Discord buttons) ──

class DeliveryView(discord.ui.View):
    def __init__(self, include_fast_button: bool = True, worker_base_url: str = "",
                 platform: str = "eldorado", db=None):
        super().__init__(timeout=None)
        self.include_fast = include_fast_button
        self.worker_base_url = worker_base_url
        self.platform = platform
        self.db = db
        suffix = "_eldo" if platform == "eldorado" else "_g2g"
        for child in list(self.children):
            if hasattr(child, "custom_id"):
                child.custom_id = child.custom_id + suffix
        if not include_fast_button:
            for child in list(self.children):
                if hasattr(child, "custom_id") and "guest_arrived" in child.custom_id:
                    self.remove_item(child)

    @discord.ui.button(label="⚡ Khách vào (Ưu tiên)", style=discord.ButtonStyle.red,
                       custom_id="btn_guest_arrived", row=0)
    async def guest_arrived(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_click(interaction, action_type="fast")

    @discord.ui.button(label="🚀 Đã giao (Gửi Proof)", style=discord.ButtonStyle.green,
                       custom_id="btn_delivered", row=1)
    async def confirm_delivery(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_click(interaction, action_type="normal")

    async def _handle_click(self, interaction: discord.Interaction, action_type: str):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            return

        thread = interaction.channel
        order_id = thread.name.upper()
        bot_user = interaction.client.user

        if action_type == "fast" and not self.include_fast:
            await interaction.followup.send("Nút này chỉ dành cho Eldorado.", ephemeral=True)
            return

        # Prevent double-click: check if order already completed/delivering
        if self.db:
            from shared.constants import ORDER_DELIVERING, ORDER_COMPLETED
            existing = self.db.get_order(order_id)
            if existing and existing.get("status") in (ORDER_DELIVERING, ORDER_COMPLETED, "FAILED"):
                await interaction.followup.send(
                    f"Đơn {order_id} đã ở trạng thái {existing['status']}!", ephemeral=True
                )
                return

        # Extract order info from thread
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

        if action_type == "fast":
            await interaction.followup.send("⚡ **FAST:** Đang gửi task...", ephemeral=False)
            task_data = {
                "action": "fast_delivery",
                "order_id": order_id,
                "order_url": found_url,
                "delivery_qty": found_qty,
                "thread_id": str(thread.id),
            }
        else:
            # Download proof files
            from shared.constants import PROOF_DIR
            from workers.base_worker import sanitize_filename

            downloaded_files = []
            PROOF_DIR.mkdir(parents=True, exist_ok=True)
            async for msg in thread.history(limit=50):
                if msg.attachments:
                    for att in msg.attachments:
                        if att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".mp4")):
                            safe_name = sanitize_filename(att.filename)
                            save_path = PROOF_DIR / f"{order_id}_{att.id}_{safe_name}"
                            if not save_path.exists():
                                await att.save(str(save_path))
                            downloaded_files.append(str(save_path))

            if not downloaded_files:
                await interaction.followup.send("Thiếu ảnh bằng chứng!", ephemeral=True)
                return

            seen = set()
            unique_files = []
            for f in downloaded_files:
                if f not in seen:
                    unique_files.append(f)
                    seen.add(f)

            await interaction.followup.send(
                f"Đã nhận {len(unique_files)} file bằng chứng...", ephemeral=False
            )
            task_data = {
                "action": "normal_delivery",
                "order_id": order_id,
                "order_url": found_url,
                "delivery_qty": found_qty,
                "files": unique_files,
                "thread_id": str(thread.id),
            }

        # Dispatch to Worker via HTTP API
        success = await dispatch_task(self.worker_base_url, task_data)
        if not success:
            # Persist the task so a background loop can keep trying after
            # the worker recovers (restart, transient outage, etc.). Files
            # already on disk in PROOF_DIR are referenced by path inside
            # task_data and survive coordinator restarts.
            if self.db:
                try:
                    self.db.queue_dispatch(
                        order_id,
                        self.worker_base_url,
                        json.dumps(task_data, ensure_ascii=False),
                    )
                    await interaction.followup.send(
                        "⏳ Worker chưa phản hồi — đã đưa vào hàng đợi, "
                        "hệ thống sẽ tự retry.",
                        ephemeral=False,
                    )
                    logger.warning(
                        f"Dispatch failed for {order_id} — queued for retry "
                        f"(worker={self.worker_base_url})"
                    )
                    return
                except Exception as e:
                    logger.error(f"Failed to queue dispatch for {order_id}: {e}")
            await interaction.followup.send("❌ Không thể gửi task đến Worker!", ephemeral=True)


async def dispatch_task(worker_url: str, task_data: dict) -> bool:
    """Send task to Worker via HTTP POST."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{worker_url}/task",
                json=task_data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    logger.info(f"Task dispatched: {task_data['order_id']}")
                    return True
                else:
                    logger.error(f"Worker returned {resp.status}")
                    return False
    except Exception as e:
        logger.error(f"Failed to dispatch task: {e}")
        return False


# ── Discord Bot ──

class CoordinatorBot(commands.Bot):
    def __init__(self, db: Database):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = db
        global _bot_instance
        _bot_instance = self

    async def on_ready(self):
        logger.info(f"Coordinator logged in as {self.user}")

        # Register persistent views for both platforms
        eldo_view = DeliveryView(include_fast_button=True, worker_base_url=WORKER_ELDO_URL,
                                  platform="eldorado", db=self.db)
        g2g_view = DeliveryView(include_fast_button=False, worker_base_url=WORKER_G2G_URL,
                                 platform="g2g", db=self.db)
        self.add_view(eldo_view)
        self.add_view(g2g_view)

        # Startup recovery
        await self._recover_missed_orders()

    async def _recover_missed_orders(self):
        for channel_id in CHANNEL_IDS:
            channel = self.get_channel(channel_id)
            if not channel:
                continue
            for platform in ("eldorado", "g2g"):
                pending = self.db.get_orders_by_status(platform, ORDER_NOTIFIED)
                if not pending:
                    continue
                include_fast = platform == "eldorado"
                worker_url = WORKER_ELDO_URL if platform == "eldorado" else WORKER_G2G_URL
                logger.info(f"Recovering {len(pending)} {platform} orders in channel {channel_id}")
                async for msg in channel.history(limit=100):
                    if not msg.webhook_id:
                        continue
                    oid = extract_order_id_from_message(msg.content)
                    if not oid:
                        continue
                    oid = oid.upper()
                    for order in pending:
                        if order["order_id"] == oid:
                            try:
                                thread = await msg.create_thread(name=oid, auto_archive_duration=1440)
                                url = extract_order_url_from_message(msg.content) or order.get("order_url", "")
                                qty = extract_qty_from_message(msg.content)
                                view = DeliveryView(include_fast_button=include_fast,
                                                    worker_base_url=worker_url,
                                                    platform=platform, db=self.db)
                                await thread.send(
                                    f"Xử lý đơn: **{oid}**\n"
                                    f"🔗 Link: `{url}`\n"
                                    f"📦 Số lượng: **{qty}**\n"
                                    f"📸 Kéo thả bằng chứng vào đây rồi bấm nút.",
                                    view=view,
                                )
                                self.db.update_order_status(oid, ORDER_THREAD_CREATED,
                                                            discord_thread_id=str(thread.id))
                                logger.info(f"Recovered: {oid}")
                            except Exception as e:
                                logger.warning(f"Recovery failed for {oid}: {e}")

    async def on_message(self, message):
        if message.author == self.user:
            return
        if not message.webhook_id:
            return
        if message.channel.id not in CHANNEL_IDS:
            return

        # Determine platform from URL in message content
        order_url = extract_order_url_from_message(message.content) or ""
        is_eldo = "eldorado" in order_url.lower()

        order_id = extract_order_id_from_message(message.content)
        if not order_id:
            return

        # Normalize — Eldorado IDs are uppercase in DB
        order_id = order_id.upper()

        if self.db.is_order_in_status(order_id, ORDER_THREAD_CREATED):
            return

        url = extract_order_url_from_message(message.content) or "Link_Not_Found"
        qty = extract_qty_from_message(message.content)

        include_fast = is_eldo
        worker_url = WORKER_ELDO_URL if is_eldo else WORKER_G2G_URL

        try:
            thread = await message.create_thread(name=order_id, auto_archive_duration=1440)
            view = DeliveryView(include_fast_button=include_fast,
                                worker_base_url=worker_url,
                                platform="eldorado" if is_eldo else "g2g", db=self.db)
            await thread.send(
                f"Xử lý đơn: **{order_id}**\n"
                f"🔗 Link: `{url}`\n"
                f"📦 Số lượng: **{qty}**\n"
                f"📸 Kéo thả bằng chứng vào đây rồi bấm nút.",
                view=view,
            )
            self.db.update_order_status(order_id, ORDER_THREAD_CREATED,
                                        discord_thread_id=str(thread.id),
                                        discord_channel_id=str(message.channel.id))
            logger.info(f"Thread created: {order_id}")
        except Exception as e:
            logger.error(f"Thread creation failed: {e}")

        await self.process_commands(message)

    async def lock_thread(self, thread_id: str):
        """Send completion message, then lock + archive thread."""
        try:
            thread = self.get_channel(int(thread_id))
            if thread and isinstance(thread, discord.Thread):
                await thread.send("✅ Đã trả đơn thành công!")
                await thread.edit(locked=True, archived=True)
                logger.info(f"Thread completed: {thread.name}")
                return True
        except Exception as e:
            logger.error(f"Failed to complete thread {thread_id}: {e}")
        return False

    async def update_fast_delivered(self, thread_id: str, order_id: str):
        """After fast delivery: edit message to remove 'Khách vào' button."""
        try:
            thread = self.get_channel(int(thread_id))
            if not thread or not isinstance(thread, discord.Thread):
                return
            # Find the message with buttons and edit it
            async for msg in thread.history(limit=10):
                if msg.components:
                    view = DeliveryView(include_fast_button=False,
                                        worker_base_url=WORKER_ELDO_URL,
                                        platform="eldorado", db=self.db)
                    await msg.edit(
                        content=msg.content + "\n✅ **Khách vào — Delivered**",
                        view=view,
                    )
                    await thread.send("✅ Đã bấm Delivered! Kéo thả bằng chứng rồi bấm **Đã giao**.")
                    logger.info(f"Fast delivered, buttons updated: {order_id}")
                    return
        except Exception as e:
            logger.error(f"Failed to update fast delivery buttons: {e}")


async def _retry_pending_dispatches(db: Database):
    """Background loop: keep dispatching tasks that previously failed to
    reach the worker. Survives coordinator restarts because the queue is
    SQLite-backed. Drops after MAX_DISPATCH_ATTEMPTS and logs an error so
    the operator can investigate manually."""
    while not _shutdown_event.is_set():
        try:
            await asyncio.sleep(30)
            due = db.get_due_dispatches()
            for row in due:
                order_id = row["order_id"]
                worker_url = row["worker_url"]
                attempt = (row.get("attempt_count") or 0) + 1
                try:
                    task_data = json.loads(row["task_data"])
                except Exception as e:
                    logger.error(f"Bad task_data JSON for {order_id}, dropping: {e}")
                    db.remove_dispatch(order_id)
                    continue

                ok = await dispatch_task(worker_url, task_data)
                if ok:
                    db.remove_dispatch(order_id)
                    logger.info(
                        f"Queued dispatch succeeded for {order_id} "
                        f"on attempt {attempt}"
                    )
                    continue

                if attempt >= MAX_DISPATCH_ATTEMPTS:
                    logger.error(
                        f"Dispatch retry cap hit for {order_id} after "
                        f"{attempt} attempts — dropping from queue"
                    )
                    db.remove_dispatch(order_id)
                    continue

                delay = _dispatch_backoff(attempt)
                # SQLite-friendly format: space separator (datetime('now') style),
                # so string comparison against datetime('now') sorts correctly.
                next_at = (
                    datetime.datetime.utcnow()
                    + datetime.timedelta(seconds=delay)
                ).strftime("%Y-%m-%d %H:%M:%S")
                db.mark_dispatch_attempt(
                    order_id,
                    error=f"worker unreachable (attempt {attempt})",
                    next_retry_at_iso=next_at,
                    attempt_count=attempt,
                )
                logger.warning(
                    f"Dispatch retry {attempt} failed for {order_id} — "
                    f"next in {delay}s"
                )
        except Exception as e:
            logger.error(f"Dispatch retry loop error: {e}")


async def _http_server():
    """Lightweight HTTP server for worker callbacks."""
    from aiohttp import web

    async def handle_complete(request):
        data = await request.json()
        thread_id = data.get("thread_id", "")
        success = data.get("success", False)
        order_id = data.get("order_id", "")
        action = data.get("action", "normal_delivery")
        if _bot_instance and thread_id:
            if success and action == "fast_delivery":
                # Fast delivery: remove "Khách vào" button, keep "Đã giao"
                await _bot_instance.update_fast_delivered(thread_id, order_id)
            elif success:
                await _bot_instance.lock_thread(thread_id)
                logger.info(f"Order completed & thread locked: {order_id}")
            else:
                logger.warning(f"Order failed: {order_id}")
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/complete", handle_complete)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8030)
    await site.start()
    logger.info("Callback server on port 8030")


def main():
    db = Database(DATABASE_PATH)

    def handle_signal(sig, frame):
        logger.info(f"Signal {sig}, shutting down...")
        _shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    bot = CoordinatorBot(db)

    # Start HTTP callback server alongside Discord bot
    async def run_all():
        await _http_server()

        async def heartbeat():
            while not _shutdown_event.is_set():
                db.update_heartbeat("coordinator", os.getpid())
                await asyncio.sleep(30)
        asyncio.create_task(heartbeat())
        asyncio.create_task(_retry_pending_dispatches(db))

        await bot.start(BOT_TOKEN)

    logger.info(f"Starting Coordinator (PID: {os.getpid()})")

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        _shutdown_event.set()


if __name__ == "__main__":
    main()
