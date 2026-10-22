# Plan — Đơn Eldorado Aion 2 Kinah vào ERP .100 đúng như đơn G2G

> Lập 2026-10-23. Chủ giao 23/10: "giải quyết vấn đề botpastedon paste sai về erp .100 luôn đi".
> Plan cũ ở file này (chat G2G/Eldorado, `9c61063`) nằm trong git history.

## Bối cảnh (đo 23/10)

- 22–23/10 mở 104 offer Aion 2 Kinah trên Eldorado (gameId 360, category Currency; server là
  tradeEnvironment 3 cấp Region → Faction → Server; `unitSystem` `Unit1000000`). Xem
  `docs/marketplace_offers.md`.
- Đơn đầu tiên `A11F9438-8AB7-40A9-CEF8-08DF302C7ECE` (Europe / Asmodian / Zikel, 10 × 0,76699 USD,
  tổng 7,67) vào .100 thành `SO-261023-R1CG2IBY`: `game="Custom Request"` (mục cuối của gameId 360
  trong `/library`) → `game_context` NULL; `server="Europe"`; `itemName="Kinah"`; số lượng
  10.000.000 × 0,000001. Đơn đã bị seller huỷ trên Eldorado ("Out_Of_Stock"), ERP Cancelled theo
  sync — không cần dọn.
- Đơn G2G Aion 2 trên .100 (235 đơn): context `Aion 2 - Global` (context duy nhất của game),
  `order_item_title` = nhãn server `"Zikel - Asmodian [NA East]"`, số lượng theo Mil Kinah
  (1 = 1M), đơn giá USD/1M, không gắn Currency Item (đúng luồng hiện nay: Aion 2 giao ngoài ERP).
- .100 chạy mã ERP cũ hơn `main` rất xa → không sửa ERP; sửa bên bot cho đơn Eldorado mang đúng
  khuôn đơn G2G.

## Goal

Đơn Eldorado Aion 2 Kinah gửi ERP: `game="Aion 2"`, `server` = `itemName` = `"<Server> - <Phe> [<Vùng>]"`
(Europe → EU, như G2G), `quantity` theo Mil Kinah, `unit_price` USD/1M (tổng không đổi).

## Allowed files

- `scanners/eldorado_scanner_api.py`
- `tests/test_eldo_order_map.py` (mới, chạy offline trên mẫu đơn thật)
- `docs/architecture.md` / `docs/marketplace_offers.md` (doc-sync), `.ai/task-log.md`

## Do-not-touch

`auth/*`, `status_sync/*`, `scanners/main.py`, `shared/database.py`, `.env` trên .220, `data/orders.db`,
ERP .100/.102.

## Steps

1. `ELDO_GAME_ID_TITLES["360"] = "Aion 2"`.
2. Nhãn server cho game env nhiều cấp (bảng theo gameId; hiện chỉ 360): ghép
   `"{Server} - {Faction} [{Region}]"`, map vùng Europe → EU; dùng cho cả `server` và `itemName`.
   Game khác giữ nguyên cách cũ (env đầu tiên).
3. Đơn vị Mil cho (gameId 360, Currency): cùng nhánh quy đổi với Gold (ERP đếm theo 1M).
4. Test: 15 đơn Eldorado thật (PoE, PoE2, D4 Gold, D4 item, đơn Aion 2) — đơn không phải Aion 2
   cho kết quả `_map_order_data` y hệt mã cũ; đơn Aion 2 ra đúng khuôn.
5. Commit → push `main` → `scripts/deploy_git.py scanner_eldo` (KHÔNG restart auth).

## Acceptance

- Test xanh; diff kết quả 14 đơn không phải Aion 2 = 0.
- Sau deploy: scanner Eldorado chạy, không lỗi; auth không bị restart.
- Đơn Eldorado Aion 2 kế tiếp vào .100 với context `Aion 2 - Global`, tiêu đề nhãn server, số lượng Mil.

## Risks

- Restart scanner Eldorado khi đang có đơn Paid: scanner idempotent theo `orders.db`, đơn đã xử lý
  không bị đẩy lại.
- Vùng Latin America: G2G không có nhãn tương ứng → giữ "Latin America".
