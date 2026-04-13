# GegeOrder.py - Bot Discord tích hợp Auto Scanner
# Version 2.0 - 4 Drivers: 2 Scanner + 2 Worker

import discord
from discord.ext import commands
import re
import os
import asyncio
import config
from driver_manager import get_driver
from order_queue import queue_eldo, queue_g2g, process_worker_eldo, process_worker_g2g, DeliveryView
from order_scanner import OrderScanner, format_order_message, send_discord_webhook, shutdown_executor

# ==========================================
# KHỞI TẠO 4 TRÌNH DUYỆT (2 Scanner + 2 Worker)
# ==========================================
print("=" * 60)
print("🚀 Đang khởi động 4 Chrome Drivers...")
print("=" * 60)

# --- DRIVERS CHO WORKER (TRẢ HÀNG) ---
print("📦 [1/4] Driver Eldorado Worker...")
driver_eldo_worker = get_driver("chrome_profile_eldo_worker")

print("📦 [2/4] Driver G2G Worker...")
driver_g2g_worker = get_driver("chrome_profile_g2g_worker")

# --- DRIVERS CHO SCANNER (QUÉT ĐƠN) ---
print("🔍 [3/4] Driver Eldorado Scanner...")
driver_eldo_scanner = get_driver("chrome_profile_eldo_scanner")
# Mở ngay trang danh sách đơn Eldorado
driver_eldo_scanner.get("https://www.eldorado.gg/dashboard/orders/sold?orderState=PendingDelivery&displayFilter=DisplaySellingOrders")
print("   ✅ Đã mở trang danh sách Eldorado")

print("🔍 [4/4] Driver G2G Scanner...")
driver_g2g_scanner = get_driver("chrome_profile_g2g_scanner")
# Mở ngay trang danh sách đơn G2G
driver_g2g_scanner.get("https://www.g2g.com/g2g-user/sale?status=preparing")
print("   ✅ Đã mở trang danh sách G2G")

print("✅ Đã khởi động xong 4 Drivers!")

# Alias để tương thích
driver_eldo = driver_eldo_worker
driver_g2g = driver_g2g_worker

if not os.path.exists("proofs"):
    os.makedirs("proofs")

# ==========================================
# KHỞI TẠO BOT
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================
# SCANNER INSTANCES
# ==========================================
scanner_eldo: OrderScanner = None
scanner_g2g: OrderScanner = None
scanner_tasks = []

# ==========================================
# HELPER: GỬI WEBHOOK
# ==========================================
async def send_order_webhook(order_data: dict):
    """Gửi đơn hàng đến Discord Webhook - Tìm đúng kênh theo game"""
    webhook_config = config.SCANNER_CONFIG.get("webhooks", {})
    mappings = webhook_config.get("mappings", [])
    default_webhook = webhook_config.get("default", "")

    # Lấy thông tin để match
    game_name = (order_data.get("game") or "").lower().strip()
    item_name = (order_data.get("itemName") or "").lower()
    order_text = f"{game_name} {item_name}".lower()

    print(f"🔍 [Webhook] Matching game: '{game_name}', item: '{item_name}'")

    # Tìm webhook phù hợp
    target_webhook = None
    matched_game = None

    for mapping in mappings:
        keywords = mapping.get("keywords", [])
        # Kiểm tra từng keyword
        for keyword in keywords:
            if keyword.lower() in order_text:
                target_webhook = mapping.get("url", "")
                matched_game = mapping.get("game", "Unknown")
                break
        if target_webhook:
            break

    # Fallback to default
    if not target_webhook:
        if default_webhook:
            target_webhook = default_webhook
            matched_game = "Default"
            print(f"⚠️ [Webhook] Không match game, dùng default")
        else:
            print(f"❌ [Webhook] Không tìm thấy webhook cho: {game_name}")
            return False

    print(f"✅ [Webhook] Gửi đến kênh: {matched_game}")

    # Format message
    fields_config = config.SCANNER_CONFIG.get("fields", {})
    message = format_order_message(order_data, fields_config.get("showLabels", False))

    # Gửi webhook
    return await send_discord_webhook(target_webhook, message, order_data)

