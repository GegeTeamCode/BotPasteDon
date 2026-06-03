# Architecture — BotPasteDon

## Tong quan

BotPasteDon la he thong multi-process tu dong hoa quy trinh quat don va giao hang tren 2 marketplace: **Eldorado.gg** va **G2G.com**. Gom 8 process doc lap giao tiep qua HTTP API va shared SQLite database.

## Process Map

| Process | Port | Entry Point | Vai tro |
|---------|------|-------------|---------|
| Auth Service | 8010 | `python -m auth.main` | Capture va serve G2G JWT + Eldo cookies |
| Eldo Scanner | -- | `python -m scanners.main --platform eldorado` | Poll Eldo API, gui Discord + ERP webhook |
| G2G Scanner | -- | `python -m scanners.main --platform g2g` | Poll G2G API, gui Discord + ERP webhook |
| Eldo Worker | 8001 | `python -m workers.eldorado_worker` | Thuc hien giao hang Eldorado |
| G2G Worker | 8002 | `python -m workers.g2g_worker` | Thuc hien giao hang G2G |
| Coordinator | 8030 | `python -m coordinator.main` | Discord bot, dispatch task den workers |
| Dashboard | 8766 | `python -m dashboard.server` | Web UI monitoring, OTP relay, logs |
| Watchdog | -- | `python scripts/watchdog.py` | Auto-restart crashed services |

## Data Flow

```
  Eldorado API              G2G API
       │                       │
       ▼                       ▼
  Eldo Scanner           G2G Scanner
       │                       │
       ├─ Discord Webhook ────►│
       ├─ ERP Webhook ────────►│
       │                       │
       ▼                       ▼
  Coordinator (Discord Bot)
       │
       ├── POST /task ──► Eldo Worker :8001
       └── POST /task ──► G2G Worker  :8002

  Auth Service :8010 ◄─── All processes fetch JWT/cookies here
  SQLite DB ◄─────────── All processes read/write orders
```

## Module Details

### auth/

**`auth/main.py`** — HTTP service (aiohttp, port 8010) quan ly browser sessions.

- **G2G**: Chrome headless + CDP capture JWT tu network requests. JWT song 15 phut, capture moi 13 phut.
- **Eldorado**: Camoufox (anti-detect Firefox) capture cookies + XSRF token. 3 profiles: main, bak1, bak2 — rotate khi profile fail.
- Endpoints: `GET /auth/g2g`, `GET /auth/eldo`, `GET /health`, `POST /auth/otp`
- 5-min client cache. Auto-retry capture khi auth het han.

### scanners/

**`scanners/main.py`** — CLI entry point. Chon API hoac Selenium mode tuy config.

**`scanners/base_scanner.py`** — Base class voi scan loop, in-memory + SQLite dedup, async Selenium helpers.

**`scanners/eldorado_scanner_api.py`** — REST API scanner (default).
- Poll `eldorado.gg/api/orders/me/seller/orders` voi `orderState=PendingDelivery`
- Keyword filter: `item_name + offerTitle + gameCategoryTitle` vs whitelist/blacklist
- Game detection: heuristic tu `attributeId` (VD: `path-of-exile-2-orbs` → PoE2)
- Pricing: `totalPrice.amount`, `sellerPayments.sellerFees.amount` (commission), earning = total - fee

**`scanners/g2g_scanner_api.py`** — REST API scanner (default).
- Poll `sls.g2g.com/order/list_my_order`
- Smart JWT retry: khi 401 → invalidate cache → poll fresh JWT (120s timeout) → retry 1 lan
- Flow: `start_deliver → mark_as_delivering → re-fetch detail`
- Pricing: earning + commission_fee tinh total, unit_price = total/qty

**`scanners/eldorado_scanner.py`** / **`scanners/g2g_scanner.py`** — Selenium fallback (khi API khong available).

### workers/

**`workers/eldorado_worker.py`** — HTTP API (aiohttp, port 8001).
- **API mode**: `deliver_order` → `upload_proof` (Firebase Storage) → `send_message` (TalkJS WebSocket)
- **Selenium mode**: Click "Delivered" → upload proof qua TalkJS iframe → chat qua WS/REST
- Per-step delivery voi `skip_steps` tracking trong DB `retry_data`
- Recovery loop: 60s, check orders stuck in DELIVERING, retry tu step failed

**`workers/g2g_worker.py`** — HTTP API (aiohttp, port 8002).
- **API mode**: `submit_qty` → `upload_proof` (S3 presigned) → `create_sendbird_channel` → `send_chat`
- **Selenium mode**: Fill qty → upload gallery → inject ProseMirror → send
- JWT-expired recovery: check `error_message.startswith("JWT_EXPIRED:")`, retry khi co JWT moi

**`workers/talkjs_client.py`** — TalkJS WebSocket client (Phoenix Protocol). File upload qua Firebase Storage resumable upload.

**`workers/base_worker.py`** — Shared utilities: `DeliveryView` (Discord buttons), file cleanup, thread locking.

### coordinator/

