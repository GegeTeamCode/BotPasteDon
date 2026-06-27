# Order Filtering — vì sao có đơn bot thấy nhưng ERP không có

> Điều tra 2026-06-25. Trả lời câu hỏi: "có đơn nào bot lấy được thông tin nhưng
> trong ERP lại không có?" — Có (~32 đơn g2g), nhưng **toàn bộ là CỐ Ý lọc**,
> không phải rò rỉ/lỗi.

## TL;DR

Scanner **không** đẩy mọi đơn marketplace sang ERP. Mỗi đơn phải lọt một bộ
**blacklist → whitelist** (theo `title`) thì mới được `start_deliver → webhook → ERP`.
Đơn không lọt chỉ bị ghi `status='DETECTED'` trong SQLite làm dấu "đã thấy" (để
khỏi quét lại), **rồi bỏ qua** — không deliver, không webhook, không vào ERP.

Whitelist = chỉ hàng bot **tự giao được** (currency / gold / runes / gems …).
Gear / unique / account / boosting là **giao tay, ngoài phạm vi bot** → không vào
ERP là đúng thiết kế.

> **⚠️ Thay đổi 2026-06-27:** filter bị **đảo ngược** sang **allow-all** (whitelist
> rỗng). Giờ **mọi đơn** đều được paste sang ERP, **trừ** các listing bulk/placeholder
> mà worker không fulfil được (`Any Gears`, `Any Items - Aspects`) + dịch vụ rác
> (`Boosting/Leveling/Account/Custom oder`). Mục đích: gear cụ thể (Mageblood,
> Headhunter, Temporalis…) giờ vào ERP để trader giao tay. Xem chi tiết ở cuối file
> ("Phương thức filter mới — allow-all").

## Cơ chế lọc (vị trí trong code)

Lọc xảy ra ở **scanner**, KHÔNG phải ở webhook matching.

### 1. `scanners/g2g_scanner_api.py::scan_order_list` (~dòng 72-86)

```python
title = order.get("title", "") or order.get("item_name", "") or order.get("offer_title", "")
unit_name = (order.get("unit_name") or "").lower()
is_gold = "gold" in unit_name
if not is_gold and not check_keywords(title, self.config):
    self.db.insert_order("g2g", order_id, {"itemName": title})   # chỉ ghi DETECTED
    continue                                                       # BỎ: không deliver/webhook/ERP
result.append({ ... })   # chỉ đơn LỌT mới đi tiếp
```

- Đơn bị bỏ chỉ lưu `{"itemName": title}` → **không** qua `_map_order_data` →
  field `game` rỗng (`''`). Đây là dấu hiệu nhận biết đơn bị filter trong DB.
- `is_gold` là ngoại lệ: đơn Gold luôn lọt (vì G2G để `service_keyword="Game coins"`
  cho mọi đơn D4, không phân biệt Gold — phải dò qua `unit_name`).

### 2. `scanners/base_scanner.py::check_keywords` (dòng 40)

```python
def check_keywords(text: str, config: dict) -> bool:
    if not config:
        return True
    lower_text = (text or "").lower()
    if config.get("blacklist"):                       # 1) blacklist trước
        blacklist = [k.strip().lower() for k in config["blacklist"].split(",") if k.strip()]
        if any(k in lower_text for k in blacklist):
            return False                              #    trúng → BỎ
    if config.get("whitelist"):                       # 2) whitelist sau
        whitelist = [k.strip().lower() for k in config["whitelist"].split(",") if k.strip()]
        if whitelist and not any(k in lower_text for k in whitelist):
            return False                              #    không trúng từ nào → BỎ
    return True
```

Thứ tự: **blacklist trước, whitelist sau**. Khớp theo *substring* (không phân biệt hoa thường).

### 3. Config — `.env` trên server (.220)

**Hiện tại (từ 2026-06-27 — allow-all):**

| Biến | Giá trị hiện tại |
|------|------------------|
| `SCANNER_WHITELIST` | *(rỗng — allow-all)* |
| `SCANNER_BLACKLIST` | `Any Gears, Any Items - Aspects, Boosting, Leveling, Account, Custom oder` |

> Trước 2026-06-27 whitelist = `Divine Orb, Chaos Orb, ..., Items` và blacklist
> thêm `Any Items` (chỉ cho phép currency/gold/gems, drop toàn bộ gear). Các đơn
> DETECTED của đợt đó (vd `1782433314325QBNQ` — Mageblood) đã được xử lý riêng,
> không cần re-paste.

Nạp ở `shared/config.py` → `SCANNER_CONFIG["whitelist"]` / `["blacklist"]`
(`os.getenv("SCANNER_WHITELIST" / "SCANNER_BLACKLIST")`).

## Phân loại 32 đơn DETECTED đang kẹt (đối chiếu thực tế, 0 anomaly)

| Lý do bị bỏ | Số đơn | Cố ý? | Ví dụ `title` |
|---|---|---|---|
| Rớt whitelist | 27 | ✅ | `【Any Gears】【Unique - Rare】…`, `Headhunter Heavy Belt…`, `Mageblood Utility Belt…`, `Temporalis Silk Robe…` |
| Dính blacklist (`Any Items`) | 5 | ✅ | `【SS13】【Any Items - Aspects】…` |
| **Tổng** | **32** | tất cả đúng thiết kế | — |

