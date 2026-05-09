# BotPasteDon (GeGe Order Auto)

Bot Discord tự động hóa quy trình quét đơn và giao hàng trên **Eldorado.gg** và **G2G.com**. Xây dựng bằng Python + Selenium, kiến trúc 4 Chrome Driver (2 Scanner + 2 Worker) chạy song song.

## Kiến trúc

```
┌──────────────────────────────────────────────────────┐
│                    GegeOrder.py (Main)               │
│                  Discord Bot + Orchestrator          │
├──────────────┬──────────────┬────────────────────────┤
│  Scanner     │  Scanner     │  Discord Channels      │
│  Eldorado    │  G2G         │  (per-game webhooks)   │
│  (Driver 1)  │  (Driver 2)  │                        │
├──────────────┴──────────────┼────────────────────────┤
│       order_scanner.py      │   order_queue.py       │
│  Quét đơn → Webhook →       │  Worker Eldorado (D3)  │
│  Discord Thread + Buttons   │  Worker G2G (Driver 4) │
│                             │  TalkJS WebSocket/REST │
└─────────────────────────────┴────────────────────────┘
```

**Luồng hoạt động:**

1. **Scanner** quét trang pending orders mỗi 15-25s, lọc whitelist/blacklist, extract chi tiết đơn hàng
2. **Webhook** gửi thông tin đơn hàng lên Discord channel tương ứng (theo game: Diablo 4, POE1, POE2, default)
3. **Discord Bot** nhận webhook, tạo thread kèm nút bấm "Khách vào" / "Đã giao"
4. **Operator** kéo thả bằng chứng (ảnh/video) vào thread, bấm nút
5. **Worker** download proof, tự động upload + chat trên nền tảng, sau đó khóa thread

## Tính năng

- **4 Driver song song:** 2 Scanner (quét đơn) + 2 Worker (giao hàng), không block lẫn nhau
- **Auto Scanner:** Tự động quét đơn mới trên Eldorado & G2G, lọc theo whitelist/blacklist, cache 3 giờ
- **Eldorado Fast Mode:** Nút "Khách vào" — F5 liên tục (10 lần, 60s/lần) bấm "Order Delivered"
- **Eldorado Normal Mode:** Upload proof qua TalkJS iframe, gửi tin nhắn qua TalkJS WebSocket (fallback REST API / UI)
- **G2G Delivery:** Nhập số lượng, upload proof gallery, inject tin nhắn vào ProseMirror editor
- **Discord Thread Management:** Tự động tạo thread theo Order ID, khóa & archive sau khi xong
- **Anti-detection:** Xóa `navigator.webdriver`, tắt `AutomationControlled`, random scan interval
- **Game-specific routing:** Đơn hàng được route đến webhook theo game (Diablo 4, POE1, POE2)

## Yêu cầu hệ thống

- **Python 3.10+**
- **Google Chrome** (phiên bản mới nhất)
- **Discord Bot Token** (với intent Message Content)
- **Tài khoản Eldorado/G2G** đã đăng nhập

## Cài đặt

```bash
# 1. Clone repo
git clone git@github.com:GegeTeamCode/BotPasteDon.git
cd BotPasteDon

# 2. Tạo virtual environment
python -m venv venv
# Windows:
venv\Scripts\activate

# 3. Cài dependencies
pip install -r requirements.txt

# 4. Copy và cấu hình .env
cp .env.example .env
# Chỉnh sửa .env: BOT_TOKEN, CHANNEL_IDS, WEBHOOK URLs

# 5. Đăng nhập Chrome thủ công lần đầu
python manual_login.py
# Chọn profile cần đăng nhập, đăng nhập xong đóng trình duyệt

# 6. Chỉnh sửa message.txt (nội dung tin nhắn cảm ơn gửi khách)

# 7. Cấu hình config.py (whitelist, blacklist, scan interval...)
```

## Cách chạy

```bash
python GegeOrder.py
```

Bot sẽ khởi động 4 Chrome driver, kết nối Discord, và bắt đầu quét đơn (nếu `auto_start: true`).

## Sử dụng

### Nút bấm trong Discord Thread

| Nút | Màu | Chức năng | Platform |
|-----|-----|-----------|----------|
| Khách vào (Uu tien) | Đỏ | Fast delivery — F5 liên tục bấm "Order Delivered" | Eldorado |
| Da giao (Gui Proof) | Xanh | Upload proof + gửi tin nhắn + xác nhận giao | Cả hai |

### Discord Commands

| Lệnh | Quyền | Mô tả |
|------|-------|-------|
| `!scan_start` | Admin | Bật Auto Scanner |
| `!scan_stop` | Admin | Tắt Auto Scanner |
| `!scan_status` | Tất cả | Xem trạng thái scanner + số đơn đã quét |
| `!scan_clear` | Admin | Xóa cache đơn đã quét |
| `!scan_test [platform]` | Tất cả | Test quét 1 lần (`eldorado` hoặc `g2g`) |
| `!help_scan` | Tất cả | Hiển thị hướng dẫn scanner |

## Cấu trúc thư mục

