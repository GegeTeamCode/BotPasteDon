# Database — BotPasteDon

> Điều tra 2026-06-25. Trả lời: bot có mấy database, phần nào của code đang cập nhật
> database, và các vấn đề phát hiện được.

## TL;DR

- **Chỉ 1 database nghiệp vụ**: SQLite `data/orders.db` (WAL mode), dùng chung bởi
  **tất cả** process. 7 bảng (6 nghiệp vụ + 1 bảng rác `test_t`).
- Các sqlite khác trên server **không phải DB nghiệp vụ**: là profile trình duyệt
  (`chrome_profile_eldo/cookies.sqlite`, `places.sqlite`…) — do browser quản lý,
  auth service chỉ **đọc read-only** để tái dùng cookie.
- Truy cập DB tập trung qua 1 class duy nhất: `shared/database.py::Database`.
- ⚠️ **Phát hiện nợ kỹ thuật**: schema drift (2 cột chạy production nhưng thiếu trong
  code khởi tạo), 2 file `orders.db` rỗng lạc chỗ, 1 bảng `test_t` rác. Chi tiết ở cuối.

## 1. Có mấy database?

| File (trên `.220`) | Loại | Vai trò | Ai dùng |
|---|---|---|---|
| `/opt/BotPasteDon/data/orders.db` (~51 MB) | SQLite WAL | **DB nghiệp vụ chính** | TẤT CẢ process |
| `…/data/orders.db-wal`, `…-shm` | WAL sidecar | Write-ahead log + shared mem của WAL | (tự động) |
| `chrome_profile_eldo/cookies.sqlite` (+ `places/permissions/…sqlite`) | Firefox/Camoufox profile | Cookie Eldorado | Auth service **đọc RO** (`auth/main.py:980`, bảng `moz_cookies`) |
| `chrome_profile_g2g/…` (Chrome profile) | Chrome profile | Session G2G | Browser/CDP (không qua sqlite app) |
| `/opt/BotPasteDon/orders.db` (0 B) | rác | File rỗng lạc chỗ (May 31) | — (cleanup) |
| `/opt/BotPasteDon/scanners/orders.db` (0 B) | rác | File rỗng lạc chỗ (May 31) | — (cleanup) |

- Đường dẫn cấu hình: `DATABASE_PATH = os.getenv("DATABASE_PATH", "data/orders.db")`
  (`shared/config.py:61`). Default **tương đối** → chạy sai thư mục làm việc sẽ tạo
  `orders.db` rỗng ở chỗ khác (chính là 2 file rác trên).
- Production luôn chạy với cwd `/opt/BotPasteDon` nên dùng `data/orders.db`.

## 2. Lớp truy cập & cơ chế kết nối

Tất cả I/O đi qua `shared/database.py::Database` (~640 dòng). Không process nào viết
SQL trực tiếp vào `orders.db` ngoài class này (chỉ vài script `tests/` đọc tay).

- **Mỗi process khởi tạo một `Database(DATABASE_PATH)` riêng** (scanner, worker,
  coordinator, status_sync, auth, dashboard, watchdog — xem bảng mục 4).
- Mỗi lời gọi method **mở connection mới rồi đóng** (`_get_conn()` →
  `conn.close()` trong `finally`). Không pool.
- PRAGMA: `journal_mode=WAL`, `busy_timeout=5000`, `row_factory=Row`.
- **`threading.Lock` per-instance** bao quanh mỗi method → chỉ serialize trong CÙNG
  process. Giữa các process, an toàn ghi dựa vào: **WAL + busy_timeout 5s** và
  **claim nguyên tử** (`claim_erp_order`, xem mục 6).

## 3. Các bảng & process nào GHI vào

`_init_db()` (`database.py:30`) tạo sẵn bằng `CREATE TABLE IF NOT EXISTS`.

| Bảng | rows (24/6) | Nội dung | Process GHI |
|---|---|---|---|
| `orders` | 179 | Vòng đời từng đơn (state machine + cờ ERP) | **scanner**, **worker**, **coordinator** |
| `heartbeat` | 8 | Nhịp sống mỗi service (watchdog đọc) | **mọi** process |
| `marketplace_status` | 13 450 | State marketplace từng đơn + cờ đã push ERP | **status_sync** |
| `marketplace_state_counts` | 13 | Đếm số đơn theo state (snapshot dashboard) | **status_sync** |
| `marketplace_disputes` | 244 | Case tranh chấp G2G | **status_sync** (g2g) |
| `pending_dispatches` | 0 | Hàng đợi retry dispatch coordinator→worker | **coordinator** |
| `test_t` | 0 | **RÁC** (bảng test sót lại) | — (cleanup) |

