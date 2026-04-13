import asyncio
import os
import re
import discord
from discord import NotFound, InteractionResponded
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from talkjs_client import TalkJSClient

# --- KHỞI TẠO 2 HÀNG ĐỢI ---
queue_eldo = asyncio.Queue()
queue_g2g = asyncio.Queue()

# --- HELPER LOG FUNCTION ---
def log(platform: str, order_id: str, message: str):
    """Log với format: [PLATFORM][ORDER_ID] message"""
    print(f"[{platform}][{order_id}] {message}")

# --- QUẢN LÝ TRẠNG THÁI ĐANG XỬ LÝ (Tránh Double Click/Double Process) ---
PROCESSING_TASKS = set()

# --- TALKJS CLIENT (WebSocket) ---
talkjs_client: TalkJSClient = None

# ==========================================
# CLASS DELIVERY VIEW
# ==========================================
class DeliveryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚡ Khách vào (Ưu tiên)", style=discord.ButtonStyle.red, custom_id="btn_guest_arrived", row=0)
    async def guest_arrived(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_click(interaction, action_type="fast")

    @discord.ui.button(label="🚀 Đã giao (Gửi Proof)", style=discord.ButtonStyle.green, custom_id="btn_delivered", row=1)
    async def confirm_delivery(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_click(interaction, action_type="normal")

    async def handle_click(self, interaction: discord.Interaction, action_type: str):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except (NotFound, InteractionResponded): return 
        except Exception: return

        thread = interaction.channel
        order_id = thread.name
        bot_user = interaction.client.user 
        
        # Kiểm tra xem đơn này có đang chạy không để tránh spam
        if order_id in PROCESSING_TASKS and action_type == "normal":
            await interaction.followup.send(f"⚠️ Đơn {order_id} đang được xử lý rồi, vui lòng đợi!", ephemeral=True)
            return

        found_url = None
        found_qty = "1"
        
        async for msg in thread.history(limit=10, oldest_first=True):
            if msg.author == bot_user:
                url_match = re.search(r"Link:\s*[`]?(http[^`\s]+)[`]?", msg.content)
                qty_match = re.search(r"(?:lượng|trả)[^:]*:\s*\*\*([0-9,]+)\*\*", msg.content)
                if url_match: found_url = url_match.group(1)
                if qty_match: found_qty = qty_match.group(1)
                if found_url: break
        
        if not found_url:
            await interaction.followup.send("❌ Lỗi: Không tìm thấy Link!", ephemeral=True)
            return

        is_eldorado = "eldorado" in found_url.lower()

        task_data = {
            'ctx': interaction,
            'order_id': order_id,
            'order_url': found_url,
            'delivery_qty': found_qty,
            'files': [],
            'action': 'normal_delivery'
        }

        if action_type == "fast":
            if not is_eldorado:
                await interaction.followup.send("⚠️ Nút này chỉ dành cho Eldorado!", ephemeral=True)
                return
            await interaction.followup.send(f"⚡ **FAST:** Đang mở Eldorado...", ephemeral=False)
            task_data['action'] = 'fast_delivery'
            await queue_eldo.put(task_data)
        else:
            downloaded_files = []
            # Tải file từ Discord
            async for msg in thread.history(limit=50):
                if msg.attachments:
                    for attachment in msg.attachments:
                        if attachment.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.mp4')):
                            if not os.path.exists("proofs"): os.makedirs("proofs")
                            save_path = f"proofs/{order_id}_{attachment.id}_{attachment.filename}"
                            if not os.path.exists(save_path):
                                await attachment.save(save_path)
                            downloaded_files.append(os.path.abspath(save_path))
            
            if not downloaded_files:
                await interaction.followup.send("⚠️ Thiếu ảnh bằng chứng!", ephemeral=True)
                return
            
            # Lọc trùng lặp đường dẫn
            seen = set()
            unique_files = []
            for f in downloaded_files:
                if f not in seen:
                    unique_files.append(f)
                    seen.add(f)
            
            task_data['files'] = unique_files
            await interaction.followup.send(f"📥 Đã nhận {len(unique_files)} file bằng chứng...", ephemeral=False)

            if is_eldorado: await queue_eldo.put(task_data)
            else: await queue_g2g.put(task_data)


# ==========================================
# WORKER 1: ELDORADO
# ==========================================
async def process_worker_eldo(driver):
    global talkjs_client
    print("=" * 60)
    print("🔄 [ELDORADO] Worker đã sẵn sàng!")
    print("=" * 60)

    # Khởi tạo TalkJS client
    talkjs_client = TalkJSClient(driver)

    while True:
        task_data = await queue_eldo.get()
        order_id = task_data['order_id']

        if order_id in PROCESSING_TASKS and task_data['action'] != 'fast_delivery':
            log("ELDO", order_id, "🚫 Skip duplicate task")
            queue_eldo.task_done()
            continue

        if task_data['action'] != 'fast_delivery':
            PROCESSING_TASKS.add(order_id)

        ctx = task_data['ctx']
        order_url = task_data['order_url']
        action = task_data['action']

        try:
            if len(driver.window_handles) > 1:
                driver.switch_to.window(driver.window_handles[0])

            await asyncio.sleep(0.1)

            # Lưu ý: Kết nối TalkJS WebSocket sẽ được thực hiện trong handle_eldorado
            # sau khi trang đã load và iframe xuất hiện

            if action == 'fast_delivery':
                log("ELDO", order_id, "⚡ FAST mode - Checking...")
                await ctx.followup.send(f"⚡ (Eldo) Checking...")
                status = await handle_eldorado_fast(driver, order_url, order_id)

                if status == "success":
                    log("ELDO", order_id, "✅ ĐÃ BẤM DELIVERED!")
                    await ctx.followup.send(f"✅ **ĐÃ BẤM DELIVERED!**")
                    try:
                        view = DeliveryView()
                        for child in view.children:
                            if hasattr(child, "custom_id") and child.custom_id == "btn_guest_arrived":
                                view.remove_item(child)
                                break
                        await ctx.message.edit(view=view)
                    except: pass
                elif status == "already_done":
                    log("ELDO", order_id, "ℹ️ Đơn đã xong từ trước")
                    await ctx.followup.send(f"ℹ️ Đơn đã xong từ trước.")
                else:
                    log("ELDO", order_id, f"⚠️ Trạng thái: {status}")
                    await ctx.followup.send(f"⚠️ Trạng thái: {status}")

            else:
                num_files = len(task_data['files'])
                log("ELDO", order_id, f"⚙️ Bắt đầu upload {num_files} files...")
                await ctx.followup.send(f"⚙️ (Eldo) Uploading {num_files} files...", ephemeral=False)

                # Gọi hàm xử lý (Hybrid: WebSocket + UI)
                uploaded_count = await handle_eldorado(driver, order_url, task_data['delivery_qty'], task_data['files'], order_id)

                if uploaded_count == num_files:
                    log("ELDO", order_id, f"✅ XONG! Đã upload đủ {uploaded_count}/{num_files} files")
                    await ctx.followup.send(f"✅ **XONG!** Đã trả đủ {uploaded_count}/{num_files} bằng chứng.")
                    cleanup_files(task_data['files'])
                    await lock_thread(ctx, "ELDO", order_id)
                else:
                    log("ELDO", order_id, f"⚠️ CẢNH BÁO: Chỉ upload được {uploaded_count}/{num_files} files")
                    await ctx.followup.send(f"⚠️ **CẢNH BÁO:** Sau khi thử lại vẫn chỉ up được {uploaded_count}/{num_files} file. Hãy kiểm tra bằng tay!")

        except Exception as e:
            await send_error(ctx, e, driver, task_data['order_id'], "ELDO")
        finally:
            if task_data['action'] != 'fast_delivery' and order_id in PROCESSING_TASKS:
                PROCESSING_TASKS.remove(order_id)
            queue_eldo.task_done()

# ==========================================
# WORKER 2: G2G
# ==========================================
async def process_worker_g2g(driver):
    print("=" * 60)
    print("🔄 [G2G] Worker đã sẵn sàng!")
    print("=" * 60)
    while True:
        task_data = await queue_g2g.get()
        order_id = task_data['order_id']

        if order_id in PROCESSING_TASKS:
             log("G2G", order_id, "🚫 Skip duplicate task")
             queue_g2g.task_done()
             continue
        PROCESSING_TASKS.add(order_id)

        ctx = task_data['ctx']
        order_url = task_data['order_url']

        try:
            if len(driver.window_handles) > 1:
                driver.switch_to.window(driver.window_handles[0])

            await asyncio.sleep(0.1)

            log("G2G", order_id, f"⚙️ Bắt đầu xử lý - {len(task_data['files'])} files")
            await ctx.followup.send(f"⚙️ (G2G) Uploading...", ephemeral=False)
            await handle_g2g(driver, order_url, task_data['delivery_qty'], task_data['files'], order_id)

            log("G2G", order_id, "✅ XONG!")
            await ctx.followup.send(f"✅ **XONG!** Đã trả đơn G2G.")
            cleanup_files(task_data['files'])
            await lock_thread(ctx, "G2G", order_id)

        except Exception as e:
            await send_error(ctx, e, driver, task_data['order_id'], "G2G")
        finally:
            if order_id in PROCESSING_TASKS:
                PROCESSING_TASKS.remove(order_id)
            queue_g2g.task_done()

# --- Helper Functions ---
async def lock_thread(ctx, platform: str = "", order_id: str = ""):
    try:
        channel = ctx.channel
        if isinstance(channel, discord.Thread):
            if platform and order_id:
                log(platform, order_id, "🔒 Đang khóa thread...")
            await ctx.followup.send("🔒 Đang khóa hồ sơ...")
            await channel.edit(locked=True)
            await asyncio.sleep(1)
            await channel.edit(archived=True)
    except Exception as e:
        if platform and order_id:
            log(platform, order_id, f"⚠️ Lỗi Lock Thread: {e}")
        else:
            print(f"⚠️ Lỗi Lock Thread: {e}")

def cleanup_files(files):
    for f in files:
        try:
            if os.path.exists(f): os.remove(f)
        except: pass

async def send_error(ctx, e, driver, order_id, platform: str = ""):
    if "Unknown interaction" in str(e) or "404 Not Found" in str(e): return
    if platform:
        log(platform, order_id, f"❌ Lỗi: {e}")
    else:
        print(f"❌ Err {order_id}: {e}")
    try:
        await ctx.followup.send(f"❌ Lỗi: {str(e)[:100]}")
        driver.save_screenshot(f"error_{order_id}.png")
    except: pass


# ==========================================
# ELDORADO FAST (F5 LOOP)
# ==========================================
async def handle_eldorado_fast(driver, url, order_id: str = ""):
    log("ELDO", order_id, f"🚀 FAST mode - {url}")
    driver.switch_to.window(driver.window_handles[0])
    driver.get(url)

    TAG_ACTION = "eld-seller-deliver-item"
    TAG_WAITING = "eld-seller-waiting-buyer-response"
    TAG_COMPLETED = "eld-seller-order-completed"
    BTN_XPATH = "//button[@data-testid='order-page-seller-order-delivered-button-xm0D']"

    for attempt in range(10):
        log("ELDO", order_id, f"🔄 Check State (lần {attempt+1}/10)...")
        driver.switch_to.default_content()
        state_found = False
        driver.implicitly_wait(0)

        for _ in range(60):
            if len(driver.find_elements(By.TAG_NAME, TAG_ACTION)) > 0:
                state_found = True
                try:
                    btns = driver.find_elements(By.XPATH, BTN_XPATH)
                    if len(btns) > 0:
                        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", btns[0])
                        driver.execute_script("arguments[0].click();", btns[0])
                        log("ELDO", order_id, "🖱️ Clicked Delivered -> F5")
                        await asyncio.sleep(3)
                        driver.refresh()
                        await asyncio.sleep(5)
                        break
                except: break

            elif len(driver.find_elements(By.TAG_NAME, TAG_WAITING)) > 0:
                driver.implicitly_wait(10)
                return "success"

            elif len(driver.find_elements(By.TAG_NAME, TAG_COMPLETED)) > 0:
                driver.implicitly_wait(10)
                return "already_done"

            await asyncio.sleep(0.5)

        driver.implicitly_wait(10)

        if not state_found:
            log("ELDO", order_id, "❌ Timeout -> F5")
            driver.refresh()
            await asyncio.sleep(3)

    return "timeout"


# ==========================================
# G2G - LOGIC CONFIRM LOOPS
# ==========================================
async def handle_g2g(driver, url, qty, file_paths, order_id: str = ""):
    log("G2G", order_id, f"🚀 Bắt đầu xử lý - Qty: {qty}, Files: {len(file_paths)}")
    log("G2G", order_id, f"🔗 URL: {url}")
    driver.switch_to.window(driver.window_handles[0])
    driver.get(url)
    wait = WebDriverWait(driver, 20)

    try:
        inp = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@data-attr='order-item-add-delivered-qty-input']")))
        inp.click()
        inp.clear()
        inp.send_keys(qty)
        log("G2G", order_id, f"📝 Đã nhập số lượng: {qty}")
    except Exception as e:
        log("G2G", order_id, f"⚠️ Lỗi nhập số lượng: {e}")

    # Kiểm tra có báo cáo cancel không (trước khi submit)
    has_cancel_report = bool(driver.find_elements(By.CSS_SELECTOR, "div.g-alert-box.bg-negative"))
    if has_cancel_report:
        log("G2G", order_id, "⚠️ Phát hiện Report cancel trên đơn hàng")

    try:
        btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[@data-attr='order-item-add-delivered-qty-submit-btn']")))
        driver.execute_script("arguments[0].click();", btn)
        log("G2G", order_id, "🖱️ Clicked nút submit qty")
        await asyncio.sleep(2)

        # Chỉ chờ overlay khi có cancel report
        if has_cancel_report:
            for _ in range(10):
                overlay_btns = driver.find_elements(By.XPATH, "//button[contains(., 'Continue') and contains(@class, 'bg-primary')]")
                if overlay_btns and overlay_btns[0].is_displayed():
                    driver.execute_script("arguments[0].click();", overlay_btns[0])
                    log("G2G", order_id, "✅ Clicked Continue trên overlay open case")
                    await asyncio.sleep(2)
                    break
                await asyncio.sleep(0.5)
    except Exception as e:
        log("G2G", order_id, f"⚠️ Lỗi submit số lượng: {e}")

    try:
        driver.execute_script("arguments[0].click();", wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Proof gallery')]"))))
        wait.until(EC.presence_of_element_located((By.ID, "fileUploader"))).send_keys("\n".join([os.path.abspath(f) for f in file_paths]))

        log("G2G", order_id, f"📤 Đã upload {len(file_paths)} files, chờ 4s...")
        await asyncio.sleep(4)

        log("G2G", order_id, "🖱️ Checking Confirm Button loop...")
        for i in range(20):
            try:
                sub_btns = driver.find_elements(By.XPATH, "//button[@data-attr='order-item-delivery-proof-dialog-submit-btn']")
                if len(sub_btns) == 0:
                    log("G2G", order_id, "✅ Button gone -> Success!")
                    break
                sub = sub_btns[0]
                if sub.is_displayed() and sub.is_enabled():
                    log("G2G", order_id, f"   🖱️ Click Confirm (lần {i+1})...")
                    driver.execute_script("arguments[0].click();", sub)
                    await asyncio.sleep(1)
                else:
                    await asyncio.sleep(0.5)
            except Exception as e:
                log("G2G", order_id, f"   ⚠️ Loop error: {e}")
                await asyncio.sleep(1)

    except Exception as e: raise e

    try:
        # 1. Mở khung chat
        chat_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[@data-attr='order-item-chat-btn']")))
        driver.execute_script("arguments[0].click();", chat_btn)
        log("G2G", order_id, "💬 Mở chat...")
        await asyncio.sleep(3)

        if len(driver.window_handles) > 1:
            driver.switch_to.window(driver.window_handles[-1])
            try:
                # 2. Tìm Editor (Cái khung contenteditable)
                editor = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".ProseMirror.toastui-editor-contents")))

                # 3. Lấy nội dung tin nhắn
                msg_content = "Done"
                if os.path.exists("message.txt"):
                    with open("message.txt", "r", encoding="utf-8") as f:
                        msg_content = f.read().strip()

                # 4. CHÈN TEXT BẰNG JS THÔNG MINH (Inject + Dispatch Event)
                # Logic: Xóa sạch HTML cũ -> Gán Text mới -> Báo cho trình duyệt biết ("input")
                log("G2G", order_id, f"💉 Injecting message: {msg_content[:30]}...")

                js_injector = """
                var el = arguments[0];
                var txt = arguments[1];

                // Xóa placeholder và các thẻ rác, gán thẳng vào textContent
                // ProseMirror thường sẽ tự động bọc lại bằng thẻ <p> khi nhận sự kiện input
                el.innerText = txt;

                // QUAN TRỌNG: Giả lập sự kiện để React/Vue/G2G nhận diện thay đổi
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                """

                driver.execute_script(js_injector, editor, msg_content)
                await asyncio.sleep(1)

                # 5. Bấm nút gửi
                send_btn = driver.find_element(By.XPATH, "//button[.//i[text()='send']]")
                driver.execute_script("arguments[0].click();", send_btn)
                log("G2G", order_id, "✅ Đã gửi tin nhắn")
                await asyncio.sleep(2)

            except Exception as e:
                log("G2G", order_id, f"⚠️ Chat Error: {e}")
    except: pass
    finally:
        if len(driver.window_handles) > 1: driver.close()
        driver.switch_to.window(driver.window_handles[0])


