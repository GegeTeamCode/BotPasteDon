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
             │ Discord Webhook + ERP                       │
             ▼                                             ▼
   ┌──────────────────────────────────────────────────────────────┐
   │              Coordinator (Discord Bot) :8030                 │
   │    Nhan webhook → tao Discord thread + nut bam               │
   │    Dispatch task den Worker qua HTTP                         │
   └──────────┬─────────────────────────────────┬────────────────┘
              │                                  │
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
2. **Discord Webhook** gui thong tin don hang len channel tuong ung (Diablo 4, PoE2, PoE1)
3. **ERP Webhook** dong bo don hang vao ERP (Frappe/ERPNext)
4. **Coordinator** nhan webhook, tao Discord thread kem nut bam "Giao nhanh" / "Gui bang chung"
5. **Worker** nhan task qua HTTP, tu dong giao hang (mark delivered, upload proof, chat)
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
cp .env.example .env        # Config tokens, webhooks, ERP URL
```

## Cach chay

```bash
# Chay tung service
python -m auth.main                          # Auth service (port 8010)
python -m scanners.main --platform eldorado  # Eldo scanner
python -m scanners.main --platform g2g       # G2G scanner
python -m workers.eldorado_worker            # Eldo worker (port 8001)
python -m workers.g2g_worker                 # G2G worker (port 8002)
python -m coordinator.main                   # Discord bot (port 8030)
python scripts/watchdog.py                   # Watchdog
python -m dashboard.server                   # Dashboard (port 8766)

# Hoac chay tat ca
bash scripts/start.sh
```

**Thu tu khoi dong:** Auth → Workers → Coordinator → Scanners → Watchdog → Dashboard

## Cau truc thu muc

```
BotPasteDon/
├── auth/                   # Auth service - G2G JWT + Eldo cookies
├── coordinator/            # Discord bot + HTTP callback server
├── dashboard/              # Web UI - status, logs, OTP relay
├── deploy/                 # systemd units + start/stop scripts
├── docs/                   # Architecture + operations docs
├── scanners/               # Order scanners (API + Selenium fallback)
├── scripts/                # start.sh, stop.sh, watchdog.py
├── shared/                 # Config, DB, API clients, utilities
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
| discord.py | Discord bot framework |
| selenium | Browser automation (fallback mode) |
| curl_cffi | HTTP client voi browser impersonation |
| aiohttp | Async HTTP (webhooks, worker API) |
| websockets | TalkJS WebSocket (Eldo chat) |
| python-dotenv | Load .env |
| camoufox | Anti-detect Firefox (Eldo auth) |
