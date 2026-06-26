# Plan — Fix lỗ hổng G2G reconcile (BOT: ERP-driven per-order lookup)

Nhánh: `main` (single-branch). Cấu phần BOT của cụm "G2G ERP-driven reconcile".
Plan master (vấn đề + scope + phần ERP): `frappe-erp15-gegecurrency/.ai/current-plan.md`.

## Vấn đề
`status_sync` G2G chỉ thấy ≤100 đơn mới nhất/status qua `list_my_order` → đơn cũ completed/cancelled
KHÔNG bao giờ fetch → ERP kẹt non-terminal. `reconcile_unpushed` cũ là bot-driven (đẩy đơn bot ĐÃ có)
→ vô dụng. Prod: **G2G 578 đơn** kẹt (486 Delivered, 89 Queued, 3 Claimed).

## Giải pháp
ERP trả danh sách đơn non-terminal của nó → bot lookup từng đơn `get_order_detail` lấy
`order_item_status` thật → push terminal qua `status_update`. ERP stateless; bot giữ throttle/back-off/batch.

## Tasks
| ID | Task | Chi tiết |
|----|------|----------|
| **B-1** | `ERPClient.get_pending_orders(platform, limit)` | GET `ERP_PENDING_ORDERS_URL` (derive từ ERP_WEBHOOK_URL: `.new_order`→`.get_pending_marketplace_orders`), header `X-API-Key`=ERP_API_KEY_G2G. Trả list `external_order_id`. 4xx→[] + log; timeout/5xx→[] (thử lại cycle sau). |
| **B-2** | `status_sync/erp_reconcile.py::reconcile_from_erp(db, erp, api, auth, platform, batch, throttle, backoff_h)` | Lấy pending từ ERP. Lọc bỏ đơn đã check gần đây còn non-terminal (`marketplace_status.last_synced_at` < now-backoff). Per-order (cap `batch`): `api.get_order_detail(ext+"-1", auth)` → `order_item_status`: `completed`→push `completed`; `cancelled`/`refunded`→push `cancelled`; `delivering`/`preparing`/khác→ chỉ upsert_marketplace_status (back-off, KHÔNG push). Mỗi lookup `sleep(throttle)`. `RateLimitError`→dừng batch run này (resume cycle sau). Trả `(completed, cancelled, skipped)`. |
| **B-3** | Cadence trong `g2g_sync.run_once` | Đếm cycle; chạy `reconcile_from_erp` mỗi `ERP_RECONCILE_EVERY_N_CYCLES` cycle (sau bước cases). Cần auth (đã có ở run_once). |
| **B-4** | `shared/database.py` | `get_marketplace_status(platform, order_id)` (getter, nếu chưa có) để đọc last_synced_at + state cho back-off. (upsert_marketplace_status đã set last_synced_at.) |
| **B-5** | `shared/config.py` | `ERP_PENDING_ORDERS_URL` (derive), `ERP_RECONCILE_EVERY_N_CYCLES` (default 4 = ~2h), `ERP_RECONCILE_BATCH` (default 150), `ERP_RECONCILE_THROTTLE_SEC` (default 0.4), `ERP_RECONCILE_BACKOFF_H` (default 12). |
| **B-6** | Test | `reconcile_from_erp` với fake api (get_order_detail trả completed/cancelled/delivering) + fake erp (capture push) + db tạm → assert push đúng + back-off skip. py_compile. |
| **B-7** | Doc-sync | `docs/architecture.md` (luồng reconcile ERP-driven) + `.ai/decisions.md`. |

## Allowed files
- `status_sync/erp_client.py` (B-1), `status_sync/erp_reconcile.py` (mới, B-2),
  `status_sync/g2g_sync.py` (B-3 cadence), `shared/database.py` (B-4 getter),
  `shared/config.py` (B-5), `docs/`+`.ai/` (B-7), `tests/` (B-6).

## Do not touch
- `status_update` ERP (đã idempotent). `reconcile_unpushed` cũ (giữ — vẫn hữu ích cho eldo/đơn bot có local).
- Luồng scanner/worker/auth. `.env`/secrets.

## Idempotency & an toàn
- `status_update` no_change/protected/manual_required + credit/reverse guard.
- In Delivery KHÔNG có trong list ERP + bị chặn ở status_update (2 lớp).
- Back-off tránh re-hammer 486 Delivered (đa số đang delivering thật) mỗi run.
- Throttle + batch + RateLimitError → không 429 storm (5k lookup từng ~1.5h; 578 ~10min nếu gộp → chia batch).

## Acceptance
- py_compile + test xanh.
- Deploy SAU ERP. `deploy_git.py status_sync`. Log "erp_reconcile: completed=X cancelled=Y skip=Z" giảm dần backlog.
- Tùy chọn `--once` kick ngay; theo dõi G2G non-terminal 578 → giảm.

## Thứ tự (phụ thuộc ERP)
ERP deploy endpoint TRƯỚC → bot deploy. Nếu bot gọi khi ERP chưa có endpoint → 404→[] (an toàn, no-op).
