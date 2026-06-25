# Known Issues & Fix Plan — BotPasteDon

> Tổng hợp 2026-06-25 từ 2 đợt điều tra (`order_filtering.md`, `database.md`).
> Mỗi mục: mô tả · bằng chứng · mức độ · plan fix. Đánh dấu ✅ khi đã xử lý.

Mức độ: **P1** = có thể gãy production / mất dữ liệu · **P2** = nợ kỹ thuật cần dọn ·
**P3** = tồn đọng nhỏ / quyết định thủ công.

---

## [P1] #1 — Schema drift: `orders.erp_synced` & `erp_retry_count` thiếu trong code khởi tạo — ✅ ĐÃ FIX (commit `33c1bd4`, 2026-06-26: ALTER loop idempotent)

**Mô tả.** 2 cột này đang chạy trong DB production nhưng **không có** trong
`_init_db()` `CREATE TABLE orders`, cũng không có `ALTER TABLE … ADD COLUMN` nào trong
code (chỉ `retry_data` có ALTER ở `database.py:121`). Tức 2 cột được thêm **thủ công**
trên server.

**Bằng chứng.** `PRAGMA table_info(orders)` prod có `erp_synced`, `erp_retry_count`;
grep toàn repo: chỉ `retry_data` được ALTER; `erp_synced`/`erp_retry_count` chỉ xuất hiện
trong câu UPDATE/SELECT (`mark_erp_synced`, `claim_erp_order`, `increment_erp_retry`,
`get_unsynced_orders`). Các bảng khác KHỚP code (không drift).

**Rủi ro.** Deploy lên DB mới / xoá DB tạo lại → thiếu 2 cột → `mark_erp_synced`,
`claim_erp_order`, `increment_erp_retry` **crash** `sqlite3.OperationalError: no such
column: erp_synced`. Scanner đứng đường đẩy ERP.

**Plan fix (code, idempotent).** Trong `shared/database.py::_init_db`, thay block ALTER
đơn lẻ `retry_data` bằng vòng lặp migration:
```python
for col, ddl in (
    ("retry_data", "TEXT"),
    ("erp_synced", "INTEGER DEFAULT 0"),
    ("erp_retry_count", "INTEGER DEFAULT 0"),
):
    try:
        conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")
        conn.commit()
    except Exception:
        pass  # cột đã tồn tại → no-op
```
- Trên prod (cột đã có) → no-op, **không đụng dữ liệu**.
- Trên DB mới → tạo đủ cột.
- Rủi ro: thấp. Deploy git như thường (`deploy_git.py`).

---

## [P2] #2 — `DATABASE_PATH` tương đối → sinh file `orders.db` rỗng lạc chỗ — ✅ ĐÃ FIX (commit `33c1bd4` resolve theo root + xoá 2 file rỗng)

**Mô tả.** `.env` đặt `DATABASE_PATH=data/orders.db` (tương đối). Process chạy sai cwd
sẽ tạo `orders.db` rỗng ở chỗ khác.

**Bằng chứng.** `/opt/BotPasteDon/orders.db` (0 B, 31/05) và
`/opt/BotPasteDon/scanners/orders.db` (0 B, 31/05). `config.py:61`
`DATABASE_PATH = os.getenv("DATABASE_PATH", "data/orders.db")`, không có BASE_DIR.

**Rủi ro.** Vô hại về dữ liệu (prod luôn cwd `/opt/BotPasteDon`), nhưng dễ nhầm + nguy cơ
một process tương lai ghi nhầm vào DB rỗng.

