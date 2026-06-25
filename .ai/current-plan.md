# Plan — status_sync: push cancel-request / dispute alert + phân loại report_case

Nhánh: `main` (single-branch, commit thẳng). **Cấu phần BOT** của cụm "G2G cancel/resolution → ERP".
Plan master (toàn cảnh + quyết định nghiệp vụ): `frappe-erp15-gegecurrency/.ai/current-plan.md`.

## Goal
Cung cấp cho ERP **tín hiệu cảnh báo** khi khách mở Resolution trên G2G (đơn chưa completed,
vẫn giao được), phân loại đúng **cancel request** vs **dispute**, và push cả lúc mở lẫn lúc đóng
để ERP clear cảnh báo. Lấy `report_case` của G2G case làm chân lý phân loại.

**Bối cảnh (dữ liệu thật DB `.220`):**
- Resolution/khiếu nại nằm ở `list_my_cases` → bảng `marketplace_disputes` (đã có sẵn `report_case`,
  `report_reason`, `case_status`). `marketplace_status` KHÔNG hề có `cancellation_requested`/`disputed`.
- `report_case`: `cancel` (239 ca, = yêu cầu hủy) · `did_not_receive` (7, = dispute).
- Hiện `_sync_cases` map MỌI case → `disputed` và **chỉ push khi `status=='open'`** → đa số ca đã
  `close` lúc sync nên trượt → ERP không bao giờ thấy cảnh báo.

## Fix (tasks)
| ID | Task | Chi tiết |
|----|------|----------|
| **B-1** | Phân loại trong `_sync_cases` | `report_case=cancel` → `marketplace_state="cancel_requested"`; còn lại (`did_not_receive`…) → `"disputed"`. Kèm `alert: True/False`. |
| **B-2** | Push ON + OFF | Bỏ điều kiện chỉ-push-khi-`open`. Push `alert=True` khi case vào `open/escalate`; push `alert=False` (clear) khi case `close`. Idempotent: chỉ push khi `(case_status, report_case)` đổi so bản ghi cũ (cột đã có trong `marketplace_disputes`). |
| **B-3** | Payload | Thêm `report_case`, `report_reason`, `case_status` để ERP hiển thị reason. Giữ `external_order_id = order_id`. |
| **B-4** | Reconcile cases | Đẩy lại alert chưa push thành công (mark `notified_pushed_at`), tương tự `reconcile_unpushed`. |
| **B-5** | Doc-sync | `.ai/architecture.md` + `docs/architecture.md` (state map `cancel_requested`/`disputed`) + `.ai/decisions.md` (ghi quyết định 2026-06-26). |

## Allowed files
- `status_sync/g2g_sync.py` — `_sync_cases` (B-1,2,3) + reconcile cases (B-4).
- `status_sync/erp_client.py` — KHÔNG cần đổi (payload generic).
- `shared/database.py` — chỉ thêm helper đọc case chưa-push nếu cần cho B-4 (không đổi schema).
- `docs/architecture.md`, `.ai/architecture.md`, `.ai/decisions.md` (B-5).

## Do not touch
- ERP-side mapping/logic (terminal cancelled→Cancelled/Refunded + đảo ví) — làm ở repo ERP.
- Luồng scanner/worker/coordinator/auth, `TRACKED_STATES=(completed,cancelled)` cho luồng terminal.
- `.env`, secrets, `data/orders.db` rows (chỉ đọc + mark qua hàm có sẵn).

## Acceptance
- `py_compile` sạch.
- Deploy: `python scripts/deploy_git.py status_sync` (SAU khi ERP đã nhận state `cancel_requested`/`disputed`).
- Log "cases: pushed alert ON/OFF N" hợp lý; ERP trả `alert_set`/`alert_cleared`.
- Đơn `In Delivery` không bị đụng (ERP chặn sẵn).

## Thứ tự (phụ thuộc ERP)
ERP deploy field + webhook TRƯỚC → bot deploy status_sync → FE banner → backfill. Nếu bot push state
mới khi ERP chưa nhận → ERP trả `ignored` (an toàn, không vỡ).

## Risks
- Sync 30m → cảnh báo trễ tối đa 30 phút (chấp nhận).
- Case `escalate` (G2G nâng cấp tranh chấp) hiếm → gộp vào nhánh alert `disputed`, không xử lý riêng (để sau).
