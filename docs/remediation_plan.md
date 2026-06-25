# Remediation Plan — BotPasteDon (chuẩn bị scale vài ngàn đơn/ngày)

> Lập 2026-06-25. Giải quyết các lỗi đã biết (`known_issues.md`) theo thứ tự ưu tiên đã
> chốt: **Phase 1 (#8) → Phase 2 (#5/#6) → Phase 3 (pipeline) → Phase 4 (DB hardening)**.
> Mục hạ tầng/queue/scale-ngang (điểm 5) **để riêng**, không nằm trong plan này.
>
> Quy ước: deploy **git-only** qua `scripts/deploy_git.py <service>` (single-branch `main`).
> Mỗi phase = commit riêng + deploy + verify trước khi sang phase sau.

---

## Phase 1 — #8 Rò rỉ evidence (P0) — ✅ ĐÃ LÀM 2026-06-25 (commit `3ab63cd`)

**Vì sao P0:** ở vài ngàn đơn/ngày, mỗi proof ~1-5MB → 3-15 GB/ngày đổ vào `/tmp` không
dọn → disk 32G đầy trong 1-3 ngày → **sập toàn bộ bot**.

**Root cause:** `cleanup_files(task_data["files"])` nhận list **dict** file-info ERP, không
phải path tmp đã tải (path nằm ở biến local) → `Path(dict)` lỗi → no-op.

**Thay đổi code (cả 2 worker):**
1. `workers/g2g_worker.py::handle_g2g_api` — gom path tmp đã tải-từ-dict vào list riêng,
   `try/finally` unlink cuối hàm (re-download lại được ở lần retry nên xoá luôn an toàn):
   ```python
   downloaded_tmp = []
   files = []
   for fp in raw_files:
       if isinstance(fp, dict):
           local = await _download_g2g_file(fp, erp_api_key)
           if local:
               files.append(local); downloaded_tmp.append(local)
       else:
           files.append(fp)            # path Discord (PROOF_DIR) — giữ cơ chế cũ
   try:
       ... các bước qty/proof/chat ...
   finally:
       cleanup_files(downloaded_tmp)   # luôn dọn /tmp re-download
   ```
2. `workers/eldorado_worker.py::handle_eldo_api` — y hệt (path tmp từ `_download_file`).
3. `workers/base_worker.py::cleanup_files` — harden: bỏ qua phần tử không phải path
   (`if not isinstance(f, (str, Path)): continue`) thay vì nuốt TypeError.
4. Giữ `cleanup_files(task_data["files"])` ở `process_task` cho **path Discord str**
   (xoá `proofs/` trên COMPLETED/TERMINAL như cũ).

**Defense-in-depth (server):**
- Một lần: `rm -f /tmp/erp_evidence_*` — ✅ đã chạy 2026-06-25 (giải phóng 7.3GB).
- Thêm lưới đỡ: cleanup định kỳ `/tmp/erp_evidence_*` >24h (cron nhỏ hoặc tận dụng
  `systemd-tmpfiles` với drop-in `/etc/tmpfiles.d/botpaste.conf`).
- `proofs/` (282MB): chốt policy giữ — xoá file COMPLETED >30 ngày (hay giữ làm bằng chứng).

**Test:** giao 1 đơn có evidence ERP → xác nhận `/tmp` file biến mất sau giao; giao 1 đơn
proof Discord → `proofs/` bị dọn khi COMPLETED, giữ khi RETRY.
**Deploy:** commit → `deploy_git.py g2g_worker eldo_worker`.
**Rủi ro:** thấp (chỉ thêm unlink path tải tạm).

---

## Phase 2 — #5/#6 Redesign `_do_extract` — ✅ ĐÃ LÀM 2026-06-26 (commit `a901344`)

> Deploy scanner_g2g sạch; happy path verified empiric (đơn `1782412824491BE73`:
> start/mark OK → gate delivering → ERP accepted → SO-260626-EBOWVEDV; 0 false NotReady/
> EXTRACT_FAILED). Nhánh lỗi (NotReady→NEEDS_MANUAL, EXTRACT_FAILED→recovery) chưa kích
> hoạt vì chưa gặp lỗi thật — cần theo dõi ca EXTRACT_FAILED/NEEDS_MANUAL đầu tiên.


**Mục tiêu (2 nguyên tắc):** (a) **chỉ push ERP sau khi xác nhận delivering + đủ
delivery_info**; (b) **không bao giờ mất đơn đã delivering trên G2G**.

**Thay đổi code — `scanners/g2g_scanner_api.py`:**

1. `_do_extract`: 2 bước PUT **không còn best-effort nuốt lỗi**:
   - `start_deliver`/`mark_as_delivering` fail (non-auth) → **KHÔNG** map/push. Trả tín hiệu
     "chưa sẵn sàng" (vd raise `NotReadyError` nội bộ) → KHÔNG insert → chu kỳ sau quét lại
     (đơn vẫn "preparing" trong list → retry tự nhiên).
   - Chống lặp vô hạn: đếm số lần thử per order (in-memory `{order_id: n}`); quá `MAX_START_RETRY`
     (vd 5) → insert record `NEEDS_MANUAL` + log alert (để `is_order_processed` chặn + nổi lên).
2. Sau khi start+mark OK → `get_order_detail` (lúc này state=delivering → có delivery_info):
   - Thành công → map. Cảnh báo nếu `delivery_info` vẫn rỗng (không fallback im lặng "Check
     Order" cho loại cần character).
   - Fail 3 lần (đơn ĐÃ delivering trên G2G) → **insert record `EXTRACT_FAILED`**,
     `raw_data={api_id, order_item_id, url}` thay vì `return None`.
3. **Recovery loop mới** (trong `scanners/main.py` hoặc `status_sync`): định kỳ quét
   `status='EXTRACT_FAILED'` → re-fetch `get_order_detail` → map → push ERP → mark synced.
   Idempotent (đã có claim ERP). Mọi đơn delivering-trên-G2G được nhặt lại, không mất.
4. `scan_order_list`: giữ `is_order_processed` chặn trùng, nhưng nhớ các record mới
   (`NEEDS_MANUAL`, `EXTRACT_FAILED`) cũng tính là "processed" → không bị quét lại như đơn
   thường (đúng — recovery loop xử riêng).

**Test:** mock `start_deliver` ném APIError → đơn không push, retry; mock `get_order_detail`
fail sau khi mark OK → record `EXTRACT_FAILED` → recovery loop push thành công.
**Deploy:** commit → `deploy_git.py g2g_scanner` (+ service chứa recovery loop).
**Rủi ro:** **TRUNG BÌNH-CAO** — đụng luồng giao hàng live. Cần test kỹ + theo dõi sát sau deploy.
Cân nhắc bật log chi tiết tạm thời để quan sát tần suất start_deliver fail thực tế.

---

## Phase 3 — Pipeline (chịu tải burst) — 🟡 PHẦN 1 ĐÃ LÀM 2026-06-26

> ✅ **Đã làm (commit kèm):** `page_size=100` cho `get_pending_orders` (scanner thấy tới
> 100 đơn preparing/lần — hết tràn cửa sổ 10) + `list_orders_by_status` (giúp lỗ hổng g2g
> reconcile chỉ thấy ~20). Probe live: G2G honors `page_size` (limit/page/per_page bị bỏ
> qua), payload có cursor `next`. Thêm log visibility cho 429. Vòng extract tuần tự = throttle
> tự nhiên nên page lớn không gây burst 429.
> ⏸️ **Hoãn (chưa cần / rủi ro cao):** back-off 429 sâu + throttle (3.2 dưới) và song song
> hoá worker (3.3) — làm khi có tải thật + load-test, không thêm concurrency vào path vừa
> rewrite. Cursor `next` (phân trang đầy đủ >100) cũng để sau (100 đã dư cho hiện tại).


**Mục tiêu:** không bỏ sót đơn lúc burst + không bị marketplace rate-limit đánh sập.

1. **Cửa sổ scanner ~20 (chống tràn):** `get_pending_orders` chỉ lấy ~20 đơn mới nhất.
   - Phân trang `list_my_order` (lặp tới khi hết đơn mới hoặc chạm cap), HOẶC
   - giảm `scan_interval` động khi phát hiện list đầy (≈20 → có thể còn nữa).
2. **Back-off rate-limit:** `_parse` đã có `RateLimitError(retry_after)` nhưng
   `get_pending_orders` nuốt `APIError→[]`. Xử lý 429 tường minh: sleep `retry_after`, không
   hammer. Thêm throttle tối thiểu giữa các call (token bucket / sleep nhỏ).
3. **Song song hoá có kiểm soát:** extract hiện tuần tự per order. Dùng
   `asyncio.gather` + `Semaphore(N)` để xử nhiều đơn cùng lúc, N giới hạn theo rate-limit G2G.
   Worker: chạy nhiều instance (claim pattern đã chống trùng) thay vì 1.

**Test:** giả lập burst >20 đơn → xác nhận không sót; giả lập 429 → có back-off, không spam.
**Deploy:** từng phần nhỏ, theo dõi rate-limit thật.
**Rủi ro:** TRUNG BÌNH — đụng nhịp gọi API; làm sau khi Phase 1+2 ổn định.

---

## Phase 4 — DB hardening — ✅ ĐÃ LÀM 2026-06-26 (commit `33c1bd4`, +#3 cleanup server)

> ✅ #1 schema migration idempotent · busy_timeout 5s→15s · #2 DATABASE_PATH tuyệt đối ·
> #7 cleanup_old_orders→no-op · #3 drop test_t + xoá 2 file orders.db rỗng. Validate trên
> prod DB trước khi bounce (path đúng, busy_timeout=15000, 2 cột có sẵn no-op, cleanup
> no-op 163→163); bounce `all` sạch, 9 process sống, 0 traceback/no-such-column.
> ⏸️ **Hoãn (ghi rõ):** gỡ coupling `updated_at`/claim (hoạt động ổn, để khi rảnh); WAL
> checkpoint thủ công (auto-checkpoint đủ); log bền (đổi launcher — việc hạ tầng riêng).


**Mục tiêu:** chịu ghi đa-process khi tải cao mà không `database is locked` / mất ghi.
(Gộp luôn #1, #2, #7 từ `known_issues.md`.)

1. **#1 Schema migration idempotent** — `shared/database.py::_init_db`: vòng ALTER cho
   `retry_data`, `erp_synced INTEGER DEFAULT 0`, `erp_retry_count INTEGER DEFAULT 0`
   (try/except). Chống deploy DB mới crash `no such column`.
2. **Write-retry wrapper** — helper retry khi `sqlite3.OperationalError: database is locked`
   (3-5 lần, backoff 50-200ms) bọc mọi hàm ghi (hoặc trong `_get_conn().execute`). Đây là
   điểm yếu chính khi nhiều process ghi.
3. **WAL checkpoint định kỳ** — `PRAGMA wal_checkpoint(TRUNCATE)` (vd trong heartbeat 1
   process) để chặn WAL phình.
4. **Gỡ coupling `updated_at`/claim** — tách cột `claimed_at` riêng cho đo "claim stale
   >180s", không dùng chung `updated_at` (vốn bị mọi UPDATE đụng tới).
5. **#2 `DATABASE_PATH` tuyệt đối** — `config.py` resolve theo project root nếu là path
   tương đối. Sau đó xoá 2 file `orders.db` rỗng + drop bảng `test_t` (#3).
6. **#7 Bỏ `cleanup_old_orders`** (user đã chốt) + chuyển log bot từ `/tmp`+`>` sang
   `/opt/BotPasteDon/logs/`+`>>`/logrotate (giữ lịch sử điều tra).

**Test:** chạy nhiều writer đồng thời (script) → không mất ghi, không ném lock; deploy DB
sạch → đủ cột.
**Deploy:** commit → `deploy_git.py` toàn bộ service (đụng `database.py`/`config.py` chung).
**Rủi ro:** TRUNG BÌNH — `database.py` dùng chung mọi process; test kỹ write-retry.

---

## Thứ tự & phụ thuộc

```
Phase 1 (#8)  ──►  Phase 2 (#5/#6)  ──►  Phase 3 (pipeline)  ──►  Phase 4 (DB hardening)
   P0               P1 sensitive          scale-prep              scale-prep
```
- Phase 1 độc lập, an toàn, làm ngay.
- Phase 2 độc lập với 1, nhưng nhạy cảm → làm sau 1 để giảm biến số.
- Phase 3, 4 là chuẩn bị scale; làm sau khi 1+2 chạy ổn vài ngày.
- **Hoãn (điểm 5, plan riêng):** tách worker đa-VM, hàng đợi Redis/RQ, writer-service đơn,
  cân nhắc đổi datastore — chỉ cần khi thật sự lên vài ngàn/ngày bền vững.

## Liên quan
`known_issues.md` (chi tiết từng bug), `proof_mechanism.md`, `database.md`,
`order_filtering.md`, `architecture.md`.
