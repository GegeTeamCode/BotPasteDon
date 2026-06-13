# Handoff — 2026-06-13 (Dashboard SSE realtime refactoring)

> Previous handoff 2026-06-10 moved to decisions.md + task-log.md.
> This file reflects current state for the next session.

## Đã làm xong trong session

- **Dashboard SSE realtime + Alpine.js reactive UI** — `7ed8c53`
  - Backend: expand SSE broadcaster to push 5 event types on sub-intervals
    (status 5s, auth 3s, orders 2s, log_update 1s). Incremental log reader
    via byte-offset tracking with rotation handling. Order change detection
    via `MAX(updated_at)` + `count(*)` comparison. Initial data burst on
    SSE connect (added to broadcast list AFTER burst to avoid race).
  - Frontend: Alpine.js reactive UI (~14KB CDN), zero `setInterval()` polling.
    Polished dark theme: connection indicator, JWT expiry progress bar,
    log level filters (ALL/INFO/WARN/ERR), scroll-based auto-scroll disable,
    pagination-safe SSE orders update.
  - 5 review fixes applied (WARN filter, pagination override, x-intersect→@scroll,
    SSE race condition, order deletion detection).
- **Opus review passed** — APPROVE with fixes applied.

## Đang dở / chưa làm

- **Deploy lên prod `.220`** — code đã commit `7ed8c53`, cần:
  ```bash
  git push origin main
  python scripts/deploy_git.py dashboard
  ```
- **Alpine.js CDN dependency** — nếu browser xem dashboard không có internet,
  cần download `alpine.min.js` về `dashboard/static/` và thêm static route.
  Hiện tại dùng CDN (browser cần internet, không phải server).
- **`g2g_scanner_api.py` Step 3 retry fix** — vẫn CHƯA commit/deploy từ session 2026-06-10.
- **`gege_custom` repo push dev → CI → prod** — vẫn pending từ session 2026-06-10.
- **bak1 Eldo profile VNC re-login** — vẫn pending.
- **`check_all_processes.py` "8 services"** — nên update thành "9 services".

## Quyết định quan trọng

- **Alpine.js over vanilla JS** — reactive data binding, no build step, single HTML
  file consistent với project style. ~14KB CDN load.
- **SSE over WebSocket** — unidirectional sufficient, auto-reconnect built-in,
  no new dependency.
- **SSE orders pagination-safe** — frontend chỉ update `this.orders` từ SSE khi
  `orderPage === 0`; paginated views dùng REST `fetchOrders()`.
- **Order deletion detection** — so sánh cả `MAX(updated_at)` và `count(*)` để
  phát hiện khi `cleanup_old_orders` xóa đơn.

## Việc tiếp theo (đề xuất thứ tự)

1. **Push + deploy dashboard** lên `.220` qua `deploy_git.py dashboard`.
2. Verify SSE events flow đúng (browser DevTools → Network → EventStream).
3. Deploy `g2g_scanner_api.py` Step 3 retry fix (từ session 2026-06-10).
4. Push `gege_custom` dev → CI → prod, restart status_sync.
5. bak1 Eldo VNC re-login.
6. `check_all_processes.py` update "9 services".

## Lưu ý / cạm bẫy

- **Deploy qua `deploy_git.py`, KHÔNG SCP trực tiếp** — server `/opt/BotPasteDon`
  là git checkout, SCP tạo drift và `deploy_git.py` sẽ abort.
- **Alpine.js CDN cần internet phía browser** — không phải server. Nếu viewer
  offline → download local.
- **Dashboard đọc DB qua `db._get_conn()` trực tiếp (không qua `threading.Lock`)** —
  pre-existing pattern, không phải regression. Dashboard là process riêng, read-only,
  SQLite WAL handle được. Nhưng nên ghi nhận cho nhất quán.
- **Sync I/O trong async loop** — `open().readlines()` (8 log files) + DB queries
  là blocking, chạy mỗi 1s. Hiện tải nhỏ nên ổn, nhưng nếu nhiều client/log lớn
  thì cân nhắc `run_in_executor`.
- **`onLogScroll` tắt auto-scroll khi cuộn lên nhưng không tự bật lại khi về đáy**
  — phải bấm nút toggle. Chấp nhận được.