# ==========================================
# ELDORADO NORMAL (NÂNG CẤP: WEBSOCKET + UI HYBRID)
# ==========================================
async def handle_eldorado(driver, url, qty, file_paths, order_id: str = ""):
    """
    Eldorado delivery handler - Hybrid approach:
    - UI automation cho upload file (vì Firebase auth phức tạp)
    - WebSocket cho gửi tin nhắn text (nhanh, ổn định)
    """
    # Extract order_id từ URL nếu không được truyền vào
    if not order_id:
        import re as re_module
        order_id_match = re_module.search(r'/order/([a-f0-9\-]{36})', url)
        order_id = order_id_match.group(1) if order_id_match else "unknown"

    log("ELDO", order_id, "=" * 50)
    log("ELDO", order_id, f"🟢 Bắt đầu xử lý - Qty: {qty}, Files: {len(file_paths) if file_paths else 0}")
    log("ELDO", order_id, f"🔗 URL: {url}")
    log("ELDO", order_id, "=" * 50)

    driver.switch_to.window(driver.window_handles[0])

    sent_registry = []
    total_files = len(file_paths) if file_paths else 0

    # --- GLOBAL RETRY LOOP ---
    MAX_GLOBAL_RETRIES = 3

    for global_attempt in range(MAX_GLOBAL_RETRIES):
        log("ELDO", order_id, f"🔄 Thử lần {global_attempt+1}/{MAX_GLOBAL_RETRIES}")

        # 1. Điều hướng / Refresh
        if global_attempt == 0:
            driver.get(url)
        else:
            log("ELDO", order_id, "⚠️ Upload thiếu file -> F5 và thử lại...")
            driver.refresh()
            await asyncio.sleep(5)

        # 2. Chờ trang load & Click Delivered (nếu có)
        driver.implicitly_wait(0)
        try:
            for _ in range(90):
                if len(driver.find_elements(By.TAG_NAME, "eld-seller-deliver-item")) > 0 or \
                   len(driver.find_elements(By.TAG_NAME, "eld-seller-waiting-buyer-response")) > 0:
                   break
                await asyncio.sleep(0.5)

            driver.switch_to.default_content()
            btns = driver.find_elements(By.XPATH, "//button[@data-testid='order-page-seller-order-delivered-button-xm0D']")
            if len(btns) > 0 and btns[0].is_displayed():
                 driver.execute_script("arguments[0].click();", btns[0])
                 log("ELDO", order_id, "🖱️ Clicked Delivered button")
                 await asyncio.sleep(2)
        except: pass

        # 3. UPLOAD FILES (qua UI - vì Firebase auth phức tạp)
        if file_paths:
            log("ELDO", order_id, f"⏳ Bắt đầu quy trình gửi {total_files} file...")

            for idx, file_path in enumerate(file_paths):
                file_name = os.path.basename(file_path)

                if file_name in sent_registry:
                    continue

                log("ELDO", order_id, f"   📂 [File {idx+1}/{total_files}] {file_name}")
                file_sent_ok = False

                # Tối đa 5 lần thử (F5 nếu không thấy iframe)
                for page_retry in range(5):
                    try:
                        driver.switch_to.default_content()
                        log("ELDO", order_id, f"      ⏳ Đợi TalkJS iframe... (lần {page_retry + 1}/5)")

                        # Tìm iframe TalkJS - kiểm tra mỗi 3s, tối đa 15s (5 lần)
                        iframe = None
                        iframe_selectors = [
                            "iframe[name*='talkjs']",
                            "iframe[src*='talkjs']",
                            "iframe[src*='app.talkjs.com']",
                            "iframe[name='____talkjs__chat__ui_internal']"
                        ]

                        # Kiểm tra mỗi 3s, tổng cộng 5 lần = 15s
                        for check in range(5):
                            for selector in iframe_selectors:
                                frames = driver.find_elements(By.CSS_SELECTOR, selector)
                                if len(frames) > 0:
                                    # Kiểm tra iframe đã thực sự load (không phải about:blank)
                                    src = frames[0].get_attribute("src") or ""
                                    if src and not src.startswith("about:blank"):
                                        iframe = frames[0]
                                        log("ELDO", order_id, f"      ✅ Tìm thấy iframe sau {check * 3}s")
                                        break
                            if iframe:
                                break
                            log("ELDO", order_id, f"      ⏳ Chờ iframe... ({check + 1}/5)")
                            await asyncio.sleep(3)

                        if not iframe:
                            log("ELDO", order_id, "      ⚠️ Sau 15s không thấy iframe -> F5 và thử lại...")
                            # Debug: liệt kê tất cả iframe
                            all_frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
                            log("ELDO", order_id, f"      🔍 Tìm thấy {len(all_frames)} iframes")
                            for i, f in enumerate(all_frames):
                                name = f.get_attribute("name") or "no-name"
                                src = f.get_attribute("src") or "no-src"
                                log("ELDO", order_id, f"         [{i}] name={name[:50]}, src={src[:80]}")
                            # F5 và thử lại
                            driver.refresh()
                            await asyncio.sleep(3)
                            continue

                        # ===== QUAN TRỌNG: Đợi 3s để iframe load hoàn thiện =====
                        log("ELDO", order_id, "      ⏳ Đợi iframe load hoàn thiện (3s)...")
                        driver.switch_to.frame(iframe)
                        await asyncio.sleep(3)

                        # Tìm input - dùng JavaScript để tương tác với hidden input
                        inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
                        if not inputs:
                            inputs = driver.find_elements(By.CSS_SELECTOR, "input.test__fileupload-input")

                        if len(inputs) > 0:
                            abs_path = os.path.abspath(file_path).replace("\\", "/")
                            input_el = inputs[0]

                            # ===== QUAN TRỌNG: Xóa file cũ trước khi upload lại (trừ lần đầu) =====
                            if page_retry > 0:
                                log("ELDO", order_id, "      🗑️ Xóa file cũ trước khi upload lại...")
                                try:
                                    # Reset input value
                                    driver.execute_script("""
                                        arguments[0].value = '';
                                        arguments[0].files = new DataTransfer().files;
                                    """, input_el)
                                    await asyncio.sleep(0.5)

                                    # Thử click nút cancel/xóa nếu có
                                    cancel_btns = driver.find_elements(By.CSS_SELECTOR,
                                        "button[class*='cancel'], button[class*='remove'], button[class*='delete'], .file-remove")
                                    for btn in cancel_btns:
                                        try:
                                            if btn.is_displayed():
                                                driver.execute_script("arguments[0].click();", btn)
                                                await asyncio.sleep(0.3)
                                        except:
                                            pass
                                except Exception as e:
                                    log("ELDO", order_id, f"      ⚠️ Lỗi xóa file cũ: {e}")

                            # Hiện input để send_keys
                            driver.execute_script("arguments[0].style.display = 'block'; arguments[0].style.visibility = 'visible';", input_el)
                            await asyncio.sleep(0.3)

                            # Send keys - upload file mới
                            input_el.send_keys(abs_path)
                            log("ELDO", order_id, "      📤 Đã chọn file, chờ upload...")

                            # Đợi file upload xong (quan trọng với file lớn)
                            await asyncio.sleep(3)

                            clicked_send = False
                            log("ELDO", order_id, "      Waiting for Send button...")

                            for _ in range(80):  # Tăng timeout
                                # Thử nhiều selector cho nút Send
                                btns = driver.find_elements(By.CSS_SELECTOR, ".confirm-send.test__confirm-upload-button")
                                if not btns:
                                    btns = driver.find_elements(By.CSS_SELECTOR, ".confirm-send")
                                if not btns:
                                    btns = driver.find_elements(By.CSS_SELECTOR, "button[class*='confirm']")

                                if len(btns) > 0:
                                    try:
                                        if btns[0].is_displayed() and btns[0].is_enabled():
                                            driver.execute_script("arguments[0].click();", btns[0])
                                            clicked_send = True
                                            log("ELDO", order_id, "      -> Clicked Send!")
                                            break
                                    except:
                                        pass
                                await asyncio.sleep(0.3)

                            if clicked_send:
                                log("ELDO", order_id, "      ✅ OK! Chờ 2s...")
                                await asyncio.sleep(2)
                                file_sent_ok = True
                                sent_registry.append(file_name)
                                break  # Thoát vòng page_retry
                            else:
                                log("ELDO", order_id, "      ⚠️ Timeout: Không hiện nút Send -> Sẽ xóa file cũ và thử lại...")
                        else:
                            log("ELDO", order_id, "      ⚠️ Lỗi: Không thấy Input trong iframe.")
                            # Debug: list tất cả inputs
                            all_inputs = driver.find_elements(By.CSS_SELECTOR, "input")
                            log("ELDO", order_id, f"      🔍 Tìm thấy {len(all_inputs)} input elements trong iframe")

                    except Exception as e:
                        log("ELDO", order_id, f"      ⚠️ Lỗi page_retry {page_retry}: {e}")

                    # Nếu thành công thì thoát vòng page_retry
                    if file_sent_ok:
                        break

                if not file_sent_ok:
                    log("ELDO", order_id, f"   ❌ Thất bại với file: {file_name}")

        # 4. Kiểm tra điều kiện thoát Global Loop
        if len(sent_registry) == total_files:
            log("ELDO", order_id, f"🎉 Đã upload đủ tất cả {total_files} file!")
            break
        else:
            log("ELDO", order_id, f"⚠️ Mới được {len(sent_registry)}/{total_files} file.")

    # 5. GỬI TIN NHẮN (Dùng UI - đơn giản và chắc chắn hoạt động)
    msg_sent = False

    try:
        driver.switch_to.default_content()
        # Tìm iframe TalkJS - dùng selector giống như khi upload
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe[name*='talkjs']")
        if not frames:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe[src*='talkjs']")

        if len(frames) > 0:
            driver.switch_to.frame(frames[0])
            await asyncio.sleep(0.5)

            ed = None
            for _ in range(20):
                eds = driver.find_elements(By.CSS_SELECTOR, "div.test__entry-field")
                if len(eds) > 0:
                    ed = eds[0]
                    break
                await asyncio.sleep(0.2)

            if ed:
                msg = "Done"
                if os.path.exists("message.txt"):
                    with open("message.txt", "r", encoding="utf-8") as f:
                        msg = f.read().strip()

                # Nhập tin nhắn
                driver.execute_script("arguments[0].innerHTML = '<p>'+arguments[1]+'</p>'; arguments[0].dispatchEvent(new Event('input', {bubbles:true}));", ed, msg)
                await asyncio.sleep(0.5)

                # Tìm và click nút send
                send_btns = driver.find_elements(By.CSS_SELECTOR, "button.test__send-button")
                if len(send_btns) > 0:
                    driver.execute_script("arguments[0].click();", send_btns[0])
                    log("ELDO", order_id, "✅ Đã gửi tin nhắn qua UI!")
                    msg_sent = True
                await asyncio.sleep(1)
            else:
                log("ELDO", order_id, "⚠️ Không tìm thấy ô nhập tin nhắn")
        else:
            log("ELDO", order_id, "⚠️ Không tìm thấy iframe để gửi tin nhắn")

        driver.switch_to.default_content()
    except Exception as e:
        log("ELDO", order_id, f"⚠️ Lỗi gửi tin nhắn: {e}")
        try:
            driver.switch_to.default_content()
        except:
            pass

    driver.implicitly_wait(10)
    log("ELDO", order_id, "🏁 Hoàn thành!")
    return len(sent_registry)