# ==========================================
# SCANNER CALLBACK
# ==========================================
async def on_order_scanned(order_data: dict):
    """Callback khi scanner tìm thấy đơn hàng mới"""
    platform = order_data.get("platform", "Unknown")
    order_id = order_data.get("orderId", "Unknown")
    print(f"🎯 [{platform}] Đơn hàng mới: {order_id}")
    # Gửi webhook
    await send_order_webhook(order_data)

# ==========================================
# KHỞI TẠO SCANNER
# ==========================================
async def init_scanners():
    """Khởi tạo và chạy các scanner"""
    global scanner_eldo, scanner_g2g, scanner_tasks

    scanner_config = config.SCANNER_CONFIG

    # Scanner Eldorado
    if scanner_config.get("platforms", {}).get("eldorado", True):
        scanner_eldo = OrderScanner(driver_eldo_scanner, "eldorado", scanner_config)
        # CHỈ dùng scan_callback (on_order_scanned đã gửi webhook)
        # KHÔNG dùng webhook_sender để tránh gửi 2 lần
        scanner_eldo.set_callbacks(
            scan_callback=on_order_scanned
        )

        # Chạy trong background (trang đã mở khi khởi động driver)
        task = bot.loop.create_task(scanner_eldo.start())
        scanner_tasks.append(task)
        print("✅ [ELDORADO SCANNER] Đã khởi động và bắt đầu quét!")

    # Scanner G2G
    if scanner_config.get("platforms", {}).get("g2g", True):
        scanner_g2g = OrderScanner(driver_g2g_scanner, "g2g", scanner_config)
        # CHỈ dùng scan_callback (on_order_scanned đã gửi webhook)
        # KHÔNG dùng webhook_sender để tránh gửi 2 lần
        scanner_g2g.set_callbacks(
            scan_callback=on_order_scanned
        )

        # Chạy trong background (trang đã mở khi khởi động driver)
        task = bot.loop.create_task(scanner_g2g.start())
        scanner_tasks.append(task)
        print("✅ [G2G SCANNER] Đã khởi động và bắt đầu quét!")

# ==========================================
# BOT EVENTS
# ==========================================
@bot.event
async def on_ready():
    print(f'✅ Logged in as {bot.user}')
    print(f'✅ Hệ thống sẵn sàng:')
    print(f'   - 2 Worker drivers (trả hàng)')
    print(f'   - 2 Scanner drivers (quét đơn)')

    bot.add_view(DeliveryView())

    # --- CHẠY 2 WORKER XỬ LÝ ĐƠN HÀNG ---
    bot.loop.create_task(process_worker_eldo(driver_eldo_worker))
    bot.loop.create_task(process_worker_g2g(driver_g2g_worker))

    # --- KHỞI TẠO SCANNER (NẾU BẬT) ---
    if config.SCANNER_CONFIG.get("auto_start", False):
        await init_scanners()
        print("✅ Auto Scanner đã được bật tự động")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.channel.id not in config.CHANNEL_IDS:
        return

    # Xử lý webhook messages (đơn hàng mới từ scanner)
    if message.webhook_id:
        print(f"📩 Nhận tin Webhook: {message.content[:50]}...")
        content = message.content

        id_match = re.search(r"\[([A-Za-z0-9\-]+)\]\(", content)
        link_match = re.search(r"\((?:<)?(https?://[^\)>]+)(?:>)?\)", content)
        qty_match = re.search(r"\|\s*([0-9,]+)", content)

        if id_match:
            order_id = id_match.group(1)
            order_url = link_match.group(1) if link_match else "Link_Not_Found"

            clean_qty = "1"
            if qty_match:
                clean_qty = qty_match.group(1).replace(",", "").replace(".", "")

            print(f"🎯 Data: ID={order_id} | Qty={clean_qty}")

            try:
                thread = await message.create_thread(name=order_id, auto_archive_duration=1440)

                view = DeliveryView()
                if "g2g.com" in order_url.lower():
                    for child in view.children:
                        if hasattr(child, "custom_id") and child.custom_id == "btn_guest_arrived":
                            view.remove_item(child)
                            break

                await thread.send(
                    f"👋 Xử lý đơn: **{order_id}**\n"
                    f"🔗 Link: `{order_url}`\n"
                    f"📦 Số lượng: **{clean_qty}**\n"
                    f"📸 Hãy **Kéo thả bằng chứng** vào đây rồi bấm nút.",
                    view=view
                )
            except Exception as e:
                print(f"❌ Lỗi tạo thread: {e}")

    await bot.process_commands(message)