## 4. Phần code nào CẬP NHẬT database (theo process)

Liệt kê đầy đủ các method ghi (INSERT/UPDATE/DELETE) và nơi gọi:

### Scanner (`scanners/main.py`, `base_scanner.py`, `*_scanner_api.py`)
| Method | Vị trí gọi | Tác dụng |
|---|---|---|
| `insert_order` | `g2g_scanner_api.py:78`, `eldorado_scanner_api.py:70`, `base_scanner.py:294`, `main.py:145/196/248` | Tạo đơn mới status `DETECTED` (`INSERT OR IGNORE`) |
| `update_order_status` | `base_scanner.py:308`, `main.py:150/200/252` | → `NOTIFIED` sau khi webhook OK |
| `claim_erp_order` / `mark_erp_synced` / `release_erp_order` / `increment_erp_retry` | `main.py:63-112` (`send_order_webhook` + `erp_retry_loop`) | Cờ đồng bộ ERP (0/1/2 + retry count) |
| `cleanup_old_orders` | `base_scanner.py:142`, `main.py:136/189` | Xoá COMPLETED quá hạn + DETECTED >24h |
| `update_heartbeat` | `main.py:163/213/260` | Nhịp `scanner_{platform}` |

### Worker (`workers/g2g_worker.py`, `eldorado_worker.py`)
| Method | Vị trí | Tác dụng |
|---|---|---|
| `update_order_status` | g2g `193/205/223/235`, eldo `131-173` | `DELIVERING` → `COMPLETED` / `FAILED` |
| `mark_retry_attempt` | g2g `243` | → `RETRY_PENDING` + bump retry_count |
| `update_heartbeat` | g2g `553`, eldo `640` | Nhịp `worker_g2g` / `worker_eldo` |

### Coordinator (`coordinator/discord_bot.py`)
| Method | Vị trí | Tác dụng |
|---|---|---|
| `update_order_status` | `286/332` | → `THREAD_CREATED` (kèm discord_thread_id) |
| `queue_dispatch` / `mark_dispatch_attempt` / `remove_dispatch` | `186/421/394-411` | Quản hàng đợi retry dispatch task |
| `update_heartbeat` | `483` | Nhịp `coordinator` |

### Status Sync (`status_sync/g2g_sync.py`, `eldo_sync.py`, `reconcile.py`, `main.py`)
| Method | Vị trí | Tác dụng |
|---|---|---|
| `upsert_marketplace_status` | g2g `120`, eldo `117` | Ghi/cập nhật state marketplace từng đơn |
| `mark_marketplace_pushed` | g2g `146`, eldo `133`, reconcile `47` | Đánh dấu đã push ERP (hoặc tăng push_attempts) |
| `set_marketplace_state_counts` | g2g `100`, eldo `84` | Snapshot đếm theo state |
| `upsert_dispute` | g2g `171/192` | Ghi/cập nhật case tranh chấp |
| `update_heartbeat` | `main.py:43` | Nhịp `status_sync` |

### Auth / Dashboard / Watchdog
| Process | Ghi gì |
|---|---|
| Auth (`auth/main.py:1631`) | chỉ `update_heartbeat("auth_service")`. (Đọc cookie RO từ `cookies.sqlite`, **không** ghi orders.db) |
| Dashboard (`dashboard/server.py:467`) | chỉ `update_heartbeat("dashboard")` (còn lại READ-only để hiển thị) |
| Watchdog (`scripts/watchdog.py:199`) | **đọc** `get_stale_services` (không ghi nghiệp vụ) |

**Tóm tắt quyền ghi:**
- `orders` ← scanner (tạo + ERP flag), worker (delivery), coordinator (thread).
- `marketplace_*` + `disputes` ← chỉ status_sync.
- `pending_dispatches` ← chỉ coordinator.
- `heartbeat` ← mọi process.

