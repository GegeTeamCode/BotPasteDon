# Architecture — BotPasteDon

## Tong quan

BotPasteDon la he thong multi-process tu dong hoa quy trinh quat don va giao hang tren 2 marketplace: **Eldorado.gg** va **G2G.com**. Gom 9 process doc lap giao tiep qua HTTP API va shared SQLite database.

## Process Map

| Process | Port | Entry Point | Vai tro |
|---------|------|-------------|---------|
| Auth Service | 8010 | `python -m auth.main` | Capture va serve G2G JWT + Eldo cookies |
| Eldo Scanner | -- | `python -m scanners.main --platform eldorado` | Poll Eldo API, gui Discord + ERP webhook |
| G2G Scanner | -- | `python -m scanners.main --platform g2g` | Poll G2G API, gui Discord + ERP webhook |
| Eldo Worker | 8001 | `python -m workers.eldorado_worker` | Thuc hien giao hang Eldorado |
| G2G Worker | 8002 | `python -m workers.g2g_worker` | Thuc hien giao hang G2G |
| Coordinator | 8030 | `python -m coordinator.main` | Discord bot, dispatch task den workers |
| Status Sync | -- | `python -m status_sync` | Poll marketplace state (G2G + Eldo) → push ERP `status_update` mỗi 30 min |
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

  Status Sync ─── polls G2G + Eldo state every 30m ───► ERP status_update webhook
              ───► writes marketplace_status / marketplace_disputes (SQLite)
