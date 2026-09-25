# Proof Mechanism — G2G & Eldorado

> Điều tra 2026-06-25. Cơ chế đính bằng chứng giao hàng (proof) end-to-end cho 2 sàn,
> nguồn file, đích upload, khác biệt, và liên hệ rò rỉ storage (#8 trong `known_issues.md`).

## TL;DR

- **2 nguồn file proof** (chung cả 2 sàn): (1) [ĐÃ GỠ 2026-09-15] nút Discord → lưu
  `proofs/`; (2) ERP gửi `file_info` dict → worker tải về `/tmp/erp_evidence_*`.
- **G2G**: proof là **delivery_proof CHÍNH THỨC** trên marketplace (upload S3 presigned).
  **Bắt buộc** — thiếu proof thì đơn terminal, không complete.
- **Eldorado**: proof gửi **qua chat TalkJS/Firebase** (không có delivery_proof chính thức).
  **Non-fatal** — fail thì gửi link text, đơn vẫn complete.
- Cả 2 worker dùng chung pattern `cleanup_files()` lỗi → **file `/tmp` tải về không bao giờ
  bị dọn** (rò rỉ #8).

---

## 1. Nguồn file proof (2 luồng nạp)

### 1a. Discord-button (ĐÃ GỠ 2026-09-15 cùng coordinator — chỉ còn để tham khảo)
`coordinator/discord_bot.py:138-175` (và `workers/base_worker.py:113-122` bản tương tự):
- Operator đính kèm ảnh/video vào Discord thread → bấm nút **"🚀 Đã giao (Gửi Proof)"**.
- Coordinator quét `thread.history`, lọc ext `.png/.jpg/.jpeg/.mp4`, lưu vào
  `PROOF_DIR/{order_id}_{att.id}_{safe_name}` (= `/opt/BotPasteDon/proofs/`).
- `task_data["files"] = [đường dẫn str]` → dispatch sang worker.
- Đó là vì sao `proofs/` có các file `{order_id}_*_bandicam_2026-..._.mp4` (bản ghi màn hình).

### 1b. ERP-driven (tự động)
- `task_data["files"] = [dict {url, evidence_id, name}]` (evidence do ERP cấp).
- Worker tải từng dict về `/tmp` qua `tempfile.NamedTemporaryFile(delete=False,
  prefix="erp_evidence_")` (g2g `_download_g2g_file:262`, eldo `_download_file:181`).
- `str` (Discord path) → dùng luôn; `dict` (ERP) → tải `/tmp`.

> Workflow đã chuyển dần sang 1b (ERP-driven): `proofs/` ngừng tăng từ ~18/6, còn
> `/tmp/erp_evidence_*` mới là chỗ phình (xem #8).

---

## 2. G2G — proof chính thức trên marketplace

### API path — `g2g_worker.handle_g2g_api` → `g2g_api._upload_proofs` (g2g_api.py:325)
1. `get_upload_url(filename, upload_type="delivery_proof")` → presigned POST (G2G S3).
2. `upload_to_s3(presigned, file)` → POST file lên **S3 của G2G** (requests thường, không
   cần fingerprint).
3. `submit_delivery_proof(order_item_id, upload_list)` → gắn proof CHÍNH THỨC vào order.

Đặc điểm:
- **Ext cho phép** (`_G2G_PROOF_EXTS`): `jpg, jpeg, png, gif, mp4, mov`. Khác → G2G trả
  HTTP 400 "unsupported file type" → **skip từng file** (per-file isolation; 1 file webp
  hỏng không phá cả bước — fix order LVB9).
- **Phân loại lỗi khi 0 file upload được** (fix 2026-08-31, bug 27–31/8: lỗi S3 tạm thời
  bị gán nhãn "unsupported" → terminal → 7 đơn kẹt không proof):
  - `failed` (không có presigned URL, S3/network lỗi từng file) → `APIError` **retry**
    (message nhúng nguyên văn lỗi underneath để `_classify_error` xếp network/unknown —
    đều vào nhánh retry của worker).
  - Chỉ còn `unsupported` (ext xấu) → giữ message terminal "manual upload needed"
    nguyên văn: KHÔNG complete đơn thiếu proof, KHÔNG loop vô hạn.
  - ≥1 file upload được → submit như thường, log kèm số unsupported/failed.
- Thứ tự bước giao: qty → proof → chat (`handle_g2g_api`).

### Selenium fallback — `handle_g2g` (g2g_worker.py:414)
Upload qua web: mở order, click dialog **"Proof gallery"**, submit qua nút
`order-item-delivery-proof-dialog-submit-btn`.

---

## 3. Eldorado — proof là ĐÍNH KÈM THẬT trong chat TalkJS

Eldorado không có endpoint `delivery_proof` chính thức; bằng chứng nằm trong
hội thoại TalkJS. Từ 26/09/2026 nó là **đính kèm thật** (người mua thấy trình
phát video / ảnh ngay trong khung chat), không còn là link dán vào tin nhắn.

### API path — `eldorado_worker.handle_eldo_api` bước "proofs"
1. Lấy `talkJsConversationId` từ `get_order_detail`.
2. `_download_file(fp)` → local path (`/tmp/erp_evidence_*`).
3. `talkjs_client.send_attachment(conv_id, local_path, display_name)`.
4. Bước "chat" chỉ còn gửi lời nhắn trong `message.txt` — **không** kèm link nữa.

### Vì sao phải đi backend cũ

TalkJS có hai mặt API nhận payload khác nhau:

| | Realtime WS `wss://realtime.talkjs.com/v1` | Backend cũ `https://app.talkjs.com/api/v0` |
|---|---|---|
| Dùng cho | tin nhắn text (`content` blocks) | đường trình duyệt dùng thật |
| Gửi file | **bắt buộc `fileToken`** | nhận thẳng `attachment` + URL Firebase |
| Cấp fileToken | chỉ secret key phía server | không cần — TalkJS **tự đúc** |

Bot chỉ có JWT người dùng nên không xin được `fileToken`. Vì vậy đính kèm đi
qua `POST /say/{convId}/` của backend cũ — TalkJS nhận URL Firebase rồi tự sinh
`fileToken` và trả về tin nhắn `content:[{type:"file", …}]` hoàn chỉnh.

### Ba chi tiết bắt buộc khớp với client thật

1. **Đường lưu Firebase** `user_files/{appId}/{uuid-không-gạch}/{tên gốc}` —
   đoạn giữa là UUID ngẫu nhiên mỗi lần, **không phải** conversation id.
2. **`contentDisposition: inline`** cho mp4/png/jpg/gif/pdf… — thiếu nó thì
   trình duyệt người mua **tải file về** thay vì xem ngay trong chat.
3. **Id nội bộ**: `nymId` và conversation id trong URL đều là SHA-1 rút gọn
   10 byte của id ngoài (`talkjs_internal_id`), nym thêm hậu tố `_n`.
   Sai nym → `404 "Sender does not exist"`; sai conv → `400 "conversation_not_found"`.
   Cả hai lỗi đều xảy ra **trước khi** tin nhắn được tạo, nên không sinh rác.

Vector băm được khoá trong `tests/test_talkjs.py` — test đỏ nghĩa là có người
đụng vào hàm băm, đừng sửa vector.

### Header `x-talkjs-client-build`

`TalkJSConfig.client_build` ghim bản frontend của TalkJS. Lấy lại khi cần:
tải `https://app.talkjs.com/app/{appId}/user/0/inbox/chats` rồi đọc thẻ
`<script src=".../browser-bundle-release-<hash>.js">`. Nếu TalkJS đổi bản và
backend bắt đầu từ chối, cập nhật hằng này.

### Đặc điểm
- **Non-fatal**: cả bước proof bọc `try/except → warning`, đơn **vẫn complete** dù proof fail.
- `_download_file`: `str` → trả luôn; `dict` (ERP) → tải `/tmp/erp_evidence_*`.
  Tên tạm vô nghĩa nên phải truyền `display_name` = `fp["name"]` để người mua
  thấy tên file thật.
- `width`/`height`/`duration` đọc bằng `shared/media_probe.py` (parse header,
  **không cần ffmpeg**). Thiếu số đo không phải lỗi — chỉ là thumbnail giật một nhịp.
- Thứ tự bước: deliver → proofs → chat.

---

## 4. Khác biệt chính

| | G2G | Eldorado |
|---|---|---|
| Đích upload | G2G S3 (presigned POST) | Firebase Storage của TalkJS |
| Gắn vào | `delivery_proof` chính thức của order | đính kèm thật trong chat TalkJS |
| Bắt buộc? | **CÓ** — thiếu proof → terminal, không complete | **KHÔNG** — fail thì bỏ qua, vẫn complete |
| Lọc ext ở bot | jpg/jpeg/png/gif/mp4/mov | không (đẩy thẳng Firebase) |
| Fallback | Selenium "Proof gallery" | không có — proof fail thì bỏ qua |
| Thứ tự bước | qty → proof → chat | deliver → proofs → chat |

---

## 5. Storage & dọn dẹp — cả 2 sàn đều dính bug #8

- **ERP dict → `/tmp/erp_evidence_*`** (`delete=False`): `cleanup_files(task_data["files"])`
  (g2g `g2g_worker.py:206/228`, eldo `eldorado_worker.py:138/153`) nhận **list dict**, làm
  `Path(dict)` → TypeError → `except: pass` → **không xoá gì**. Path tmp thật nằm ở **biến
  local `files`** (g2g `handle_g2g_api:340-347`, eldo trong vòng download), không ghi lại
  `task_data` → orphan vĩnh viễn. → **rò rỉ** (đã vá tay 7.3 GB 2026-06-25; CODE chưa fix).
- **Discord str → `proofs/`**: `cleanup_files(str)` → `Path(str).unlink()` OK, **chỉ chạy
  trên COMPLETED/TERMINAL**. Đơn RETRY cố ý giữ file để retry. Còn 73 file/282MB = đơn
  fail/không-complete (legacy).
- **Fix #8 (xem known_issues.md):** sau khi dùng, unlink path tmp THẬT (đặt
  `cleanup_files(files_local)` trên path đã tải, hoặc `try/finally` unlink), KHÔNG truyền
  dict. Áp cho CẢ 2 worker.

## Liên quan
- `known_issues.md` #8 (rò rỉ storage), #5/#6 (`_do_extract`).
- `database.md`, `order_filtering.md`, `architecture.md`.
