# order_scanner.py - Module quét đơn hàng tự động (thay thế Chrome Extension)
# Tích hợp với Bot Discord hiện tại

import asyncio
import re
import time
import json
import os
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Thread pool cho Selenium operations (tránh block Discord event loop)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="selenium_")

def shutdown_executor():
    """Dọn dẹp thread pool khi bot dừng"""
    _executor.shutdown(wait=True)
    print("✅ Đã shutdown Selenium thread pool")

# ==========================================
# CẤU HÌNH MẶC ĐỊNH
# ==========================================
DEFAULT_CONFIG = {
    "whitelist": "Divine Orb, Mirror of Kalandra, Runes, Gold, Boss Materials, Currency",
    "blacklist": "",
    "status_list": ["Preparing", "Pending", "To be delivered", "Wait for delivery"],
    "platforms": {
        "g2g": True,
        "eldorado": True,
        "diablo": True
    },
    "scan_interval_min": 15,  # Giây
    "scan_interval_max": 21,  # Giây
    "webhooks": {
        "default": "",
        "mappings": []
    }
}

# URL mặc định cho từng platform
URL_DEFAULTS = {
    "g2g": "https://www.g2g.com/g2g-user/sale?status=preparing",
    "eldorado": "https://www.eldorado.gg/dashboard/orders/sold?orderState=PendingDelivery&displayFilter=DisplaySellingOrders"
}

# File cache cho processed orders
CACHE_DIR = "cache"
CACHE_MAX_AGE_HOURS = 3  # Xóa đơn cũ hơn 3 giờ (như extension)

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def normalize_id(id_str: str) -> Optional[str]:
    """Chuẩn hóa Order ID"""
    if not id_str:
        return None
    clean_id = str(id_str).replace('#', '').strip().upper()
    if clean_id.endswith('-1'):
        clean_id = clean_id[:-2]
    return clean_id

def check_keywords(text: str, config: dict) -> bool:
    """Kiểm tra whitelist/blacklist"""
    if not config:
        return True
    lower_text = (text or "").lower()

    # Kiểm tra blacklist
    if config.get("blacklist"):
        blacklist = [k.strip().lower() for k in config["blacklist"].split(',') if k.strip()]
        if any(k in lower_text for k in blacklist):
            return False

    # Kiểm tra whitelist
    if config.get("whitelist"):
        whitelist = [k.strip().lower() for k in config["whitelist"].split(',') if k.strip()]
        if whitelist and not any(k in lower_text for k in whitelist):
            return False

    return True