# ==========================================
# DISCORD COMMANDS - SCANNER CONTROL
# ==========================================
@bot.command(name="scan_start")
async def cmd_scan_start(ctx):
    """Bật Auto Scanner"""
    global scanner_eldo, scanner_g2g

    if not ctx.author.guild_permissions.administrator:
        await ctx.send("⚠️ Bạn cần quyền Admin để dùng lệnh này!")
        return

    if (scanner_eldo and scanner_eldo.is_running) or (scanner_g2g and scanner_g2g.is_running):
        await ctx.send("⚠️ Scanner đang chạy rồi!")
        return

    await init_scanners()
    await ctx.send("✅ **Auto Scanner đã được bật!**\n"
                   f"- Eldorado: {'✅' if config.SCANNER_CONFIG.get('platforms',{}).get('eldorado', True) else '❌'}\n"
                   f"- G2G: {'✅' if config.SCANNER_CONFIG.get('platforms',{}).get('g2g', True) else '❌'}")

@bot.command(name="scan_stop")
async def cmd_scan_stop(ctx):
    """Tắt Auto Scanner"""
    global scanner_eldo, scanner_g2g

    if not ctx.author.guild_permissions.administrator:
        await ctx.send("⚠️ Bạn cần quyền Admin để dùng lệnh này!")
        return

    stopped = []

    if scanner_eldo and scanner_eldo.is_running:
        scanner_eldo.stop()
        stopped.append("Eldorado")

    if scanner_g2g and scanner_g2g.is_running:
        scanner_g2g.stop()
        stopped.append("G2G")

    if stopped:
        await ctx.send(f"⏹ **Scanner đã dừng:** {', '.join(stopped)}")
    else:
        await ctx.send("⚠️ Không có Scanner nào đang chạy!")

@bot.command(name="scan_status")
async def cmd_scan_status(ctx):
    """Xem trạng thái Scanner"""
    embed = discord.Embed(
        title="🔍 Auto Scanner Status",
        color=discord.Color.blue()
    )

    # Eldorado status
    eldo_status = "❌ Dừng"
    if scanner_eldo and scanner_eldo.is_running:
        eldo_status = f"✅ Đang chạy ({len(scanner_eldo.get_processed_orders())} đơn đã quét)"

    # G2G status
    g2g_status = "❌ Dừng"
    if scanner_g2g and scanner_g2g.is_running:
        g2g_status = f"✅ Đang chạy ({len(scanner_g2g.get_processed_orders())} đơn đã quét)"

    embed.add_field(name="Eldorado", value=eldo_status, inline=True)
    embed.add_field(name="G2G", value=g2g_status, inline=True)

    # Config
    config_text = (
        f"Whitelist: `{config.SCANNER_CONFIG.get('whitelist', 'All')}`\n"
        f"Blacklist: `{config.SCANNER_CONFIG.get('blacklist', 'None')}`\n"
        f"Interval: {config.SCANNER_CONFIG.get('scan_interval_min', 15)}-{config.SCANNER_CONFIG.get('scan_interval_max', 25)}s"
    )
    embed.add_field(name="Config", value=config_text, inline=False)

    await ctx.send(embed=embed)

