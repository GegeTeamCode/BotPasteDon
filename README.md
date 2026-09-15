# BotPasteDon

Multi-process bot tu dong hoa quat don va giao hang tren **Eldorado.gg** va **G2G.com**.

## Kien truc tong quan

```
                        ┌─────────────────────────┐
                        │    Auth Service :8010    │
                        │  G2G JWT + Eldo Cookies  │
                        └──────┬──────────┬────────┘
                               │          │
              ┌────────────────┘          └────────────────┐
              ▼                                              ▼
   ┌─────────────────────┐                      ┌─────────────────────┐
   │  Eldo Scanner (API) │                      │   G2G Scanner (API) │
   │  Poll pending orders│                      │   Poll pending orders│
   └─────────┬───────────┘                      └──────────┬──────────┘
             │ ERP new_order webhook                       │
             ▼                                             ▼
   ┌──────────────────────────────────────────────────────────────┐
   │        ERP (.100 main / .102 currency) — Sell Order          │
   │    Trader xu ly don tren ERP → ERP dispatch task giao hang   │
   └──────────┬─────────────────────────────────┬────────────────┘
              │ POST /task                       │ POST /task
              ▼                                  ▼
   ┌──────────────────────┐          ┌──────────────────────┐
   │  Eldorado Worker     │          │     G2G Worker       │
   │  :8001               │          │     :8002            │
   │  TalkJS + Firebase   │          │  Sendbird + S3       │
   └──────────────────────┘          └──────────────────────┘

   ┌──────────────┐        ┌─────────────────┐
   │  Dashboard   │        │   Watchdog      │
   │  :8766       │        │   Monitor + restart │
   └──────────────┘        └─────────────────┘

   Shared: SQLite DB (data/orders.db) + Auth Service (cookies/JWT)
```

**Luong hoat dong:**

1. **Scanner** poll API moi 15-25s, loc whitelist/blacklist, extract chi tiet don hang
2. **ERP Webhook** dong bo don hang vao ERP (Frappe/ERPNext) — PoE/PoE2/Torchlight → .102, con lai → .100
3. **ERP** dispatch task giao hang den **Worker** qua HTTP `POST /task`
4. **Worker** tu dong giao hang (mark delivered, upload proof, chat)
5. **Status Sync** day trang thai marketplace (completed/cancelled/dispute) ve ERP
6. **Watchdog** monitor heartbeat, tu dong restart service khi crash

## Yeu cau he thong

- Python 3.10+
- Google Chrome + ChromeDriver
- Server: LXC/Linux (dang chay tren 192.168.2.220)
- ERP: Frappe/ERPNext (192.168.2.100:80)

## Cai dat

```bash
git clone git@github.com:GegeTeamCode/BotPasteDon.git
cd BotPasteDon
python -m venv venv
source venv/bin/activate    # Linux
pip install -r requirements.txt
cp .env.example .env        # Config ERP URL + API keys
```

## Cach chay

```bash
# Chay tung service
python -m auth.main                          # Auth service (port 8010)
python -m scanners.main --platform eldorado  # Eldo scanner
python -m scanners.main --platform g2g       # G2G scanner
python -m workers.eldorado_worker            # Eldo worker (port 8001)
python -m workers.g2g_worker                 # G2G worker (port 8002)
python -m status_sync                        # Status sync
python scripts/watchdog.py                   # Watchdog
python -m dashboard.server                   # Dashboard (port 8766)

# Hoac chay tat ca
bash scripts/start.sh
```

**Thu tu khoi dong:** Auth → Workers → Scanners → Status Sync → Watchdog → Dashboard

## Cau truc thu muc

```
BotPasteDon/
├── auth/                   # Auth service - G2G JWT + Eldo cookies
├── dashboard/              # Web UI - status, logs, OTP relay
├── deploy/                 # systemd units + start/stop scripts
├── docs/                   # Architecture + operations docs
├── scanners/               # Order scanners (API + Selenium fallback)
├── scripts/                # start.sh, stop.sh, watchdog.py
├── shared/                 # Config, DB, API clients, utilities
├── status_sync/            # Marketplace state → ERP status_update
├── tests/                  # Test + debug scripts
├── workers/                # Delivery workers (Eldo + G2G)
├── .env.example            # Environment template
├── message.txt             # Thank-you message template
└── requirements.txt        # Python dependencies
```

## Docs

- [docs/architecture.md](docs/architecture.md) — Cau truc du an, module, API flow
- [docs/operations.md](docs/operations.md) — Huong dan van hanh, troubleshooting, deploy

## Dependencies

| Package | Muc dich |
|---------|----------|
| selenium | Browser automation (fallback mode) |
| curl_cffi | HTTP client voi browser impersonation |
| aiohttp | Async HTTP (ERP webhook, worker API) |
| websockets | TalkJS WebSocket (Eldo chat) |
| python-dotenv | Load .env |
| camoufox | Anti-detect Firefox (Eldo auth) |
