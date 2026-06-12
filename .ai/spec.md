# Spec — BotPasteDon invariant contracts

*The "do not break" layer. Skills and plans point here; they never copy these.
If a change must alter a contract below, that change needs Claude Opus review
and an entry in `.ai/decisions.md`. Money and ERP accounting state depend on
these holding.*

---

## 1. Order status state machine (`orders.status`, `shared/database.py`)

Statuses in use: **DETECTED → COMPLETED | RETRY_PENDING | FAILED**.

- `DETECTED` — scanner inserted the order; not yet delivered. Pruned after 24h
  if still DETECTED (no longer pending on the marketplace API).
- `RETRY_PENDING` — delivery failed transiently; `retry_count` bumped, kept for
  re-dispatch.
- `COMPLETED` — delivered. Pruned by the COMPLETED cleanup path.
- `FAILED` — terminal failure; **preserved as audit trail**, never auto-deleted.

Invariants:
- New rows are always inserted as `DETECTED` (see `INSERT ... VALUES (..., 'DETECTED', ...)`).
- `FAILED` and `RETRY_PENDING` are never pruned by cleanup (audit + retry).
- Status writes go through `update_order_status()`, which also stamps
  `updated_at`. Don't write `status` with a raw UPDATE that bypasses it.

## 2. DB access discipline (`shared/database.py`)

- **Every** read/write goes through the class methods, which hold the instance
  `threading.Lock`. Never open a raw `sqlite3.connect` to `data/orders.db` that
  bypasses the lock — concurrent services share this file.
- On the server (LXC) there is **no `sqlite3` CLI**; query via `venv/bin/python`
  + the `sqlite3` module.

## 3. ERP new-order webhook payload (scanners → ERP)

Composed in `scanners/eldorado_scanner_api.py` (Eldo) / `scanners/g2g_scanner.py`
(G2G). Money fields are **strings**, currency explicit. Contract:

- Identity: `platform` (`"Eldorado"`/`"G2G"`), `orderId`, `customerName`, `url`.
- Item: `game`, `server`, `itemName`, `quantity`, `character`.
- Money: `sale_currency`, `unit_price`, `total_price`, `earning`, `channel_fee`,
  `channel_fee_rate`, `order_date`.
- **`earning = round(total_price − channel_fee, 2)`** — must stay consistent;
  this feeds ERP accounting. Null only when a source amount is missing.

## 4. ERP status-update push (status_sync → ERP)

`status_sync/erp_client.py::push_status_update`. Payload **must include**
`platform`, `external_order_id`, `marketplace_state` (+ `marketplace_state_at`).

- Auth header `X-API-Key` is **per platform** (`ERP_API_KEY_G2G` vs
  `ERP_API_KEY_ELDO`) — never cross the keys.
- HTTP 4xx = validation/auth → **do not retry**; 5xx/network → retry (≤3).
- `marketplace_state` values seen: `completed`, `disputed`, … — full mapping in
  `status_sync/{eldo,g2g}_sync.py`. status_sync can mass-mutate `workflow_state`
  in ERP, so changes here are Opus-gated.

## 5. Auth token invariants (`auth/main.py`)

- **Eldo cold start**: refresh from the on-disk `RefreshToken`; do **not** strip
  it. (Regression fixed — see git history / debug-protocol gotchas.)
- **Browser capture runs in an isolated subprocess worker** so a Camoufox
  `close()` hang can't spin the auth process. Don't move capture back inline.
- `chrome_profile_eldo*` holds the live Cognito session; refresh only via the
  VNC re-login procedure in `docs/operations.md`.