**`coordinator/discord_bot.py`** — Discord bot + HTTP callback server (port 8030).
- Nhan webhook messages trong Discord channels
- Tao per-order thread voi platform-specific buttons: "Giao nhanh" / "Gui bang chung"
- Dispatch tasks den Workers qua `POST /task` voi order data, ERP URL, skip_steps
- Startup recovery: re-process orders stuck in THREAD_CREATED
- Lock/archive threads khi delivery complete

**`coordinator/main.py`** — Thin entry point.

### dashboard/

**`dashboard/server.py`** — aiohttp web server (port 8766).
- Service health (heartbeat-based)
- Auth status cards (JWT/cookies freshness)
- G2G auto-login voi OTP relay
- Order list voi pagination
- Real-time log viewer qua SSE

**`dashboard/templates/index.html`** — Single-page dark-themed UI.

### shared/

| File | Mo ta |
|------|-------|
| `config.py` | Load .env, dinh ngha SCANNER_CONFIG (whitelist/blacklist, webhook routing, G2G title mapping, scan intervals) |
| `constants.py` | Order states, platform URLs, cache TTL, user-agent |
| `database.py` | SQLite WAL, thread-safe. Tables: `orders` (lifecycle), `heartbeat` (monitoring) |
| `discord_utils.py` | `format_order_message`, `match_webhook`, `send_discord_webhook`, `send_erp_webhook` |
| `driver_manager.py` | Chrome WebDriver factory voi anti-detection |
| `eldo_api.py` | Eldo REST client (curl_cffi). Pending orders, detail, deliver, TalkJS auth, game library |
| `eldo_auth.py` | Eldo auth manager. Fetch cookies + XSRF tu auth service, 5-min cache |
| `g2g_api.py` | G2G REST client (curl_cffi). Pending orders, detail, deliver, S3 upload, Sendbird chat |
| `g2g_auth.py` | G2G auth manager. Fetch JWT tu auth service, 5-min cache |
| `logging_config.py` | Structured logging: `[HH:MM:SS][logger] LEVEL: message`, flush after every emit |
| `order_state.py` | State machine: DETECTED → NOTIFIED → THREAD_CREATED → DELIVERING → COMPLETED |

## Keyword Filtering

Scanner loc don hang qua 2 layer:

1. **Blacklist** (reject): "Boosting, Leveling, Account, Custom oder"
2. **Whitelist** (accept): "Divine Orb, Chaos Orb, Exalted Orb, Mirror of Kalandra, Gold, Boss Materials, Runes, Currency, Gems, ..."
- **Eldorado**: filter tren `item_name + offerTitle + gameCategoryTitle`
- **G2G**: filter tren `title` (Gold orders auto-pass khi `unit_name` co "gold")

Don bi loc → insert DB voi status DETECTED (khong gui webhook).

## Webhook Routing

`match_webhook()` trong `discord_utils.py`:
- First-keyword-match tren `game_name + item_name`
- Thu tu mapping quyet dinh: Diablo 4 → PoE2 → PoE1 → Default

## ERP Integration

Scanner gui `POST` den ERP webhook cho moi don moi:
- URL: `http://<ERP_HOST>/api/method/gege_custom.gege_custom.api.botpastedon.new_order`
- Header: `X-API-Key` (khac nhau cho Eldo va G2G)
- Required fields: `orderId`, `platform`
- Pricing: `total_price`, `unit_price`, `earning`, `channel_fee`, `channel_fee_rate`

ERP tao Sell Order trong Frappe/ERPNext. Worker callback khi giao xong.

## Database Schema

```sql
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    order_url TEXT,
    game TEXT,
    server TEXT,
    item_name TEXT,
    quantity TEXT,
    character TEXT,
    customer_name TEXT,
    discord_thread_id TEXT,
    discord_channel_id TEXT,
    webhook_sent_at DATETIME,
    delivery_started_at DATETIME,
    delivery_completed_at DATETIME,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    raw_data TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    retry_data TEXT,
    erp_synced INTEGER DEFAULT 0,
    erp_retry_count INTEGER DEFAULT 0
);

CREATE TABLE heartbeat (
    service_name TEXT PRIMARY KEY,
    last_beat DATETIME,
    pid INTEGER
);
```

## Order State Machine

```
DETECTED → NOTIFIED → THREAD_CREATED → DELIVERING → COMPLETED
    │                                        │
    └─── (keyword filtered, no webhook)      └─── FAILED → retry from previous state
```

## Auth Architecture

### G2G Auth
- Chrome headless mo g2g.com, capture JWT tu CDP network requests
- JWT song ~15 phut, auth service capture moi 13 phut
- Clients cache 5 phut, tu dong invalidate khi 401

### Eldorado Auth
- Camoufox (anti-detect Firefox) mo eldorado.gg, capture cookies + XSRF token
- 3 profiles: `chrome_profile_eldo` (main), `_bak1`, `_bak2`
- Auth service rotate profile khi capture fail
- **Fix**: Camoufox Playwright sync API xung dot voi asyncio — fix bang `asyncio.set_event_loop(asyncio.new_event_loop())` trong worker thread