```

## Module Details

### auth/

**`auth/main.py`** — HTTP service (aiohttp, port 8010) quan ly browser sessions.

- **G2G**: Two-tier (backend refresh `POST sls.g2g.com/user/refresh_access` ~1s + Chrome+CDP fallback ~30-60s). JWT song 15 phut, refresh moi 13 phut.
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

### status_sync/

**`status_sync/main.py`** — long-running process. Async cycle moi `STATUS_SYNC_INTERVAL_SEC` (default 1800s = 30m). Heartbeat 30s vao bang `heartbeat`. Signal handler (SIGTERM/SIGINT) → graceful shutdown. CLI: `--interval <sec>` override + `--once` chay 1 cycle roi exit (testing). Abort startup neu `ERP_STATUS_UPDATE_URL` rong.

Moi cycle goi `G2GSync.run_once()` va `EldoSync.run_once()` song song qua `asyncio.gather(return_exceptions=True)` — exception 1 ben khong huy ben kia.

- **`G2GSync`** (`g2g_sync.py`):
  - Tripwire: `count_my_orders` so sanh voi snapshot `marketplace_state_counts`. Fetch `list_my_order` cho `completed`/`cancelled` khi `delivering` count doi hoac `last_order_completed_at` tien. (`issues` count la signal khong-action, chi de notice.)
  - Cases/resolution: `list_my_cases` chay **moi cycle** (20-page cap), KHONG gated by tripwire. Phan loai theo `report_case` (`cancel`→`cancel_requested`, con lai→`disputed`) → push **alert NON-BLOCKING** sang ERP: ON khi case mo (`open`/`escalate`) + chua tung alert, OFF khi case `close` + dang co alert. ERP chi set/clear field `custom_marketplace_alert`, KHONG doi workflow_state (trader van giao hang). `notified_pushed_at` = co "ERP dang co alert active" (idempotent + retry khi push fail, khong spam history da close).
  - State list khong paginate cap o status_sync layer — relies on `list_orders_by_status` return all.
- **`EldoSync`** (`eldo_sync.py`):
  - Tripwire: `statesCount` so sanh snapshot. Fetch `/api/orders/me/seller/orders` cho cac state co count delta.
  - Pagination: 1500 trang max on first-run backfill; 25 trang + early-exit sau 50 known-orders lien tiep tren incremental.
- **`ERPClient`** (`erp_client.py`): aiohttp `POST status_update` voi exponential backoff (2/4/8s, default 3 attempts). 4xx → KHONG retry (validation/auth fix manually). 5xx → retry. Headers: `X-API-Key` lay tu `ERP_API_KEY_G2G` hoac `ERP_API_KEY_ELDO` tuy `payload.platform`.
- **First run** (`marketplace_state_counts` rong): silent backfill — insert toan bo state hien tai vao DB KHONG push ERP (tranh spam ~10k transitions gia). Tu cycle 2: chi push khi `prev_state != new_state`.

State mapping → ERP (terminal = workflow_state; alert = field, non-blocking):
| Marketplace state | ERP xu ly |
|---|---|
| g2g.completed / eldo.Completed | → Completed (+credit vi, idempotent) |
| g2g.cancelled / eldo.Canceled | ERP tu quyet theo so cua chinh no: chua tung credit (khong ALE "In") → **Cancelled**; da credit → **Refunded** + dao vi (ALE Out mirror In, KHONG dung kho). (Sua decision 2026-06-07 "moi cancelled→Refunded".) |
| g2g `cancel_requested` (case report_case=cancel) | **Alert** field `custom_marketplace_alert="Cancel Requested"` — KHONG doi workflow_state |
| g2g `disputed` (case khac) / eldo.Disputed | **Alert** field `custom_marketplace_alert="Dispute Open"` — KHONG doi workflow_state |
| eldo.Delivered | Delivered |
| eldo.Received / eldo.PendingDelivery | (ignored) |

PROTECTED workflow states ERP webhook KHONG override: `Refunded`, `Partially Refunded`, `Cancellation Requested`, `Outstanding`, `Payment Pending`. (`Cancellation Requested` = luong **manual**; status_sync KHONG set state nay nua — chi set field alert.)

**ERP-side `status_update` handler safety layers** (gege_custom `api/botpastedon.py::status_update`, deployed 2026-06-10):

1. **PROTECTED check** — current state in the set above → return `protected`, no mutation.
2. **BLOCK check** — current state in `_BLOCK_CURRENT_STATES = {"In Delivery"}` → return `manual_required`, log Warning. A trader is actively delivering and holds inventory locks; bot must not jump in.
3. **Whitelist check** — `_SAFE_TRANSITIONS = {Delivered: {Completed, Disputed, Refunded}, Outstanding: {Completed, Refunded}, Completed: {Disputed}, Disputed: {Refunded, Completed}}`. Anything outside (e.g., source=Queued/Claimed/Evidence Uploaded) → `unsafe_transition`, log Warning.
4. **Apply** — write via `frappe.db.set_value` (bypasses `save()`'s workflow validation, which would otherwise raise `PermissionError` because the webhook runs as Guest and the `_doc_before_save` snapshot doesn't carry `ignore_permissions`). Safe because the whitelisted targets have no business hooks in `Sell Order.before_save`.

Every outcome (except `no_change` and `no_so` — too noisy) writes a `WS Activity Log` row with `action="status_update"`, status Info/Warning, full payload. Search there for monitoring + audit.

**Config knobs** (`.env` / env vars):
- `STATUS_SYNC_INTERVAL_SEC` — cycle interval (default 1800)
- `ERP_STATUS_UPDATE_URL` — full endpoint. Khong set → auto-derive tu `ERP_WEBHOOK_URL` bang cach thay `.new_order` thanh `.status_update`
- `ERP_API_KEY_G2G`, `ERP_API_KEY_ELDO` — fallback `ERP_API_KEY` neu thieu

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
| `database.py` | SQLite WAL, thread-safe. Tables: `orders` (lifecycle), `heartbeat` (monitoring), `marketplace_status` / `marketplace_state_counts` / `marketplace_disputes` (status_sync) |
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

-- status_sync (added 2026-06):
CREATE TABLE marketplace_status (
    platform TEXT NOT NULL,           -- "g2g" | "eldorado"
    order_id TEXT NOT NULL,
    order_item_id TEXT,
    marketplace_state TEXT NOT NULL,  -- "completed" / "cancelled" / "disputed" / ...
    marketplace_state_at INTEGER,
    last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_pushed_at DATETIME,           -- NULL = not yet pushed to ERP
    push_attempts INTEGER DEFAULT 0,
    raw_data TEXT,
    PRIMARY KEY (platform, order_id)
);

CREATE TABLE marketplace_state_counts (
    platform TEXT NOT NULL,
    state TEXT NOT NULL,               -- e.g. "completed", "delivering", camelCase keys
    count INTEGER NOT NULL,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (platform, state)
);

CREATE TABLE marketplace_disputes (
    platform TEXT NOT NULL,
    case_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    case_status TEXT NOT NULL,         -- "open" / "escalate" / "close"
    report_case TEXT,                  -- "cancel" (=cancel request) / "did_not_receive" (=dispute) / ...
    report_reason TEXT,
    report_qty INTEGER,
    first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_synced_at DATETIME,
    notified_pushed_at DATETIME,        -- flag "ERP dang co alert active": set khi push alert-ON OK, clear khi push alert-OFF OK
    raw_data TEXT,
    PRIMARY KEY (platform, case_id)
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
- JWT song ~15 phut, auth service capture moi 13 phut
- Clients cache 5 phut, tu dong invalidate khi 401
- Cookies critical: `refresh_token` (`<user_id>.<hex>`, TTL ~12 ngay sliding), `long_lived_token`, `active_device_token`
- JWT luu trong localStorage key `accessToken` (G2GSls self-issued, `iss=G2GSls`, `aud=https://www.g2g.com`)