# ==========================================
# HELPER: EXTRACT CONVERSATION ID
# ==========================================
async def extract_conversation_id(driver, order_id: str = "") -> str:
    """
    Extract TalkJS conversation ID từ trang Eldorado

    Returns:
        str: Conversation ID hoặc None
    """
    import json as json_module

    try:
        driver.switch_to.default_content()

        # Method 1: Tìm trong iframe src
        iframes = driver.find_elements(By.CSS_SELECTOR, "iframe[name*='talkjs']")
        if iframes:
            src = iframes[0].get_attribute("src")

            # Parse syncPlease param (base64 encoded)
            import base64
            sync_match = re.search(r'syncPlease=([^&]+)', src)
            if sync_match:
                try:
                    encoded = sync_match.group(1)
                    # URL decode rồi base64 decode
                    from urllib.parse import unquote
                    encoded = unquote(encoded)
                    # Add padding if needed
                    encoded += '=' * (4 - len(encoded) % 4)
                    decoded = base64.b64decode(encoded)
                    data = json_module.loads(decoded)
                    conv_id = data.get('externalConversationId')
                    if conv_id:
                        log("ELDO", order_id, f"📋 Conversation ID (iframe): {conv_id}")
                        return conv_id
                except Exception as e:
                    log("ELDO", order_id, f"⚠️ Lỗi parse syncPlease: {e}")

        # Method 2: Tìm trong page source / JavaScript
        page_source = driver.page_source

        # Pattern 1: UUID format
        uuid_pattern = r'"conversationId"\s*:\s*"([a-f0-9\-]{36})"'
        match = re.search(uuid_pattern, page_source)
        if match:
            log("ELDO", order_id, f"📋 Conversation ID (page): {match.group(1)}")
            return match.group(1)

        # Pattern 2: Short ID format
        short_pattern = r'"conversationId"\s*:\s*"([a-f0-9]{20})"'
        match = re.search(short_pattern, page_source)
        if match:
            log("ELDO", order_id, f"📋 Conversation ID (short): {match.group(1)}")
            return match.group(1)

        # Method 3: Execute JavaScript để lấy từ TalkJS global
        try:
            conv_id = driver.execute_script("""
                // Try to get from TalkJS popup
                if (window.talkJsPopup) {
                    return window.talkJsPopup.getConversationId();
                }
                return null;
            """)
            if conv_id:
                log("ELDO", order_id, f"📋 Conversation ID (JS): {conv_id}")
                return conv_id
        except:
            pass

        log("ELDO", order_id, "⚠️ Không tìm thấy Conversation ID")
        return None

    except Exception as e:
        log("ELDO", order_id, f"❌ Lỗi extract conversation ID: {e}")
        return None