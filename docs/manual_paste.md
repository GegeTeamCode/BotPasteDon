# Manual paste — ERP-triggered, on-demand (2026-06-27)

Cho phép owner **chủ động paste 1 đơn cụ thể** từ ERP, **bỏ qua filter** scanner.
Sinh ra vì filter allow-all (xem [order_filtering.md](order_filtering.md)) vẫn chặn
`Any Gears` / `Any Items - Aspects` / dịch vụ — nhưng đôi khi owner muốn ép 1 đơn
trong nhóm đó (hoặc 1 đơn ngoài cửa sổ scan) vào ERP để trader giao tay.

## Luồng
```
Tab "Đơn G2G/Eldorado" (SPA /create, .100)
  → frappe request_manual_paste(platform, order_id)   [session-authed]
     → POST http://192.168.2.220:<port>/manual-paste {order_id}  + X-Manual-Secret
        → scanner process: handle_manual_paste()
             • fetch order theo external id (KHÔNG gọi check_keywords)
             • map giống scan → send_erp_webhook → ERP .new_order tạo Sell Order
        ← {status: ok|error, order_id, error?}
     → lookup Sell Order theo external_order_id → trả tên đơn về tab
```

## Endpoint (mỗi scanner 1 port)
| Platform | Port (mặc định) | Health |
|----------|------|--------|
| G2G | `MANUAL_PASTE_PORT_G2G` = 8771 | `GET /manual-paste/health` |
| Eldorado | `MANUAL_PASTE_PORT_ELDO` = 8772 | `GET /manual-paste/health` |

`POST /manual-paste` body `{"order_id": "..."}`, header `X-Manual-Secret: <MANUAL_PASTE_SECRET>`.
Trả `200 {status:ok,...}` hoặc `422 {status:error, error: "..."}` (401 nếu sai secret).

External ID = ID hiển thị trên sàn:
- G2G: `1782527850272QETO`. ⚠️ ID hiển thị đã **strip hậu tố item**; API `/order/item/{id}`
  cần `order_item_id` thật = thường `<id>-1`. ID trần → 404 "order_item not found".
  `resolve_order_item_id()` probe get_order_detail cả `<id>` lẫn `<id>-1` (read-only,
  an toàn cho cả đơn đã delivering) → lấy id chuẩn rồi mới extract.
- Eldorado: UUID `8a00381c-...` (`/orders/me/{id}`, không cần resolve).

## Hai trường hợp đều chạy (sau fix 2026-06-27)
- **Đơn mới (preparing)**: resolve id → `start_deliver`+`mark_as_delivering` → đơn chuyển `delivering` → push.
- **Đơn đã delivering sẵn**: resolve id → `start_deliver` no-op (nuốt lỗi) → `get_order_detail` đọc `delivering` → push. (Gate chấp nhận `delivering/delivered/completed`.)

## Khác biệt 2 sàn
- **Eldorado**: fetch thẳng theo order_id. Không đổi trạng thái đơn trên sàn.
- **G2G**: tái dùng `_extract_with_auth_retry` → gọi `start_deliver`+`mark_as_delivering`
  rồi GATE trên `order_item_status` thật. ⚠️ Đơn sẽ chuyển **`delivering`** trên G2G →
  **trader phải claim + giao tay + upload proof**. Nếu đơn chưa thanh toán/chưa
  delivering → trả lỗi "chưa ở trạng thái giao được", không tạo SO.
- **Tên hàng G2G manual paste**: dùng nguyên `offer_title` để giữ đầy đủ mô tả
  listing custom/bulk. Auto scanner vẫn dùng mapping thuộc tính (`Gear - Amulet`,
  currency, material...) để không đổi hành vi inventory hiện tại.

## Idempotency
- ERP `new_order` dedupe theo `external_order_id` → bấm 2 lần không tạo 2 SO (trả `duplicate`, bot coi là ok).
- Bot lưu raw_data đầy đủ + `erp_synced`; nếu ERP momentarily down, `erp_retry_loop` tự đẩy lại.

## Deploy / config
`.env` trên `.220` (gitignored — `deploy_git.py` không đụng):
```
MANUAL_PASTE_SECRET=<random, khớp ERP site_config>
MANUAL_PASTE_PORT_G2G=8771
MANUAL_PASTE_PORT_ELDO=8772
```
Restart 2 scanner (watchdog-safe) để mở listener. Firewall `.220` mở (ufw off,
iptables ACCEPT) nên `.100` gọi thẳng được, không cần mở port.

## Test nhanh (trên .220)
```bash
curl -s -X POST localhost:8771/manual-paste \
  -H "X-Manual-Secret: $SECRET" -H 'Content-Type: application/json' \
  -d '{"order_id":"1782527850272QETO"}'
```