**Two-tier refresh strategy** (Phase 5, 2026-06-10):

1. **Backend refresh (fast path, ~1s, no browser)** — `POST https://sls.g2g.com/user/refresh_access`
   - Body: `{user_id, refresh_token, active_device_token, long_lived_token}` — user_id la `sub` cua JWT hien tai, 3 token con lai lay tu cookies
   - Headers: `authorization: Bearer <current_jwt>`, `origin: https://www.g2g.com`, content-type json, cookie header
   - Response 200 `{code:2000, payload:{access_token, refresh_token, long_lived_token, active_device_token, *_exp}}` — moi token co exp moi (sliding window). Refresh_token slide ~12 ngay moi call → khong bao gio het han neu refresh deu
   - Su dung `curl_cffi` impersonate `chrome120` cho TLS fingerprint khop browser

2. **Selenium fallback (slow path, ~30-60s, browser)** — chi khi backend refresh fail
   - Chrome headless mo `g2g.com/g2g-user/sale?status=preparing`
   - CDP performance log intercept `Authorization: Bearer ...` headers tu requests den `sls.g2g.com`
   - Fallback localStorage `accessToken` neu CDP khong bat duoc
   - Extract cookies, validate JWT exp

Xem [docs/marketplace_auth.md](marketplace_auth.md) cho chi tiet endpoint contract + discovery methodology.

### Eldorado Auth
- Eldorado dung **AWS Cognito** lam OAuth broker (Google login → Cognito session)
- Cookies critical: `__Host-EldoradoIdToken` (JWT, TTL ~1h), `__Host-EldoradoRefreshToken` (TTL ~30 ngay), `__Host-XSRF-TOKEN`
- 3 profiles: `chrome_profile_eldo` (main), `_bak1`, `_bak2`
- Auth service rotate profile khi capture fail

**Two-tier refresh strategy** (Phase 4, 2026-06-08):

1. **Backend refresh (fast path, ~1s, no browser)** — `POST https://www.eldorado.gg/api/authentication/refreshTokens`
   - Su dung cached cookies (RefreshToken + XSRF + others) + headers (`x-xsrf-token`, `x-client-build-time`, UA)
   - Body: `{}` (Eldo backend tu doc RefreshToken tu Cookie header)
   - Response 200 + `Set-Cookie` chua IdToken (va co the rotated RefreshToken)
   - Auth call API probe (`/api/orders/me/statesCount`) de verify
   - **Khong dung AWS Cognito truc tiep**: Eldorado client `3a4hal6jgl8gf5hnnjo06k05s5` configured voi client secret → cac request truc tiep den `cognito-idp.us-east-2.amazonaws.com` tra `NotAuthorizedException: SECRET_HASH was not received`

2. **Camoufox fallback (slow path, ~30s, browser)** — chi khi backend refresh fail
   - Camoufox (anti-detect Firefox) mo `https://www.eldorado.gg/dashboard/orders/sold`
   - `page.on("response")` listener (Firefox khong ho tro CDP) capture `nsure-device-id` + `x-client-build-time` headers tu authenticated XHRs cua page
   - Extract cookies, run API probe
   - Cookie preservation guard: neu capture moi mat IdToken/RefreshToken so voi truoc → reject, giu bundle cu

**Camoufox capture flow**:
- Phase 1: home page (Cloudflare check)
- Phase 2: navigate `/dashboard/orders/sold` voi `wait_until="domcontentloaded"` + `wait_for_load_state("networkidle", 15s)`
- Phase 3: extract cookies + verify API probe

**Fixes**:
- Camoufox Playwright sync API xung dot voi asyncio → fix bang `asyncio.set_event_loop(asyncio.new_event_loop())` trong worker thread
- G2G chromedriver: bo qua `webdriver_manager` (FileLock leak vao auth process tu deadlock) → glob `~/.wdm/drivers/.../chromedriver` truc tiep, fallback `ChromeDriverManager().install()` chi khi binary chua ton tai (Phase fix 2026-06-06)