## 5. Vòng đời bản ghi `orders` (state machine)

```
            scanner                 coordinator         worker
 (API scan) ─► DETECTED ─webhook─► NOTIFIED ─► THREAD_CREATED ─► DELIVERING ─► COMPLETED
                  │ (rớt filter:                                      │
                  │  ở lại DETECTED,                                  ├─► FAILED
                  │  xem order_filtering.md)                          └─► RETRY_PENDING ─► (worker retry)
```

Song song, cờ **`erp_synced`** (0=cần push · 1=đã sync · 2=in-flight/đã claim) +
`erp_retry_count` do scanner quản (push Sell Order sang ERP).

**Dọn dẹp** (`cleanup_old_orders`, `database.py:221`):
- Xoá `COMPLETED` cũ hơn `CACHE_MAX_AGE_HOURS`.
- Xoá `DETECTED` > 24h (đơn rớt filter, không còn pending).
- **Giữ** `FAILED` (audit) và `RETRY_PENDING` (in-flight).

Phân bố thực tế (24/6): g2g `THREAD_CREATED`=63, `COMPLETED`=26, `DETECTED`=32 (erp_synced=0),
`FAILED`=6, `NOTIFIED`=4; eldo `THREAD_CREATED`=34, `COMPLETED`=11, `FAILED`=3.

## 6. Đồng bộ ERP — chống đua đa process (`erp_synced` 0/1/2)

Hai scanner (eldo + g2g) + retry loop có thể chạm cùng đơn → cơ chế **claim nguyên tử**:
- `claim_erp_order` (`database.py:376`): `UPDATE … SET erp_synced=2 WHERE erp_synced=0
  OR (=2 AND claim cũ stale)`. `rowcount>0` ⇒ thắng claim → chỉ 1 process push ERP.
- Push xong: `mark_erp_synced` (=1). Thất bại: `release_erp_order` (về 0) +
  `increment_erp_retry`. Claim stale >180s được tự nhặt lại (process chết giữa chừng).
- `get_unsynced_orders` **loại** `status IN (DETECTED, FAILED)` → đơn rớt filter
  không bao giờ bị retry sang ERP (đúng thiết kế, xem `order_filtering.md`).

## 7. ⚠️ Nợ kỹ thuật / phát hiện cần lưu ý

1. **SCHEMA DRIFT (quan trọng)**: cột `erp_synced` và `erp_retry_count` **đang chạy
   trong DB production** nhưng **KHÔNG có** trong `_init_db()` CREATE TABLE, cũng không
   có `ALTER TABLE … ADD COLUMN` nào trong code (chỉ `retry_data` có ALTER ở
   `database.py:121`). Tức 2 cột này được thêm **thủ công** trên server.
   → **Rủi ro**: deploy lên máy/DB mới (hoặc xoá DB tạo lại) sẽ thiếu 2 cột →
   `mark_erp_synced`/`claim_erp_order`/`increment_erp_retry` **crash** `no such column`.
   → **Đề xuất**: thêm migration ALTER cho `erp_synced INTEGER DEFAULT 0` và
   `erp_retry_count INTEGER DEFAULT 0` (kiểu try/except như `retry_data`) trong `_init_db`.

2. **2 file `orders.db` rỗng lạc chỗ**: `/opt/BotPasteDon/orders.db` và
   `/opt/BotPasteDon/scanners/orders.db` (0 byte) — sinh ra do `DATABASE_PATH` mặc định
   tương đối khi chạy sai cwd. Vô hại nhưng gây nhầm. Nên xoá + cân nhắc đặt
   `DATABASE_PATH` tuyệt đối trong `.env`.

3. **Bảng rác `test_t`** trong `orders.db` — sót lại từ test. Drop được.

4. **Không có index trên `erp_synced`/`status` kết hợp cho retry**: `get_unsynced_orders`
   quét `orders` theo `erp_synced` + `status NOT IN (...)`. Bảng `orders` nhỏ (179 rows
   do cleanup 24h) nên không phải vấn đề hiệu năng hiện tại; chỉ lưu ý nếu sau này giữ
   lịch sử lâu hơn.

## Liên quan

- `order_filtering.md` — vì sao đơn `DETECTED` rớt filter không vào ERP.
- `architecture.md` — sơ đồ 9 process + data flow.