**Plan fix (code, robust).** Trong `shared/config.py`, resolve path tương đối theo project
root thay vì cwd:
```python
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_dbp = os.getenv("DATABASE_PATH", "data/orders.db")
DATABASE_PATH = _dbp if os.path.isabs(_dbp) else str(_PROJECT_ROOT / _dbp)
```
Sau deploy: xoá 2 file rỗng (mục #3). Không cần đổi `.env`.

---

## [P3] #3 — Rác trong/ngoài DB: bảng `test_t` + 2 file `orders.db` rỗng — ✅ ĐÃ DỌN (2026-06-26: drop test_t + rm 2 file rỗng)

**Mô tả.** Bảng `test_t (id)` rỗng trong `orders.db` (sót từ test). 2 file `orders.db`
rỗng ở mục #2.

**Plan fix (one-off server, sau khi #2 deploy).**
```sql
DROP TABLE IF EXISTS test_t;   -- chạy qua venv python trên server
```
```bash
rm -f /opt/BotPasteDon/orders.db /opt/BotPasteDon/scanners/orders.db
```

---

## [P3] #4 — 2 đơn false-sync (THREAD_CREATED, erp_synced=1 nhưng ERP không có)

**Mô tả.** Bot ghi `erp_synced=1` (tưởng đã sync) nhưng ERP không có đơn dưới mọi dạng id.

**Bằng chứng.** `1780247867490QY7W`, `1780248262905AP02` — cùng item PoE2
`•Goldrim Felt Cap•Custom Stats - DM Me Before Order`, khách `corvenos`, `total_price=1.00`
(earning 0.95), tạo 2026-05-31 (sát go-live ERP 29/05). Đều `THREAD_CREATED`.

**Phán đoán nguyên nhân.** Giai đoạn đầu go-live: ERP có thể trả 200/duplicate, hoặc SO
bị xoá/huỷ sau đó. Giá trị rất nhỏ ($0.95/đơn).

**Plan fix (thủ công, không code).** Xác minh lần cuối trên ERP → tạo tay 2 Sell Order nếu
là đơn thật, hoặc chấp nhận bỏ qua (giá trị không đáng kể). **Cần quyết định của chủ.**

---

## [P2/Design] #5 — Orphan risk: đơn lọt filter nhưng `get_order_detail` fail → kẹt "delivering" trên G2G, không vào DB

**Mô tả.** `g2g_scanner_api.py::_do_extract`: sau khi `start_deliver` + `mark_as_delivering`
(đã đổi state trên G2G), nếu `get_order_detail` fail cả 3 lần → return None, **không**
insert vào DB. Đơn bị khoá `delivering` trên marketplace nhưng bot mất dấu hoàn toàn —
chỉ còn log `"manual recovery needed"` (g2g_scanner_api.py:222).

**Bằng chứng.** Hiện **0 đơn** dính (không có đơn nào trong tình trạng này lúc điều tra),
nhưng là lỗ hổng tiềm tàng theo thiết kế.

**Plan fix (design, ưu tiên sau #1-#3).** Khi `_do_extract` bỏ cuộc: insert tối thiểu một
bản ghi (status riêng, vd `EXTRACT_FAILED`/`FAILED` + `error_message`, `raw_data` chứa
`api_id`) để (a) có dấu vết, (b) alertable, (c) reconcile sau này nhặt lại. Cân nhắc gộp
với PLAN "ERP-driven periodic reconcile" trong `gege-botpastedon` memory. Cần thiết kế
thêm — **không nằm trong batch fix ngay**.

---

## Tóm tắt batch fix đề xuất

| Mục | Loại | Gộp chung? | Rủi ro |
|---|---|---|---|
| #1 schema migration | code `database.py` | ✅ 1 commit | thấp |
| #2 resolve DATABASE_PATH | code `config.py` | ✅ cùng commit | thấp |
| #3 drop test_t + rm file rỗng | one-off server | sau khi #1/#2 deploy | thấp |
| #4 2 đơn false-sync | thủ công ERP | quyết định riêng | — |
| #5 orphan recovery | design | backlog sau | trung bình |

**Đề xuất:** làm #1 + #2 trong 1 commit (deploy git), rồi #3 dọn server. #4 chờ quyết định.
#5 lên thiết kế riêng.

---

# Cập nhật 2026-06-25 — điều tra cơ chế scanner g2g + storage

## [P1] #6 — Case 1: start_deliver/mark fail bị nuốt → ERP nhận đơn THIẾU thông tin giao hàng — ✅ ĐÃ FIX (commit `a901344`, deploy 2026-06-26, chung với #5)

**Mô tả.** Trong `g2g_scanner_api.py::_do_extract`, `start_deliver` + `mark_as_delivering`
(PUT) bọc `except Exception → log warning → CONTINUE` (chỉ `AuthError` raise). Khi 2 PUT
fail (G2G trả code≠2000 → `APIError`, `g2g_api.py:90`):
1. `get_order_detail` (GET) **vẫn chạy**, nhưng vì đơn **chưa chuyển sang delivering**,
   payload **thiếu `checkout_info.delivery_method_details.delivery_info`** → `_map_order_data`
   rơi vào fallback `character = buyer_username or "Check Order"` (g2g_scanner_api.py:317).
2. Đơn vẫn được insert + **push ERP với thông tin giao hàng SAI/thiếu** (erp_synced=1).
3. Trên G2G đơn **kẹt "preparing"** (chưa view/delivering).
4. **Không bao giờ retry**: lần quét sau `scan_order_list:62` `if is_order_processed(id):
   continue` → đơn đã có trong DB → bỏ qua vĩnh viễn.

**Bằng chứng.** Code path xác định. Triệu chứng = đơn ERP có `character="Check Order"`/
buyer_username. DB hiện nhỏ do purge (xem #7) nên chỉ còn vài mẫu.

**Rủi ro.** Đơn xuống ERP nhưng (a) không có thông tin giao hàng đúng, (b) G2G kẹt preparing
không ai xử. **Tệ hơn cả mất đơn vì trông như đã xử lý.**

**Plan fix.** Bắt buộc 2 PUT thành công trước khi map+push:
- `_do_extract`: nếu `start_deliver`/`mark_as_delivering` fail (non-auth) → **KHÔNG** tiếp
  tục map/push; coi như chưa sẵn sàng → chu kỳ sau retry (đừng insert để `is_order_processed`
  không chặn). Thêm **đếm số lần thử** (in-memory hoặc marker DB `DEFERRED` + cap) để đơn
  thật-sự-không-startable không lặp vô hạn.
- Nguyên tắc: **chỉ push ERP sau khi xác nhận delivering + có đủ delivery_info.**

## [P1] #5(↑) — Case 2: orphan get_order_detail fail — ✅ ĐÃ FIX (commit `a901344`, deploy 2026-06-26): EXTRACT_FAILED + recovery_loop

**Trạng thái mới.** User xác nhận **đã thực sự xảy ra** (dù hiếm) → fix dứt điểm, không để
backlog. (Mô tả gốc ở #5 trên.)

**Plan fix.** Khi `get_order_detail` bỏ cuộc SAU khi start/mark đã OK: **insert bản ghi
tracking** (status `EXTRACT_FAILED`, `raw_data` chứa `api_id`) thay vì return None. Một
**recovery loop** quét `EXTRACT_FAILED` → re-fetch detail → map → push ERP (idempotent).
Mọi đơn đã delivering-trên-G2G **không bao giờ mất**.

> #6 và #5 nên fix CHUNG (đều ở `_do_extract`): tách rõ 3 bước, mỗi bước fail có đường xử lý
> riêng — đảm bảo (a) chỉ push khi đủ info, (b) không mất đơn.

## [P2] #9 — Recovery KẸT đơn EXTRACT_FAILED chưa-từng-delivering — ✅ ĐÃ FIX (commit `d8b25db`, 2026-06-26)

> recovery_loop giờ gọi lại `scanner._do_extract` (re-attempt start_deliver+mark) thay vì
> chỉ get_order_detail; `NotReadyError` mang `status` để cancelled/refunded→FAILED.


**Mô tả.** `recovery_loop` (Phase 2) chỉ **re-fetch get_order_detail** rồi promote nếu
`order_item_status='delivering'`. Nếu một đơn vào `EXTRACT_FAILED` do **lỗi kép**
(start_deliver fail *và* get_order_detail fail — vd 429 hoặc timeout liên tiếp), đơn **chưa
hề chuyển sang delivering** (vẫn `preparing` trên G2G). Recovery đọc thấy status≠delivering
→ "retry next cycle" mãi, **không bao giờ gọi lại start_deliver** → kẹt `EXTRACT_FAILED` vĩnh
viễn → không giao, không vào ERP.

**Tần suất.** Hiếm ở tải hiện tại (cần double-failure), nhưng tăng theo 429 khi scale.
Hiện 0 đơn dính.

**Plan fix (gọn, ít churn).** Trong `recovery_loop`, thay vì chỉ get_order_detail, gọi lại
**`scanner._do_extract`** (re-attempt start+mark+gate). Cho `NotReadyError` mang `status`
để recovery: `cancelled/refunded`→`FAILED`, còn lại→giữ `EXTRACT_FAILED` retry. Như vậy đơn
chưa-start được start lại mỗi cycle.

## [P2] #7 — Bỏ cơ chế dọn dẹp định kỳ (giữ dữ liệu để điều tra) — ✅ ĐÃ LÀM (commit `33c1bd4`: cleanup_old_orders→no-op; log bền HOÃN)

**Quyết định (user 2026-06-25).** Dữ liệu không lớn → giữ để điều tra lỗi.
- **DB:** bỏ `cleanup_old_orders()` (xoá COMPLETED quá hạn + DETECTED >24h), gọi ở
  `scanners/main.py:136,189` + `base_scanner.py:142`. Volume ~30-50 đơn/ngày → `orders`
  tăng chậm, vô hại. Lợi: giữ luôn DETECTED (filter) + lịch sử soi #5/#6.
- **Log:** log bot ghi `/tmp/*.log` qua redirect `>` (nohup) → **bị ghi đè mỗi lần
  restart/deploy** → mất lịch sử (lý do journald rỗng). Đề xuất: ghi vào thư mục bền
  (`/opt/BotPasteDon/logs/`) + `>>`/logrotate thay vì `/tmp` + `>` (sửa ở script khởi động/
  `deploy_git.py`).

## [P1] #8 — Rò rỉ dung lượng: `/tmp/erp_evidence_*` = 7.3 GB không được dọn

**Root cause (bug).** Worker tải file bằng chứng từ ERP về `/tmp`
(`NamedTemporaryFile delete=False prefix="erp_evidence_"`, `g2g_worker.py:286`,
`eldorado_worker.py:211`). `cleanup_files(task_data.get("files", []))`
(`g2g_worker.py:206/228`) nhận **list dict file-info ERP** (`{url,evidence_id,name}`), KHÔNG
phải path tmp (path nằm ở biến **local `files`** trong `handle_g2g_api:340-347`, không ghi
lại `task_data`). `cleanup_files` làm `Path(dict)` → TypeError → `except: pass` → **không
xoá gì.** (Eldorado y hệt.)

**Bằng chứng.** `/tmp/erp_evidence_*` = **2933 file = 7.3 GB** (oldest 13/06; ≈40% disk).
Disk `/` 32G dùng 18G (59%). `systemd-tmpfiles-clean` chỉ đỡ một phần (atime >10d).
`/opt/BotPasteDon/proofs/` = 282MB/73 file (bản ghi bot tự tạo) cũng **không dọn** (oldest 20/05).

**Plan fix.**
1. ✅ **ĐÃ LÀM (commit `3ab63cd`, deploy 2026-06-25):** track path tmp tải-từ-dict ở
   `task_data["_downloaded_tmp"]` → unlink trong `process_task.finally` (cả g2g + eldo);
   harden `cleanup_files` bỏ qua entry non-path. Verified: code trên server, worker restart
   sạch. (Chờ 1 đơn-có-evidence để xác nhận empiric.)
2. **Một lần (server):** `rm -f /tmp/erp_evidence_*` (giải phóng ~7.3 GB ngay) + quyết policy
   `proofs/` (xoá >30 ngày, hay giữ làm bằng chứng — user quyết).
   - ✅ **Đã chạy 2026-06-25** (xoá file >60min): 2934→14 file, 7.3G→22M, disk 59%→35%.
     **CODE fix vẫn chưa làm** → rò rỉ sẽ tái diễn cho tới khi vá `cleanup_files`.

---

## Tóm tắt batch (cập nhật 2026-06-25)

| Mục | Loại | Ưu tiên | Ghi chú |
|---|---|---|---|
| #1 schema migration | code | P1 | idempotent ALTER |
| #2 DATABASE_PATH abs | code | P2 | resolve theo root |
| #3 drop test_t + rm file rỗng | server | P3 | sau #1/#2 |
| #4 2 đơn false-sync | thủ công | P3 | user tự xử |
| **#5+#6 redesign `_do_extract`** | **code** | **P1** | fix chung case-1 + case-2 dứt điểm |
| #7 bỏ cleanup_old_orders + log bền | code | P2 | giữ data điều tra |
| **#8 evidence cleanup + xoá 7.3GB** | code + server | **P1** | bug cleanup_files dict; giải phóng disk ngay |