Chạy `check_keywords` ngược trên 32 đơn: số đơn `check_keywords=True` nhưng vẫn kẹt = **0**
→ không có đơn nào bị filter nhầm.

## Vòng đời đơn bị filter trong DB

1. Scanner thấy đơn → `check_keywords` fail → `insert_order` status `DETECTED`, `erp_synced=0`.
2. **Không** vào ERP retry: `get_unsynced_orders` loại `status IN ('DETECTED','FAILED')`
   (`shared/database.py`) → không bao giờ retry (đúng — đây không phải đơn cần sync).
3. **Purge sau 24h**: DETECTED quá 24h bị `DELETE` (`shared/database.py`) → biến mất.
   Nên số DETECTED trong DB chỉ là cửa sổ 24h gần nhất; thực tế mỗi ngày có ~30 đơn
   loại này (gear/unique giao tay), tất cả đúng là **không vào ERP**.

## ⚠️ Đính chính một giả thuyết sai trước đó

Điều tra lần trước kết luận "đơn kẹt vì `match_webhook` fail do `game=''`" → **SAI**.
`WEBHOOK_DEFAULT` trong `.env` CÓ được set → `match_webhook` (`shared/discord_utils.py:52`)
luôn có `default` catch-all → **không bao giờ** trả `None`. Webhook không phải lý do.
Lý do thật là FILTER ở `scan_order_list` (mô tả trên).

## Phần thật sự là lỗi (KHÔNG cố ý) — backlog

1. **2 đơn false-sync 2026-05-31**: `1780247867490QY7W`, `1780248262905AP02` —
   status `THREAD_CREATED`, `erp_synced=1` (bot tưởng đã sync) nhưng ERP **không có**
   dưới mọi dạng id. Edge case cũ, ưu tiên thấp. Cần xác minh & tạo tay trong ERP nếu là đơn thật.
2. **Rủi ro orphan (hiện 0 đơn)**: đơn LỌT whitelist nhưng `get_order_detail` fail 3 lần
   → kẹt `delivering` trên G2G, **không** vào DB (`g2g_scanner_api.py:222`, log
   `"manual recovery needed"`). Nếu xảy ra phải khôi phục tay.

## Lưu ý khi chỉnh `SCANNER_WHITELIST` / `SCANNER_BLACKLIST`

- Khớp **substring** → từ khóa rộng dễ bắt nhầm. Vd `Items` (whitelist) khớp cả
  `Any Items` — nhưng `Any Items` nằm blacklist và **blacklist chạy trước** nên vẫn bị loại.
  Cân nhắc thứ tự ưu tiên khi thêm từ.
- Thêm một loại hàng vào whitelist = bot sẽ **tự cố giao** đơn đó. Chỉ thêm khi worker
  thực sự giao được loại hàng đó, nếu không sẽ tạo đơn ERP rồi kẹt ở khâu giao.
- Đổi `.env` xong phải restart scanner (`deploy_git.py` hoặc restart service) để nạp lại.

## Phương thức filter mới — allow-all (2026-06-27)

Từ 2026-06-27 filter bị **đảo**: thay vì "chỉ cho phép whitelist" thì giờ là
**"chặn blacklist, cho phép tất cả còn lại"**.

| Mode | `SCANNER_WHITELIST` | `SCANNER_BLACKLIST` | Hành vi |
|------|---------------------|---------------------|---------|
| **Cũ** (trước 06-27) | `Divine Orb, ..., Items` | `Boosting, ..., Any Items` | Chỉ currency/gold/gems/runes lọt; **toàn bộ gear bị drop** (Mageblood, Headhunter…). |
| **Mới** (hiện tại) | *(rỗng)* | `Any Gears, Any Items - Aspects, Boosting, Leveling, Account, Custom oder` | Mọi đơn lọt, trừ listing bulk gear (`Any Gears`, `Any Items - Aspects`) + dịch vụ rác. |

**Tại sao:** owner muốn gear cụ thể (Mageblood Utility Belt, Headhunter, Temporalis…)
vào ERP để trader **giao tay**. Hai pattern bị chặn vì là "any/bulk" listing mà worker
không fulfil được — nếu cho lọt sẽ tạo ERP Sell Order rồi kẹt (không có item cụ thể).

**Verify** (script `scripts/_verify_filter.py`, chạy trên `.220`): 10/10 case đúng —
`Any Gears`/`Any Items - Aspects`/`Boosting`/`Account` DROP; Mageblood/Headhunter/
Temporalis/Widow's Web/Divine/Gold PASTE.

> ⚠️ **Tác động G2G**: mọi đơn qua filter sẽ bị scanner gọi `start_deliver` +
> `mark_as_delivering` (để lấy delivery info) → đơn gear chuyển sang `delivering`
> trên G2G và phải được trader claim+giao tay. Eldorado **không** start_deliver
> (chỉ get detail) nên an toàn hơn cho gear.

**Quay lại mode cũ:** set `SCANNER_WHITELIST=Divine Orb, ...` + giữ blacklist, restart
scanner. Whitelist rỗng = allow-all (xem [`check_keywords`](../scanners/base_scanner.py)).
