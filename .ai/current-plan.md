# Plan — Chat G2G/Eldorado: vá các chỗ hở ở `docs/marketplace_chat.md` mục 6

> Lập 2026-10-22. Chủ giao 22/10: "giải quyết các phần còn hở … lưu ý các bẫy tránh làm mất
> phiên đăng nhập". Số đo làm căn cứ: `docs/marketplace_chat.md` mục 2.1, 2.3, 2.6 (đo 22/10).
> Plan cũ ở file này (Seller API, `bf12ea7`) nằm trong git history.

## Goal

1. G2G: kiểm phản hồi Sendbird; Session-Key thiếu/sai/thu hồi (mã 4003xx, 400401) → ném lỗi riêng
   và bắn `OPS ALERT [g2g-sendbird-key]`.
2. G2G: `POST chat/channel` lỗi (500/502/timeout/401) hoặc không trả `channel_url` → tìm kênh có sẵn
   qua Sendbird `members_exactly_in` (chỉ Session-Key). Bỏ tên kênh tự dựng `g2g_dm_{buyer}_{seller}`
   (sai 17/50 kênh).
3. Logger `g2g.api`, `eldo.api` có handler (`setup_logger`) để dòng INFO vào log.
4. Chat hỏng được gửi lại (cả hai sàn): đơn vẫn `COMPLETED` (hàng đã giao), gắn
   `error_message = "CHAT_PENDING:…"` + `retry_data = {"chat": {...}}`; vòng hồi phục sẵn có của mỗi
   worker gửi lại **riêng lời nhắn**, giãn 1 → 5 → 30 → 60 phút, tối đa 5 lượt rồi `CHAT_STUCK:` +
   ops alert. Lần gửi lại G2G dùng lại `buyer_id`/`channel_url` đã biết, không gọi G2G nữa.
5. Xoá `full_delivery` chết ở `shared/g2g_api.py` và `shared/eldo_api.py` (không ai gọi; bản Eldo gọi
   hàm không tồn tại `send_text_message_rest`).
6. Sửa tài liệu sai: `seller_api_migration_plan.md` bảng 2.3 ("lùi sang REST"), `proof_mechanism.md`
   mục 3 (bằng chứng Eldorado "non-fatal" — cũ từ `533b305`).

## Allowed files

- `shared/g2g_api.py`, `shared/eldo_api.py`
- `shared/database.py` — chỉ thêm hàm đọc `get_orders_by_error_prefix`, KHÔNG đổi schema
- `workers/base_worker.py` — helper `CHAT_PENDING` dùng chung
- `workers/g2g_worker.py`, `workers/eldorado_worker.py`
- `tests/test_marketplace_chat.py` (mới)
- `docs/marketplace_chat.md`, `docs/proof_mechanism.md`, `docs/seller_api_migration_plan.md`,
  `docs/architecture.md` (nếu cần) — doc-sync
- `.ai/current-plan.md`, `.ai/task-log.md`

## Do not touch

- `auth/main.py`, `shared/g2g_auth.py`, `shared/eldo_auth.py` — mọi thứ liên quan phiên đăng nhập.
- `.env` trên server, `chrome_profile_*`, hàng có sẵn trong `data/orders.db`.
- `scanners/`, `status_sync/`, payload ERP, các bước qty/proof/deliver.
- Hành vi `call_with_retry` của G2G: gặp `AuthError` thì invalidate và KHÔNG gọi lại ngay.

## Steps

1. `shared/g2g_api.py`: `SendbirdError`, `SendbirdKeyError`, `ChatError(progress)`; `_parse_sendbird`;
   `send_chat_message` kiểm `message_id`; `find_chat_channel`; `create_chat_channel` trả `""` khi
   thiếu; `_send_chat(progress=…)` (lỗi gì cũng gói thành `ChatError`, gồm `AuthError` — không thử
   lại ngay); `setup_logger`; xoá `full_delivery`.
2. `shared/eldo_api.py`: `setup_logger`; xoá `full_delivery`.
3. `shared/database.py`: `get_orders_by_error_prefix(platform, status, prefix)`.
4. `workers/base_worker.py`: `read_message`, `mark_chat_pending`, `due_chat_retries`,
   `complete_order` (xoá dấu `CHAT_*` cũ khi đơn hoàn tất lại).
5. `workers/g2g_worker.py`: bước chat bỏ vòng thử lại ngay sau lỗi auth (nguy cơ gọi G2G bằng JWT
   chết → G2G đá phiên); lỗi → `CHAT_PENDING`; vòng `recover_pending_retries` quét thêm chat.
6. `workers/eldorado_worker.py`: `handle_eldo_api` trả thêm lỗi chat; `CHAT_PENDING`; vòng
   `recover_auth_failed` quét thêm chat.
7. Test cô lập (Sendbird/G2G/TalkJS giả, DB tạm): `tests/test_marketplace_chat.py`.
8. Doc-sync + `.ai/task-log.md`, commit, push `main`.
9. Deploy CHỈ `worker_g2g worker_eldo` (xem Risks).

## Acceptance

- Test mới xanh; `tests/test_talkjs.py` vẫn xanh; `py_compile` mọi file sửa.
- Sau deploy: hai worker lên, 1 PID mỗi cổng 8001/8002; `auth.main` giữ nguyên PID và uptime; đơn
  G2G kế tiếp có dòng `Chat sent to buyer` trong `/tmp/g2g_worker.log`.

## Risks

- **Mất phiên đăng nhập** — chỉ xảy ra nếu `auth.main` bị restart (G2G: token xoay chỉ trong RAM;
  Eldorado: đường lùi cookie chỉ còn `bak1`). Plan không restart auth. Bẫy: `deploy_git.py` chạy
  `systemctl stop bot-watchdog.service`, giết mọi tiến trình trong cgroup watchdog — nếu `auth.main`
  từng được watchdog dựng lại thì nó nằm trong cgroup đó và chết theo. **Kiểm cgroup trước**; auth
  nằm trong đó thì KHÔNG dùng `deploy_git.py`.
- Restart worker giữa lúc đang giao → đơn kẹt `DELIVERING`. Deploy lúc không có task đang chạy (dòng
  `Processing:`/`Task received` cuối cùng đã có `Completed:` tương ứng).
- Gửi lại chat có thể trùng tin nếu Sendbird/TalkJS đã nhận nhưng trả lời bị mất (timeout). Chấp nhận:
  thừa một lời cảm ơn tốt hơn thiếu.
- Scanner/status_sync cũng import `g2g_api`/`eldo_api` nhưng KHÔNG restart lần này; đổi logger có hiệu
  lực với chúng ở lần restart sau.