```
BotPasteDon/
├── .env                          # Secrets (BOT_TOKEN, CHANNEL_IDS, WEBHOOKs)
├── .env.example                  # Template cho .env
├── config.py                     # Cấu hình scanner, whitelist/blacklist, webhooks
├── GegeOrder.py                  # Main entry point — Discord bot + orchestrator
├── driver_manager.py             # Tạo & cấu hình Chrome WebDriver (anti-detection)
├── manual_login.py               # Tool đăng nhập thủ công vào 4 Chrome profile
├── order_scanner.py              # Auto scanner — quét đơn Eldorado & G2G
├── order_queue.py                # Worker queues + Selenium logic (delivery, proof, chat)
├── talkjs_client.py              # TalkJS WebSocket/REST client cho Eldorado chat
├── message.txt                   # Nội dung tin nhắn cảm ơn gửi khách
├── requirements.txt              # Python dependencies
├── Guide.txt                     # Hướng dẫn setup chi tiết (Tiếng Việt)
├── proofs/                       # Thư mục tạm chứa bằng chứng (auto-create)
├── cache/                        # Cache đơn đã quét (JSON, auto-clean 3h)
├── chrome_profile_eldo_worker/   # Chrome profile — Worker Eldorado
├── chrome_profile_eldo_scanner/  # Chrome profile — Scanner Eldorado
├── chrome_profile_g2g_worker/    # Chrome profile — Worker G2G
└── chrome_profile_g2g_scanner/   # Chrome profile — Scanner G2G
```

## Mô tả file

### `GegeOrder.py` — Main Entry Point
Khởi tạo 4 Chrome driver, Discord bot, scanner instances. Xử lý webhook messages, tạo thread với nút bấm, điều phối scanner và worker.

### `config.py` — Cấu hình
Load `.env` qua `python-dotenv`. Định nghĩa `SCANNER_CONFIG` với whitelist/blacklist, scan interval, platform toggles, webhook routing theo game.

### `driver_manager.py` — Chrome Driver
Tạo Selenium WebDriver với anti-detection: xóa `enable-automation`, tắt `AutomationControlled`, ẩn `navigator.webdriver`. Dùng `webdriver_manager` tự cài ChromeDriver.

### `order_scanner.py` — Auto Scanner
Class `OrderScanner` quét trang pending orders mỗi 15-25s. Dùng `ThreadPoolExecutor` (4 workers) để chạy Selenium sync mà không block Discord event loop. Extract chi tiết đơn hàng (game, server, item, quantity, character). Cache processed orders 3 giờ.

### `order_queue.py` — Order Processing
Hàng đợi `asyncio.Queue` cho mỗi platform. `DeliveryView` (Discord UI buttons). Logic giao hàng:
- **Eldorado:** Click "Delivered" → upload proof qua TalkJS iframe (retry 5 lần/file, 3 global retries) → gửi tin nhắn qua WebSocket/REST/UI
- **G2G:** Nhập quantity → upload proof gallery → inject tin nhắn vào ProseMirror editor → gửi

### `talkjs_client.py` — TalkJS Client
WebSocket client dùng Phoenix Protocol giao tiếp trực tiếp với TalkJS server. Extract auth token từ browser, gửi tin nhắn qua WebSocket (primary), REST API (fallback). App ID hardcoded `49mLECOW`.

### `manual_login.py` — Đăng nhập thủ công
CLI menu mở Chrome với từng profile để đăng nhập thủ công Eldorado/G2G. Cookies persist trong profile.

## Cấu hình Webhook

Trong `.env`, định nghĩa webhook URLs cho từng game channel:

```env
WEBHOOK_DEFAULT=https://discord.com/api/webhooks/...
WEBHOOK_DIABLO4=https://discord.com/api/webhooks/...
WEBHOOK_POE2=https://discord.com/api/webhooks/...
WEBHOOK_POE1=https://discord.com/api/webhooks/...
```

Trong `config.py`, mapping keywords → webhook:

```python
"mappings": [
    {"game": "Diablo 4", "keywords": ["diablo 4", "d4"], "url": os.getenv("WEBHOOK_DIABLO4")},
    {"game": "POE2", "keywords": ["poe 2", "poe2", "path of exile 2"], "url": os.getenv("WEBHOOK_POE2")},
    {"game": "POE1", "keywords": ["poe 1", "poe1", "path of exile"], "url": os.getenv("WEBHOOK_POE1")},
]
```

## Dependencies

| Package | Mục đích |
|---------|----------|
| `discord.py` | Discord bot framework |
| `selenium` | Browser automation (Chrome) |
| `webdriver-manager` | Tự cài ChromeDriver |
| `aiohttp` | Async HTTP client (webhook, REST API) |
| `websockets` | TalkJS WebSocket client |
| `python-dotenv` | Load `.env` file |

## Lưu ý

- Phải chạy `manual_login.py` đăng nhập lần đầu trước khi chạy bot
- Không mở Chrome cùng profile khi bot đang chạy (xung đột profile)
- Scanner cache tự clean sau 3 giờ, hoặc dùng `!scan_clear`
- TalkJS auth token có thể expire — bot tự động extract mới từ browser
- G2G dùng Vue.js — dùng `send_keys` thay vì `execute_script` để trigger reactivity