def log(platform: str, message: str):
    """Log với format chuẩn"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}][{platform}] {message}")

# ==========================================
# ORDER SCANNER CLASS
# ==========================================
class OrderScanner:
    def __init__(self, driver, platform: str, config: dict = None):
        """
        Khởi tạo Scanner

        Args:
            driver: Selenium WebDriver instance
            platform: 'g2g' hoặc 'eldorado'
            config: Cấu hình scanner
        """
        self.driver = driver
        self.platform = platform.lower()
        self.platform_display = platform.upper()
        self.config = config or DEFAULT_CONFIG
        self.processed_orders: Dict[str, float] = {}  # {order_id: timestamp}
        self.is_running = False
        self.scan_callback = None  # Callback khi tìm thấy đơn hàng mới (gửi webhook)
        self.processing_orders: set = set()  # Đơn đang được xử lý (tránh race condition)

        # Tạo thư mục cache nếu chưa có
        if not os.path.exists(CACHE_DIR):
            os.makedirs(CACHE_DIR)

        # File cache cho platform này
        self.cache_file = os.path.join(CACHE_DIR, f"processed_{self.platform}.json")

        # Load cache từ file
        self._load_cache()

    # ==========================================
    # ASYNC HELPERS - Chạy Selenium không block event loop
    # ==========================================
    async def _run_sync(self, func, *args, timeout: float = 30, **kwargs):
        """Chạy function synchronous trong thread pool để không block event loop"""
        try:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(_executor, partial(func, *args, **kwargs)),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            log(self.platform_display, f"⚠️ Timeout sau {timeout}s")
            return None
        except Exception as e:
            log(self.platform_display, f"⚠️ Lỗi _run_sync: {e}")
            return None

    async def _driver_get(self, url: str, timeout: float = 30):
        """Navigate to URL (non-blocking)"""
        await self._run_sync(self.driver.get, url, timeout=timeout)

    async def _driver_refresh(self, timeout: float = 30):
        """Refresh page (non-blocking)"""
        await self._run_sync(self.driver.refresh, timeout=timeout)

    async def _get_current_url(self, timeout: float = 5) -> str:
        """Get current URL (non-blocking)"""
        result = await self._run_sync(lambda: self.driver.current_url, timeout=timeout)
        return result or ""

    async def _get_page_source(self, timeout: float = 10) -> str:
        """Get page source (non-blocking)"""
        result = await self._run_sync(lambda: self.driver.page_source, timeout=timeout)
        return result or ""

    async def _find_elements(self, by: By, value: str, timeout: float = 10):
        """Find elements (non-blocking)"""
        return await self._run_sync(
            lambda: self.driver.find_elements(by, value),
            timeout=timeout
        ) or []

    def _load_cache(self):
        """Load processed orders từ file cache"""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Convert string keys back và filter old entries
                    now = time.time()
                    max_age = CACHE_MAX_AGE_HOURS * 3600
                    for order_id, timestamp in data.items():
                        if now - timestamp < max_age:
                            self.processed_orders[order_id] = timestamp
                    log(self.platform_display, f"📂 Loaded {len(self.processed_orders)} cached orders")
        except Exception as e:
            log(self.platform_display, f"⚠️ Load cache error: {e}")
            self.processed_orders = {}

    def _save_cache(self):
        """Lưu processed orders vào file cache"""
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.processed_orders, f)
        except Exception as e:
            log(self.platform_display, f"⚠️ Save cache error: {e}")

    def _cleanup_old_orders(self):
        """Xóa đơn hàng cũ khỏi cache (>3 giờ)"""
        now = time.time()
        max_age = CACHE_MAX_AGE_HOURS * 3600
        old_count = len(self.processed_orders)

        self.processed_orders = {
            k: v for k, v in self.processed_orders.items()
            if now - v < max_age
        }

        new_count = len(self.processed_orders)
        if old_count > new_count:
            log(self.platform_display, f"🧹 Cleaned {old_count - new_count} old orders (>3h)")
            self._save_cache()

    def _mark_processed(self, order_id: str):
        """Đánh dấu đơn hàng đã xử lý và lưu cache"""
        self.processed_orders[order_id] = time.time()
        self._save_cache()

    def _is_processed(self, order_id: str) -> bool:
        """Kiểm tra đơn hàng đã xử lý chưa"""
        return order_id in self.processed_orders or order_id in self.processing_orders

    def set_callbacks(self, scan_callback=None):
        """Set callback khi tìm thấy đơn hàng mới"""
        self.scan_callback = scan_callback

    async def start(self):
        """Bắt đầu quét - chạy liên tục"""
        self.is_running = True
        log(self.platform_display, "🚀 Scanner đã bắt đầu")

        while self.is_running:
            try:
                await self.scan_loop()
            except Exception as e:
                log(self.platform_display, f"❌ Lỗi scan loop: {e}")
                await asyncio.sleep(5)

    async def run_once(self):
        """Chỉ quét 1 lần (để test)"""
        return await self.scan_order_list()

    def stop(self):
        """Dừng quét"""
        self.is_running = False
        log(self.platform_display, "⏹ Scanner đã dừng")

    async def _is_on_correct_page(self) -> bool:
        """Kiểm tra có đang ở đúng trang danh sách không (non-blocking)"""
        try:
            current_url = await self._get_current_url()
            current_url = current_url.lower()

            if self.platform.lower() == "g2g":
                # G2G: phải có status=preparing trong URL
                return "status=preparing" in current_url

            elif self.platform.lower() == "eldorado":
                # Eldorado: phải có orderState=PendingDelivery trong URL
                return "orderstate=pendingdelivery" in current_url

            return False
        except:
            return False

    async def _is_error_page(self) -> bool:
        """Kiểm tra trang có bị lỗi không (404, 500, connection error...) - non-blocking

        Lưu ý: "No results found" KHÔNG phải là lỗi - đây là trang bình thường
        """
        try:
            page_source = await self._get_page_source()
            page_source = page_source.lower()
            current_url = await self._get_current_url()
            current_url = current_url.lower()

            # Các dấu hiệu lỗi thực sự (KHÔNG bao gồm "no results")
            error_indicators = [
                "something went wrong",
                "error page",
                "page not found",
                "server error",
                "/error/",
                "this site can't be reached",
                "connection refused",
                "err_connection",
                "err_internet",
                "404 -",  # Tránh match 404 trong order ID
                "500 -",
            ]

            for indicator in error_indicators:
                if indicator in page_source or indicator in current_url:
                    log(self.platform_display, f"⚠️ Phát hiện lỗi: '{indicator}'")
                    return True

            # Kiểm tra mascot image (G2G error page) - chỉ khi URL không đúng
            if "g2g.com" in current_url and "status=preparing" not in current_url:
                mascot = await self._find_elements(By.CSS_SELECTOR, 'img[src*="mascot"]')
                if mascot:
                    log(self.platform_display, "⚠️ G2G error page (mascot)")
                    return True

            return False
        except Exception as e:
            log(self.platform_display, f"⚠️ Lỗi check error page: {e}")
            return False  # Không coi là lỗi nếu không check được

    async def _ensure_correct_page(self, need_refresh: bool = True):
        """Đảm bảo đang ở đúng trang danh sách (non-blocking)

        Args:
            need_refresh: Có cần F5 để lấy đơn mới không (False = chỉ check trang đúng)
        """
        target_url = URL_DEFAULTS.get(self.platform.lower())
        if not target_url:
            return False

        try:
            current_url = await self._get_current_url()

            # Kiểm tra trang lỗi
            if await self._is_error_page():
                log(self.platform_display, "⚠️ Phát hiện trang lỗi, điều hướng lại...")
                await self._driver_get(target_url)
                await asyncio.sleep(3)
                return True

            # Kiểm tra đúng trang
            if await self._is_on_correct_page():
                if need_refresh:
                    # Đang đúng trang → F5 để lấy đơn mới
                    log(self.platform_display, "🔄 Refresh trang...")
                    await self._driver_refresh()
                    await asyncio.sleep(3)
                return True
            else:
                # Không đúng trang → Điều hướng
                log(self.platform_display, f"🔄 Điều hướng đến trang danh sách...")
                log(self.platform_display, f"   Hiện tại: {current_url[:80]}...")
                await self._driver_get(target_url)
                await asyncio.sleep(3)
                return True

        except Exception as e:
            log(self.platform_display, f"❌ Lỗi điều hướng: {e}")
            # Thử lại
            try:
                await self._driver_get(target_url)
                await asyncio.sleep(3)
                return True
            except:
                return False

    async def scan_loop(self):
        """Vòng lặp quét chính"""
        if not self.is_running:
            return

        # Cleanup đơn cũ định kỳ
        self._cleanup_old_orders()

        # Kiểm tra đang ở đúng trang (không F5, chỉ check)
        if not await self._ensure_correct_page(need_refresh=False):
            log(self.platform_display, "❌ Không thể điều hướng đến trang đúng!")
            await asyncio.sleep(5)
            return

        # F5 để lấy đơn mới (non-blocking)
        try:
            log(self.platform_display, "🔄 Refresh trang để lấy đơn mới...")
            await self._driver_refresh()
            await asyncio.sleep(3)
        except Exception as e:
            log(self.platform_display, f"⚠️ Lỗi refresh: {e}")

        # Quét đơn hàng
        orders = await self.scan_order_list()

        if orders:
            # Lọc các đơn chưa xử lý
            new_orders = [o for o in orders if not self._is_processed(o['id'])]

            if new_orders:
                log(self.platform_display, f"📋 Tìm thấy {len(new_orders)} đơn hàng mới")

                for order in new_orders:
                    await self.process_order(order)
            else:
                log(self.platform_display, f"📭 {len(orders)} đơn đã xử lý trước đó")
        else:
            # Không có đơn → log ngắn gọn hơn
            pass  # Không log để tránh spam

        # Chờ random trước khi quét lại
        if self.is_running:
            interval_min = self.config.get("scan_interval_min", 15)
            interval_max = self.config.get("scan_interval_max", 25)
            wait_time = random.randint(interval_min, interval_max)
            await asyncio.sleep(wait_time)

    async def scan_order_list(self) -> List[Dict]:
        """Quét danh sách đơn hàng và trả về danh sách đơn mới"""
        orders = []

        try:
            if self.platform.lower() == "g2g":
                orders = await self._scan_g2g_list()
            elif self.platform.lower() == "eldorado":
                orders = await self._scan_eldorado_list()
        except Exception as e:
            log(self.platform, f"❌ Lỗi quét: {e}")

        return orders

    async def _scan_g2g_list(self) -> List[Dict]:
        """Quét danh sách G2G (non-blocking)"""
        def _sync_scan():
            orders = []
            try:
                rows = self.driver.find_elements(By.CSS_SELECTOR, "a.g-card-no-deco")

                for row in rows:
                    try:
                        link = row.get_attribute('href')
                        if not link:
                            continue

                        id_match = re.search(r'order/item/([A-Z0-9-]+)', link)
                        if not id_match:
                            continue

                        order_id = normalize_id(id_match.group(1))
                        if not order_id or self._is_processed(order_id):
                            continue

                        title_el = row.find_elements(By.CSS_SELECTOR, 'span[data-attr="order-item-offer-title"]')
                        title = title_el[0].text if title_el else ""

                        qty_el = row.find_elements(By.CSS_SELECTOR, '[data-attr="order-item-purchased-qty"]')
                        qty_text = qty_el[0].text if qty_el else ""

                        full_text = f"{title} {qty_text}"
                        if not check_keywords(full_text, self.config):
                            self._mark_processed(order_id)
                            continue

                        orders.append({
                            'id': order_id,
                            'url': link,
                            'title': title,
                            'quantity': qty_text
                        })
                    except:
                        continue
            except Exception as e:
                log(self.platform_display, f"❌ Lỗi quét G2G: {e}")
            return orders

        return await self._run_sync(_sync_scan, timeout=30) or []

    async def _scan_eldorado_list(self) -> List[Dict]:
        """Quét danh sách Eldorado (non-blocking)"""
        def _sync_scan():
            orders = []
            try:
                rows = self.driver.find_elements(By.CSS_SELECTOR, 'a[href*="/order/"]')

                for row in rows:
                    try:
                        # Chỉ lấy đơn hàng Pending
                        row_text = row.text.lower()
                        if 'pendingdelivery' not in row_text and 'pending' not in row_text:
                            continue

                        link = row.get_attribute('href')
                        if not link:
                            continue

                        # Extract Order ID từ URL
                        id_match = re.search(r'order/([a-zA-Z0-9-]+)', link)
                        if not id_match:
                            continue

                        order_id = normalize_id(id_match.group(1))
                        if not order_id or self._is_processed(order_id):
                            continue

                        # Kiểm tra whitelist/blacklist (config có whitelist/blacklist ở top-level)
                        if not check_keywords(row.text, self.config):
                            log(self.platform_display, f"⛔ Bỏ qua (filter): {order_id}")
                            self._mark_processed(order_id)  # Đánh dấu đã xử lý để không quét lại
                            continue

                        orders.append({
                            'id': order_id,
                            'url': link
                        })

                    except Exception as e:
                        continue

            except Exception as e:
                log(self.platform_display, f"❌ Lỗi quét Eldorado: {e}")

            return orders

        return await self._run_sync(_sync_scan, timeout=30) or []

    async def process_order(self, order: Dict):
        """Xử lý đơn hàng mới"""
        order_id = order['id']
        order_url = order['url']

        log(self.platform_display, f"🎯 Xử lý đơn hàng: {order_id}")

        # Thêm vào processing set NGAY để tránh race condition
        self.processing_orders.add(order_id)

        try:
            # Mở trang chi tiết và extract data
            order_data = await self.extract_order_data(order_url)

            if order_data:
                # G2G title mapping: ghi đè itemName nếu title khớp pattern
                if self.platform.lower() == "g2g":
                    title = order.get('title', '')
                    if title:
                        for mapping in self.config.get('G2G_TITLE_MAP', []):
                            if mapping['title_pattern'].lower() in title.lower():
                                log(self.platform_display, f"🔄 Title map: {order_data.get('itemName')} → {mapping['display_name']}")
                                order_data['itemName'] = mapping['display_name']
                                break

                # CHỈ dùng scan_callback để gửi webhook (tránh duplicate)
                # scan_callback sẽ gọi send_order_webhook trong GegeOrder.py
                if self.scan_callback:
                    await self.scan_callback(order_data)
                    log(self.platform_display, f"✅ Đã xử lý đơn: {order_id}")

            # Đánh dấu đã xử lý sau khi hoàn thành
            self._mark_processed(order_id)

        except Exception as e:
            log(self.platform_display, f"❌ Lỗi xử lý đơn {order_id}: {e}")
        finally:
            # Luôn xóa khỏi processing set
            self.processing_orders.discard(order_id)

    async def extract_order_data(self, url: str) -> Optional[Dict]:
        """Mở trang chi tiết và extract dữ liệu

        Flow theo extension:
        - G2G: Click "View details" → Click "Start deliver" → Đợi data → Extract
        - Eldorado: Đợi data → Extract
        """
        try:
            # Lưu URL hiện tại để quay lại (non-blocking)
            return_url = await self._get_current_url()

            # Điều hướng đến trang chi tiết (non-blocking)
            await self._driver_get(url)
            await asyncio.sleep(2)

            # Extract data dựa trên platform
            if self.platform.lower() == "g2g":
                data = await self._extract_g2g_detail_with_clicks()
            elif self.platform.lower() == "eldorado":
                data = await self._extract_eldorado_detail_with_wait()
            else:
                data = None

            # Quay lại trang danh sách (non-blocking)
            await self._driver_get(return_url)
            await asyncio.sleep(1)

            return data

        except Exception as e:
            log(self.platform, f"❌ Lỗi extract data: {e}")
            return None

    async def _extract_g2g_detail_with_clicks(self) -> Optional[Dict]:
        """Extract dữ liệu G2G với các bước click (theo extension) - non-blocking

        Flow:
        1. Click "View details" nếu chưa thấy brand/game
        2. Click "Start deliver" nếu chưa thấy delivery info
        3. Đợi có đủ: game, server, character
        4. Extract data
        """
        def _sync_extract():
            data = {
                'platform': 'G2G',
                'orderId': None,
                'customerName': None,
                'game': None,
                'server': None,
                'itemName': None,
                'quantity': None,
                'character': None,
                'url': self.driver.current_url
            }

            max_attempts = 40  # 40 * 0.5s = 20s max

            for attempt in range(max_attempts):
                try:
                    # Bước 1: Click "View details" nếu chưa thấy brand
                    brand_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr="order-item-brand"]')
                    if not brand_el or not brand_el[0].text.strip():
                        # Tìm và click "View details"
                        detail_btns = self.driver.find_elements(By.XPATH, "//span[contains(text(), 'View details')]")
                        if detail_btns:
                            try:
                                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", detail_btns[0])
                                detail_btns[0].click()
                                log(self.platform_display, f"👆 Clicked 'View details'")
                                time.sleep(1)
                            except:
                                pass

                    # Bước 2: Click "Start deliver" nếu chưa thấy delivery info
                    char_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr^="order-item-delivery-info"]')
                    if not char_el or not char_el[0].text.strip():
                        start_btns = self.driver.find_elements(By.XPATH, "//button[contains(., 'Start deliver')]")
                        if start_btns:
                            try:
                                start_btns[0].click()
                                log(self.platform_display, f"👆 Clicked 'Start deliver'")
                                time.sleep(1)
                            except:
                                pass

                    # Bước 3: Extract data hiện tại
                    data = self._extract_g2g_data_from_page()

                    # Kiểm tra đã có đủ data chưa
                    has_game = data.get('game') and data['game'] not in ['N/A', '']
                    has_server = data.get('server') and data['server'] not in ['N/A', '']
                    has_char = data.get('character') and data['character'] not in ['N/A', '', data.get('customerName', '')]

                    if has_game and has_server and has_char:
                        log(self.platform_display, f"✅ G2G Data Ready (attempt {attempt + 1})")
                        return data

                    # Chờ trước khi thử lại
                    time.sleep(0.5)

                except Exception as e:
                    log(self.platform_display, f"⚠️ G2G extract attempt {attempt + 1}: {e}")
                    time.sleep(0.5)

            # Timeout - trả về data đã có
            log(self.platform_display, f"⏰ G2G timeout sau {max_attempts} attempts, dùng data hiện có")
            return data if data.get('orderId') else None

        return await self._run_sync(_sync_extract, timeout=60)

    def _extract_g2g_data_from_page(self) -> Dict:
        """Extract dữ liệu từ trang G2G (không có logic click)"""
        data = {
            'platform': 'G2G',
            'orderId': None,
            'customerName': None,
            'game': None,
            'server': None,
            'itemName': None,
            'quantity': None,
            'character': None,
            'url': self.driver.current_url
        }

        try:
            # Order ID
            oid_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr="order-item-order-id"]')
            if oid_el:
                data['orderId'] = normalize_id(oid_el[0].text)

            if not data['orderId']:
                url_match = re.search(r'order/item/([A-Z0-9-]+)', self.driver.current_url)
                if url_match:
                    data['orderId'] = normalize_id(url_match.group(1))

            # Buyer Username
            buyer_el = self.driver.find_elements(By.CSS_SELECTOR, 'a[data-attr="order-item-buyer-username"]')
            if buyer_el:
                data['customerName'] = buyer_el[0].text.strip()

            # Brand/Game - theo extension: getValueByLabelG2G("Brand") || getValueByLabelG2G("Game")
            brand_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr="order-item-brand"]')
            if brand_el:
                data['game'] = brand_el[0].text.strip()

            # Fallback: tìm theo label giống extension
            if not data['game']:
                data['game'] = self._get_g2g_value_by_label("Brand") or self._get_g2g_value_by_label("Game")

            # Fallback: Lấy game từ breadcrumb
            if not data['game']:
                breadcrumb = self.driver.find_elements(By.CSS_SELECTOR, '.custom-breadcrumb')
                if breadcrumb and '>' in breadcrumb[0].text:
                    parts = breadcrumb[0].text.split('>')
                    if len(parts) >= 2:
                        data['game'] = parts[1].strip()

            # Server - theo extension: getValueByLabelG2G("Server") → fallback title
            data['server'] = self._get_g2g_value_by_label("Server")
            if not data['server']:
                data['server'] = self._get_g2g_value_by_label("Realm")
            if not data['server']:
                data['server'] = self._get_g2g_value_by_label("Region")
            # Fallback: từ offer title nếu có dấu "-" (giống extension)
            if not data['server']:
                title_el = self.driver.find_elements(By.CSS_SELECTOR, '[data-attr="order-item-offer-title"]')
                if title_el and '-' in title_el[0].text:
                    data['server'] = title_el[0].text.strip()

            # Quantity - theo extension: div → span → fallback label
            qty_el = self.driver.find_elements(By.CSS_SELECTOR, 'div[data-attr="order-item-purchased-qty"]')
            if not qty_el:
                qty_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr="order-item-purchased-qty"]')

            raw_qty = ""
            if qty_el:
                raw_qty = qty_el[0].text.strip()
            else:
                # Fallback label (giống extension)
                raw_qty = self._get_g2g_value_by_label("Purchase quantity") or \
                          self._get_g2g_value_by_label("Quantity") or ""

            if raw_qty:
                cleaned_qty = re.sub(r'[×x]', '', raw_qty, flags=re.IGNORECASE)
                cleaned_qty = re.sub(r'\b(Mil|Gold|Unit|Units)\b', '', cleaned_qty, flags=re.IGNORECASE)
                data['quantity'] = re.sub(r'\s+', ' ', cleaned_qty).strip()

            # Item Name - Logic theo extension (content.js dòng 292-321)
            # Bước 1: Tìm "Item Type" (ví dụ: "Runes", "Currency", ...)
            specific_item = None
            item_type = self._get_g2g_value_by_label("Item Type")
            if item_type:
                # Bước 2: Dùng tên Item Type làm label để tìm chi tiết
                # Ví dụ: getValueByLabel("Runes") → "Bac"
                detail_value = self._get_g2g_value_by_label(item_type)
                if detail_value:
                    specific_item = f"{item_type} - {detail_value}"
                else:
                    specific_item = item_type

            # Fallback: thử các label khác
            if not specific_item:
                specific_item = self._get_g2g_value_by_label("Item") or \
                                self._get_g2g_value_by_label("Product") or \
                                self._get_g2g_value_by_label("Currency")

            # Fallback: kiểm tra service type
            if not specific_item:
                service_el = self.driver.find_elements(By.CSS_SELECTOR, 'div[data-attr="order-item-service-type"]')
                if service_el and 'coin' in service_el[0].text.lower():
                    specific_item = "Gold"

            # Fallback: Lấy item từ title link
            if not specific_item:
                title_link = self.driver.find_elements(By.CSS_SELECTOR, 'a[data-attr="order-item-offer-title"]')
                if not title_link:
                    title_link = self.driver.find_elements(By.CSS_SELECTOR, '[data-attr="order-item-offer-title"]')
                if title_link:
                    title_text = title_link[0].text.strip()
                    if 'gold' in title_text.lower():
                        specific_item = "Gold"
                    else:
                        specific_item = title_text
                else:
                    specific_item = "Unknown Item"

            data['itemName'] = specific_item

            # Character / Delivery Info - theo extension getCharacterInfoG2G()
            # 1. Ưu tiên lấy theo attribute đặc biệt (chính xác nhất)
            char_el = self.driver.find_elements(By.CSS_SELECTOR, 'span[data-attr^="order-item-delivery-info"]')
            if char_el and char_el[0].text.strip():
                data['character'] = char_el[0].text.strip()

            # 2. Fallback: tìm theo nhiều label (giống extension)
            if not data['character']:
                char_labels = ["Character Name", "BattleTag", "Riot ID", "IGN", "Account", "User ID"]
                for lbl in char_labels:
                    val = self._get_g2g_value_by_label(lbl)
                    if val:
                        data['character'] = val
                        break

            # 3. Fallback cuối: dùng buyer name (giống extension)
            if not data['character'] and data.get('customerName'):
                data['character'] = data['customerName']

        except Exception as e:
            log(self.platform_display, f"❌ Lỗi extract G2G data: {e}")

        return data

    async def _extract_eldorado_detail_with_wait(self) -> Optional[Dict]:
        """Extract dữ liệu Eldorado với đợi game data (theo extension) - non-blocking

        Flow:
        1. Đợi có game data
        2. Extract data
        """
        def _sync_extract():
            data = {
                'platform': 'Eldorado',
                'orderId': None,
                'customerName': None,
                'game': None,
                'server': None,
                'itemName': None,
                'quantity': None,
                'character': None,
                'url': self.driver.current_url
            }

            max_attempts = 40  # 40 * 0.5s = 20s max

            for attempt in range(max_attempts):
                try:
                    data = self._extract_eldorado_data_from_page()

                    # Kiểm tra đã có đủ game và itemName chưa
                    has_game = data.get('game') and data['game'] not in ['N/A', '']
                    has_item = data.get('itemName') and data['itemName'] not in ['Unknown Item', '']

                    if has_game and has_item:
                        log(self.platform_display, f"✅ Eldorado Data Ready (attempt {attempt + 1})")
                        return data

                    time.sleep(0.5)

                except Exception as e:
                    log(self.platform_display, f"⚠️ Eldorado extract attempt {attempt + 1}: {e}")
                    time.sleep(0.5)

            # Timeout - trả về data đã có
            log(self.platform_display, f"⏰ Eldorado timeout sau {max_attempts} attempts, dùng data hiện có")
            return data if data.get('orderId') else None

        return await self._run_sync(_sync_extract, timeout=60)

    def _get_eldorado_value_by_label(self, label_text: str) -> Optional[str]:
        """Lấy giá trị theo label từ trang Eldorado - giống extension getValueByLabelEldorado()

        Extension logic (content.js dòng 136-146):
        1. XPath: //span[.text-secondary chứa labelText]/following-sibling::span[.text-primary]
        2. Backup: //span[.text-secondary chứa labelText]/../*[last()]
        """
        try:
            # Cách 1: text-secondary → sibling text-primary (giống extension XPath chính)
            xpath = f"//span[contains(@class, 'text-secondary') and contains(text(), '{label_text}')]/following-sibling::span[contains(@class, 'text-primary')]"
            els = self.driver.find_elements(By.XPATH, xpath)
            if els and els[0].text.strip():
                return els[0].text.strip()

            # Cách 2: Backup - lấy phần tử cuối cùng trong cùng parent (giống extension XPath backup)
            xpath_backup = f"//span[contains(@class, 'text-secondary') and contains(text(), '{label_text}')]/../*[last()]"
            els_backup = self.driver.find_elements(By.XPATH, xpath_backup)
            if els_backup and els_backup[0].text.strip():
                return els_backup[0].text.strip()

            # Cách 3: Fallback CSS - dùng row scan (giữ lại logic cũ làm dự phòng)
            rows = self.driver.find_elements(By.CSS_SELECTOR, '.order-details-card .flex.items-center.justify-between')
            for row in rows:
                label_el = row.find_elements(By.CSS_SELECTOR, '.text-secondary')
                if label_el and label_text.lower() in label_el[0].text.strip().lower():
                    value_el = row.find_elements(By.CSS_SELECTOR, '.text-primary, eld-trade-env-item')
                    if value_el and value_el[0].text.strip():
                        return value_el[0].text.strip()
        except:
            pass
        return None

    def _extract_eldorado_data_from_page(self) -> Dict:
        """Extract dữ liệu từ trang Eldorado - theo extension content.js dòng 211-237"""
        data = {
            'platform': 'Eldorado',
            'orderId': None,
            'customerName': None,
            'game': None,
            'server': None,
            'itemName': None,
            'quantity': None,
            'character': None,
            'url': self.driver.current_url
        }

        try:
            # 1. Order ID từ URL (giống extension)
            url_match = re.search(r'order/([a-zA-Z0-9-]+)', self.driver.current_url)
            if url_match:
                data['orderId'] = normalize_id(url_match.group(1))

            # Fallback: từ element
            if not data['orderId']:
                oid_el = self.driver.find_elements(By.CSS_SELECTOR, '.order-info .order-id')
                if oid_el:
                    match = re.search(r'Order ID:\s*([a-zA-Z0-9-]+)', oid_el[0].text)
                    if match:
                        data['orderId'] = normalize_id(match.group(1))

            # 2. Item Name - theo extension: h1, .offer-title, [class*="offerTitle"]
            for selector in ['h1', '.offer-title', '[class*="offerTitle"]']:
                title_el = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if title_el and title_el[0].text.strip():
                    data['itemName'] = title_el[0].text.strip()
                    break

            if not data['itemName']:
                data['itemName'] = "Unknown Item"

            # 3. Dùng label-based extraction (giống extension getValueByLabelEldorado)
            data['game'] = self._get_eldorado_value_by_label("Game") or "N/A"
            data['server'] = self._get_eldorado_value_by_label("Server") or \
                             self._get_eldorado_value_by_label("Realm") or "N/A"
            data['quantity'] = self._get_eldorado_value_by_label("Quantity") or "1"
            data['customerName'] = self._get_eldorado_value_by_label("Buyer") or \
                                   self._get_eldorado_value_by_label("Customer") or "Unknown"

            # Fallback game: từ image alt hoặc breadcrumb
            if not data['game'] or data['game'] == 'N/A':
                game_el = self.driver.find_elements(By.CSS_SELECTOR, '.order-details-card .game-image img')
                if game_el:
                    alt = game_el[0].get_attribute('alt')
                    if alt:
                        data['game'] = alt

            if not data['game'] or data['game'] == 'N/A':
                breadcrumb = self.driver.find_elements(By.CSS_SELECTOR, 'eld-breadcrumbs')
                if breadcrumb:
                    parts = breadcrumb[0].text.split('>')
                    if len(parts) >= 2:
                        data['game'] = parts[1].strip()

            # 4. Character - theo extension (content.js dòng 226-236)
            # Tìm Username/Character name/Account Name + Battle.net Tag/Discord Tag
            char_name = self._get_eldorado_value_by_label("Username") or \
                        self._get_eldorado_value_by_label("Character name") or \
                        self._get_eldorado_value_by_label("Account Name")
            btag = self._get_eldorado_value_by_label("Battle.net Tag") or \
                   self._get_eldorado_value_by_label("Discord Tag")

            # Format giống extension: "CharName (BattleTag)" thay vì xuống dòng
            if char_name and btag:
                data['character'] = f"{char_name} ({btag})"
            else:
                data['character'] = char_name or btag or "Check Order"

        except Exception as e:
            log(self.platform, f"❌ Lỗi extract Eldorado data: {e}")

        return data

    def _get_g2g_value_by_label(self, label: str) -> Optional[str]:
        """Lấy giá trị theo label từ trang G2G"""
        try:
            xpath = f"//div[contains(@class, 'text-font-2nd') and contains(text(), '{label}')]"
            label_el = self.driver.find_elements(By.XPATH, xpath)
            if label_el:
                row = label_el[0].find_element(By.XPATH, './ancestor::div[contains(@class, "row")]')
                if row:
                    value_el = row.find_elements(By.CSS_SELECTOR, '.text-right span, .text-right')
                    if value_el:
                        return value_el[0].text.strip()
        except:
            pass
        return None

    def get_processed_count(self) -> int:
        """Lấy số lượng đơn đã xử lý"""
        return len(self.processed_orders)

    def clear_cache(self):
        """Xóa toàn bộ cache"""
        self.processed_orders = {}
        self._save_cache()
        log(self.platform_display, "🧹 Đã xóa toàn bộ cache")


# ==========================================
# WEBHOOK SENDER
# ==========================================
async def send_discord_webhook(webhook_url: str, content: str, order_data: dict = None):
    """Gửi tin nhắn đến Discord Webhook"""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            payload = {"content": content}

            async with session.post(webhook_url, json=payload) as resp:
                if resp.status in [200, 204]:
                    return True
                else:
                    print(f"❌ Webhook error: {resp.status}")
                    return False
    except Exception as e:
        print(f"❌ Lỗi gửi webhook: {e}")
        return False


def format_order_message(order_data: dict, show_labels: bool = False) -> str:
    """Format tin nhắn đơn hàng để gửi Discord

    Format theo extension:
    - Order ID (hyperlinked với <> để không hiện preview)
    - Platform | CustomerName (⚡ cho Eldorado)
    - ItemName | Quantity (không emoji)
    - Character (không emoji)
    """
    lines = []

    # Platform + Customer (đưa lên đầu, bold để nổi bật)
    meta = []
    if order_data.get('platform'):
        platform = order_data['platform']
        if platform.lower() == 'eldorado':
            platform = f"⚡ {platform}"
        meta.append(platform)
    if order_data.get('customerName'):
        meta.append(order_data['customerName'])
    if meta:
        lines.append(f"**{' | '.join(meta)}**")

    # Order ID với link
    if order_data.get('orderId'):
        url = order_data.get('url', '')
        if url:
            lines.append(f"[{order_data['orderId']}](<{url}>)")
        else:
            lines.append(f"{order_data['orderId']}")

    # Item + Quantity (không emoji, không label)
    item_info = []
    if order_data.get('itemName'):
        item_info.append(order_data['itemName'])
    if order_data.get('quantity'):
        item_info.append(order_data['quantity'])
    if item_info:
        lines.append(" | ".join(item_info))

    # Character (không emoji)
    if order_data.get('character'):
        lines.append(order_data['character'])

    return "\n".join(lines)


# ==========================================
# TEST FUNCTION
# ==========================================
async def test_scanner():
    """Test scanner với driver"""
    from driver_manager import get_driver

    print("🧪 Đang test Order Scanner...")

    # Khởi tạo driver
    driver = get_driver("chrome_profile")

    # Khởi tạo scanner
    config = {
        "whitelist": "Divine, Gold, Currency",
        "blacklist": "Boosting, Leveling"
    }

    scanner = OrderScanner(driver, "eldorado", config)

    # Set callback
    async def on_order_found(order_data):
        print(f"🎉 Tìm thấy đơn hàng: {order_data}")

    scanner.set_callbacks(scan_callback=on_order_found)

    # Chạy 1 vòng
    await scanner.scan_loop()

    input("Nhấn Enter để thoát...")
    driver.quit()


if __name__ == "__main__":
    asyncio.run(test_scanner())
