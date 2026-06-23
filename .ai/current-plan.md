# Plan — status_sync: reconcile orphaned terminal pushes

Nhánh: `feat/status-sync-reconcile-unpushed` (từ `main`).

## Goal
Chặn tái diễn bug: đơn marketplace đã `completed` nhưng **không bao giờ push** sang ERP
→ ERP kẹt Delivered → ví không cộng. Gốc: backfill ghi state với `push=False` +
incremental chỉ push khi state đổi (`prev != mp_state`) + count-diff gate bỏ sót.
Đơn completed lúc/trước backfill → mồ côi vĩnh viễn.

(Backlog eldo đã remediate tay 2026-06-23: 610 đơn → ERP Completed, ví +$12,080.37.)

## Fix
Mỗi cycle, reconcile các đơn terminal `last_pushed_at IS NULL` đẩy lại ERP — **chặn theo
ngày `createdDate >= ERP_GO_LIVE` (2026-05-29)** để KHÔNG đụng ~9,761 đơn eldo pre-ERP
(ERP chưa từng có → push chỉ ra no_so vô nghĩa). Idempotent: ERP `status_update` trả
no_change/protected; `_credit_marketplace_wallet` skip nếu đã có ALE In → an toàn lặp.

## Allowed files
- `shared/database.py` — thêm `get_unpushed_marketplace(...)` (lọc ngày qua json_extract).
- `status_sync/reconcile.py` (mới) — `reconcile_unpushed(db, erp, platform, states, ...)`.
- `status_sync/eldo_sync.py` — gọi reconcile đầu `run_once` (độc lập auth/API marketplace).
- `status_sync/g2g_sync.py` — tương tự (date field `created_at` epoch ms).
- `shared/config.py` — `ERP_GO_LIVE_ISO` + `ERP_GO_LIVE_MS`.
- `scripts/deploy_git.py` — thêm `status_sync` vào SERVICES + ALL_ORDER (hiện THIẾU).

## Do not touch
- `_credit_marketplace_wallet` / ERP code (đã đúng, idempotent).
- `.env`, secrets, `data/orders.db` rows (chỉ đọc + mark_pushed qua hàm có sẵn).
- Luồng auth/scanner/worker/coordinator.

## Acceptance
- `py_compile` sạch mọi file sửa.
- Deploy: `deploy_git.py status_sync` (stop watchdog → restart → start watchdog).
- Cycle log "reconcile: pushed N/M" — N≈0 ngay sau (backlog đã remediate), >0 chỉ khi có mồ côi mới.
- Không đụng 9,761 đơn pre-ERP (date bound).

## Risks
- Reconcile đẩy nhầm pre-ERP → date bound `createdDate>=2026-05-29` chặn.
- Double-credit → ERP idempotent (ALE In guard).
- Restart status_sync xung đột watchdog → dùng deploy_git.py (watchdog-aware).

## Out of scope (đã báo user)
- **G2G backlog 1000 đơn kẹt** (724 Queued, 260 Delivered) — bot KHÔNG track các đơn cũ
  này (chỉ có 1532 đơn `1781xxx` gần đây; cross-ref khớp 0). Cần điều tra g2g API + luồng
  Queued riêng. Code fix này chỉ chặn mồ côi đơn MỚI cho g2g.