@bot.command(name="scan_clear")
async def cmd_scan_clear(ctx):
    """Xóa cache đơn hàng đã quét"""
    global scanner_eldo, scanner_g2g

    if not ctx.author.guild_permissions.administrator:
        await ctx.send("⚠️ Bạn cần quyền Admin để dùng lệnh này!")
        return

    cleared = []

    if scanner_eldo:
        scanner_eldo.processed_orders.clear()
        cleared.append("Eldorado")

    if scanner_g2g:
        scanner_g2g.processed_orders.clear()
        cleared.append("G2G")

    if cleared:
        await ctx.send(f"🧹 **Đã xóa cache:** {', '.join(cleared)}")
    else:
        await ctx.send("⚠️ Không có Scanner nào để xóa cache!")

@bot.command(name="scan_test")
async def cmd_scan_test(ctx, platform: str = "eldorado"):
    """Test quét 1 lần"""
    global scanner_eldo, scanner_g2g

    platform = platform.lower()

    if platform in ["eldo", "eldorado"]:
        if scanner_eldo:
            await ctx.send("🔍 Đang test quét Eldorado...")
            orders = await scanner_eldo.scan_order_list()
            if orders:
                msg = f"📋 Tìm thấy {len(orders)} đơn hàng:\n" + "\n".join([f"- {o['id']}" for o in orders[:5]])
                await ctx.send(msg[:1900])
            else:
                await ctx.send("📭 Không tìm thấy đơn hàng nào!")
        else:
            await ctx.send("⚠️ Scanner Eldorado chưa khởi động! Dùng `!scan_start` trước.")

    elif platform == "g2g":
        if scanner_g2g:
            await ctx.send("🔍 Đang test quét G2G...")
            orders = await scanner_g2g.scan_order_list()
            if orders:
                msg = f"📋 Tìm thấy {len(orders)} đơn hàng:\n" + "\n".join([f"- {o['id']}" for o in orders[:5]])
                await ctx.send(msg[:1900])
            else:
                await ctx.send("📭 Không tìm thấy đơn hàng nào!")
        else:
            await ctx.send("⚠️ Scanner G2G chưa khởi động! Dùng `!scan_start` trước.")
    else:
        await ctx.send("⚠️ Platform không hợp lệ! Dùng `eldorado` hoặc `g2g`")

@bot.command(name="help_scan")
async def cmd_help_scan(ctx):
    """Hiển thị hướng dẫn Scanner"""
    embed = discord.Embed(
        title="📖 Hướng dẫn Auto Scanner",
        description="Tự động quét đơn hàng từ G2G và Eldorado",
        color=discord.Color.green()
    )

    commands_text = """
**Lệnh Scanner:**
`!scan_start` - Bật Auto Scanner
`!scan_stop` - Tắt Auto Scanner
`!scan_status` - Xem trạng thái
`!scan_clear` - Xóa cache đơn đã quét
`!scan_test [platform]` - Test quét 1 lần

**Cấu hình trong config.py:**
- `whitelist`: Chỉ lấy đơn chứa từ này
- `blacklist`: Bỏ qua đơn chứa từ này
- `platforms`: Bật/tắt G2G, Eldorado
- `auto_start`: Tự động chạy khi bot khởi động
"""
    embed.add_field(name="Commands", value=commands_text, inline=False)

    await ctx.send(embed=embed)

# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    print("=" * 60)
    print("🤖 GeGe Order Bot v2.0")
    print("   - 4 Drivers (2 Scanner + 2 Worker)")
    print("   - Auto Scanner tích hợp")
    print("=" * 60)

    try:
        bot.run(config.BOT_TOKEN)
    except KeyboardInterrupt:
        print("\n⏹ Đang dừng...")
        if scanner_eldo:
            scanner_eldo.stop()
        if scanner_g2g:
            scanner_g2g.stop()
    finally:
        # Đóng tất cả 4 drivers
        print("🔒 Đang đóng 4 Chrome drivers...")
        shutdown_executor()
        try:
            driver_eldo_worker.quit()
            driver_g2g_worker.quit()
            driver_eldo_scanner.quit()
            driver_g2g_scanner.quit()
        except:
            pass
        print("✅ Đã thoát!")
