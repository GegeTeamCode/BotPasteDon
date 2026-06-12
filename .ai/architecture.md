# Architecture — quick map for agents

> Nguồn sự thật đầy đủ: [`docs/architecture.md`](../docs/architecture.md).
> File này tóm tắt phần một agent cần biết trước khi sửa code.

## 9 services (1 instance mỗi loại, no port reuse)

| Process | Port | Entry | Vai trò 1-câu |
|---|---|---|---|
| Auth | 8010 | `python -m auth.main` | Capture + serve G2G JWT + Eldo cookies, refresh tự động |
| Eldo Scanner | – | `python -m scanners.main --platform eldorado` | Poll pending orders, push Discord + ERP `new_order` |
| G2G Scanner | – | `python -m scanners.main --platform g2g` | (same — G2G version) |
| Eldo Worker | 8001 | `python -m workers.eldorado_worker` | Mark delivered, upload proof (Firebase), chat TalkJS |
| G2G Worker | 8002 | `python -m workers.g2g_worker` | Submit qty, upload proof (S3), chat Sendbird |
| Coordinator | 8030 | `python -m coordinator.main` | Discord bot, dispatch task→workers, recovery |
| Status Sync | – | `python -m status_sync` | Poll marketplace state (30 min), push ERP `status_update` |
| Dashboard | 8766 | `python -m dashboard.server` | SSE log viewer, OTP relay, /health cards |
| Watchdog | – | `python scripts/watchdog.py` | Heartbeat poll → auto-restart crashed service |

Startup order matters: `Auth → Workers → Coordinator → Scanners → Status Sync → Watchdog → Dashboard`.

## Data flow

```
Eldo API ──► Eldo Scanner ──┐
G2G  API ──► G2G  Scanner ──┼─► Discord webhook (per-game channel)
                            └─► ERP new_order  → creates Sell Order

Coordinator (Discord bot) ◄── webhook ──► creates per-order thread
                          ├─ POST /task ─► Eldo Worker :8001
                          └─ POST /task ─► G2G  Worker :8002

Auth :8010 ◄── all processes fetch JWT/cookies here (curl_cffi)
SQLite WAL ◄── shared write target; thread-safe via shared/database.py

Status Sync ── poll marketplace state every 30m ──► ERP status_update
            ── writes marketplace_status / marketplace_disputes (SQLite)
```

## Sensitive areas (need extra care + Opus review)

1. **`auth/main.py`** — Eldo + G2G refresh + capture. Both now two-tier:
   - **Eldo (Phase 4)**: Backend refresh `POST /api/authentication/refreshTokens`
     (~1s) + Camoufox fallback at `/dashboard/orders/sold` (~30s) with
     cookie-preservation guard (reject bundle that loses IdToken/RefreshToken).
     Do NOT add AWS Cognito direct calls — client has secret, fails
     `SECRET_HASH was not received`.
   - **G2G (Phase 5, 2026-06-10)**: Backend refresh
     `POST https://sls.g2g.com/user/refresh_access` (~1s, `curl_cffi`
     impersonate `chrome120`) + Selenium CDP fallback (~30-60s). Body:
     `{user_id, refresh_token, active_device_token, long_lived_token}`.
     See [docs/marketplace_auth.md](../docs/marketplace_auth.md) for the
     full endpoint contract + discovery methodology.
2. **`shared/database.py`** — schema changes affect prod data (`data/orders.db`
   has months of order history).
3. **`status_sync/*` + ERP `status_update` handler** — push logic can
   mass-mutate ERP `workflow_state`. First-run silent backfill exists for
   a reason — disabling it spams ERP. ERP-side safety on the receiving end
   (gege_custom `api/botpastedon.py::status_update`, hardened 2026-06-10):
   PROTECTED check → BLOCK current=`In Delivery` (manual_required) →
   `_SAFE_TRANSITIONS` whitelist → `frappe.db.set_value` apply (bypass
   `save()` + workflow validation, runs raw SQL, no `set_user` escalation
   needed). WS Activity Log audits every non-noisy outcome.
4. **Webhook payloads** — money fields (`total_price`, `earning`, `channel_fee`).
   ERP team reads them for accounting.
5. **`scanners/g2g_scanner_api.py::_do_extract` Step 3** — `get_order_detail`
   has retry loop (3 attempts, 2s/4s backoff) so a curl timeout doesn't
   orphan an order on G2G (steps 1+2 already committed `delivering` state
   before retry was added). Don't remove the retry without a replacement.

## Conventions worth knowing before editing

- Logging: `from shared.logging_config import setup_logger`; format
  `[HH:MM:SS][name] LEVEL: message` (auto-flushed each emit).
- DB access: always `with self._lock: conn = self._get_conn(): try: ... finally: conn.close()`.
- HTTP servers use `aiohttp.web`. Marketplace clients use `curl_cffi` with
  `impersonate="chrome120"` or `chrome136`.
- Auth client cache TTL = 300s. On 401, scanner invalidates and re-fetches
  fresh JWT (G2G specifically polls up to 120s).
- The async/sync boundary in `auth/main.py`: Playwright `sync_api` runs in
  a worker thread under `asyncio.set_event_loop(asyncio.new_event_loop())`.
  Reusing a thread across profiles triggers "Sync API inside asyncio loop"
  → use a fresh `ThreadPoolExecutor(max_workers=1)` per profile attempt.
- SSH/deploy: paramiko + `setsid + </dev/null + & disown` via
  `Transport.open_session()`. Never `exec_command("nohup … &")` — the
  channel hangs.
