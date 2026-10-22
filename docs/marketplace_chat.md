# Chat với người mua — G2G và Eldorado

Viết ngày 2026-10-22. Tài liệu này mô tả trọn đường bot nhắn tin cho người mua trên hai sàn:
lấy token nào, mở hội thoại ra sao, gửi tin và đính kèm bằng lệnh gì, lỗi thì xử lý thế nào.
Người đọc là engineer hoặc AI agent sửa `workers/` và `shared/*_api.py`.

Cơ chế lấy token sàn (G2G JWT, Eldorado Seller API) nằm ở [marketplace_auth.md](marketplace_auth.md).
Cách tải và gắn bằng chứng nằm ở [proof_mechanism.md](proof_mechanism.md). Tài liệu này chỉ nói
phần chat và những token đi riêng cho chat.

---

## TL;DR

| | G2G | Eldorado |
|---|---|---|
| Nền tảng chat | Sendbird (app `34201740-152E-401E-AD8F-5C72EEABA386`) | TalkJS (app `49mLECOW`) |
| Token để chat | Chỉ `Session-Key` Sendbird, **tĩnh trong `.env`** (`SENDBIRD_SESSION_KEY`), không cần JWT G2G; cùng một khoá từ 20/05/2026 vẫn dùng được (mục 2.1) | JWT TalkJS xin từ `GET /api/conversations/me/authorize` bằng token Eldorado |
| Id hội thoại | `channel_url` trả về từ `POST sls.g2g.com/chat/channel`; lỗi thì tìm kênh có sẵn qua Sendbird | `talkJsConversationId` trong `GET /api/orders/me/{id}` |
| Gửi chữ | REST Sendbird `POST /v3/group_channels/{channel_url}/messages` | WebSocket `wss://realtime.talkjs.com/v1/...` |
| Gửi file trong chat | không — bằng chứng G2G là `delivery_proof` chính thức của đơn | Firebase Storage rồi `POST app.talkjs.com/api/v0/.../say/{convId}/` |
| Nội dung | 2 tin: thẻ đơn hàng + `message.txt` | 1 đính kèm cho mỗi file bằng chứng + `message.txt` |
| Chat lỗi thì đơn? | vẫn `COMPLETED` + dấu `CHAT_PENDING`, tự gửi lại riêng lời nhắn tối đa 5 lượt (mục 4) | như G2G nếu bằng chứng đã lên; bằng chứng lỗi thì `FAILED` + `PROOF_PENDING`, gửi lại cả bằng chứng lẫn lời nhắn |
| Đã thấy chạy thật | có: 20/20 đơn `Completed` gần nhất có thẻ đơn + lời nhắn trên Sendbird (đo 22/10, mục 2.6) | có: đơn 15/10 và 22/10 (mục 3.8) |

---

## 1. Khi nào bot chat

Bot không trả lời tin nhắn, không đọc hội thoại. Nó chỉ gửi **một lượt tin cuối đơn**, khi ERP gửi
task giao hàng (`POST /task` vào worker: G2G cổng 8002, Eldorado cổng 8001), tức khi nhân viên bấm
"Đã giao" trên ERP.

| Sàn | Thứ tự bước trong worker (chế độ API) | Hàm |
|---|---|---|
| G2G | `qty` → `proof` → `chat` | `workers/g2g_worker.py` `handle_g2g_api` |
| Eldorado | `deliver` → `proofs` → `chat` | `workers/eldorado_worker.py` `handle_eldo_api` |

Task Eldorado `action=fast_delivery` chỉ bấm delivered, không gửi bằng chứng, không chat.

**Lời nhắn** lấy từ `message.txt` ở gốc repo (worker chạy với cwd `/opt/BotPasteDon`), đọc lại mỗi
task, nên sửa file là có hiệu lực ngay, không cần restart.

- Nhánh API của cả hai sàn: file tồn tại thì `message = read().strip() or "Done"` (file rỗng → gửi
  `Done`); file **không tồn tại** thì `message = ""` và bước chat bị bỏ qua.
- Nhánh Selenium (không dùng trên `.220`): mặc định `"Done"`.

Nội dung hiện tại:

```
Thank you for your purchase!
I have delivered your order as requested.
Please confirm receipt and leave a feedback to support me. 🌟
Have a great day!
```

**Chế độ đang chạy trên `.220`** (đọc `.env` ngày 22/10/2026): `G2G_USE_API=true`,
`ELDO_USE_API=true`, `ELDO_AUTH_MODE=seller_api`. Nhánh Selenium của cả hai worker còn trong mã
nhưng không chạy; mô tả ngắn ở mục 2.5 và 3.7.

---

## 2. G2G — Sendbird

### 2.1 Chuỗi xác minh

Chat G2G cần **hai** thứ độc lập nhau. Bản thân Sendbird chỉ cần Session-Key; JWT G2G chỉ dùng cho
hai lệnh gọi `sls.g2g.com` để biết **gửi vào kênh nào** (bước 2.2 và 2.3).

1. **JWT G2G** cho các lệnh gọi `sls.g2g.com` (đọc đơn lấy `buyer_id`, mở kênh chat).
   - Dịch vụ auth giữ JWT (làm mới bằng `POST sls.g2g.com/user/refresh_access`, lùi về Selenium
     khi hỏng — xem [marketplace_auth.md](marketplace_auth.md) mục G2G).
   - Worker lấy qua `GET http://localhost:8010/auth/g2g` → `{jwt_token, cookies, user_agent}`,
     cache 300 giây (`shared/g2g_auth.py` `G2GAuthManager`).
   - `seller_id` = claim `sub` của JWT.
   - Header gửi `sls.g2g.com` (`G2GAuthData.build_headers`): `authorization: <JWT trần, không có
     tiền tố Bearer>`, `user-agent`, `accept`, `content-type: application/json`,
     `origin: https://www.g2g.com`, `referer: https://www.g2g.com/`, và `Cookie` nếu có. Client
     `curl_cffi` giả TLS trình duyệt (chọn ngẫu nhiên trong `chrome110..120`, `edge99/101`).
2. **Session-Key Sendbird** cho mọi lệnh gọi Sendbird (gửi tin, đọc kênh, đọc tin).
   - Dịch vụ auth **không** cấp khoá này: `/auth/g2g` chỉ trả `jwt_token`, `cookies`, `user_agent`
     (`auth/main.py` dòng ~386 và ~720). `G2GAuthData.sendbird_session_key` vì thế luôn lấy từ biến
     môi trường `SENDBIRD_SESSION_KEY` (`shared/config.py`).
   - Khoá dài 40 ký tự, chép tay từ HAR trình duyệt (comment `from HAR 8` trong `shared/g2g_api.py`).
     **Không có gì làm mới nó.**
   - **Đo 22/10/2026 trên `.220`** (lệnh GET chỉ đọc lên Sendbird, header chỉ có Sendbird, không có
     JWT hay cookie G2G):

     | Header gửi | Kết quả |
     |---|---|
     | `Session-Key` + `App-ID` | 200 |
     | chỉ `Session-Key` | 200 — `App-ID` không cần, app đã nằm trong tên miền |
     | chỉ `App-ID` | 400, `code 400401` "Api-Token is missing" |
     | `Session-Key` sai 1 ký tự | 400, `code 400303` "Session key is invalid." |

   - **Khoá chưa hết hạn sau ít nhất 155 ngày.** Lịch sử VS Code trên `.220`
     (`/root/.vscode-server/data/User/History/`) giữ các bản cũ của `/opt/BotPasteDon/.env`: bản lúc
     20/05/2026 18:30 chưa có khoá, bản 20:40 cùng ngày đã có, và giá trị trùng y hệt khoá đang dùng
     (so bằng chuỗi, không in ra). Khoá đó vẫn trả 200 ngày 22/10. Chưa biết G2G/Sendbird có thu hồi
     khoá khi nào không (ví dụ khi tài khoản đăng xuất mọi thiết bị hoặc đổi mật khẩu) — đó là giả
     định chưa kiểm. Khoá bị thu hồi thì Sendbird trả `400303`; từ 22/10 bot đọc mã này và bắn
     `OPS ALERT [g2g-sendbird-key]` (mục 2.6).

### 2.2 Bước 1 — tìm người mua

```
GET https://sls.g2g.com/order/item/{order_item_id}?seller_id={seller_id}
```

`order_item_id` phải là id đầy đủ có hậu tố `-1`; worker lấy từ `order_url` qua `_resolve_api_id`
(ERP chỉ lưu phần ngắn ở `external_order_id`). Trường dùng: `payload.buyer_id`. Không có `buyer_id`
thì bước chat hỏng và chuyển sang gửi lại (trước 22/10: `return` lặng lẽ, bước chat được tính là xong).
Lấy được rồi thì `buyer_id` được lưu trong `progress`, lượt gửi lại không gọi lệnh này nữa.

### 2.3 Bước 2 — mở (hoặc lấy lại) kênh chat

```
POST https://sls.g2g.com/chat/channel
{
  "channel_id":   "{seller_id}_{buyer_id}",
  "channel_name": "Direct Message Channel Between {seller_id} and {buyer_id}",
  "inviter_id":   "{seller_id}",
  "user_ids":     ["{seller_id}", "{buyer_id}"],
  "channel_type": "dm"
}
```

Kết quả: `payload.channel_details.channel_url`. Kênh giữa hai người là cố định, gọi lại thì nhận lại
kênh cũ.

**Đừng tự dựng tên kênh.** Thứ tự id trong `channel_url` không cố định: trong 50 kênh mới nhất (đo
22/10), 33 kênh là `g2g_dm_{buyer}_{seller}` nhưng 17 kênh là `g2g_dm_{seller}_{buyer}`. Mã cũ tự dựng
`g2g_dm_{buyer}_{seller}` khi G2G không trả `channel_url` — sai khoảng 1/3. Từ 22/10
`create_chat_channel` trả `""` trong trường hợp đó.

**Dự phòng qua Sendbird** (`G2GAPIClient._resolve_channel`, từ 22/10). `chat/channel` là bước duy nhất
của phần chat phụ thuộc máy chủ G2G, và là chỗ đã làm hỏng chat 4 đơn 16–17/10 (HTTP 500/502,
timeout). Khi nó lỗi — kể cả 401 — hoặc không trả `channel_url`, bot tìm kênh **đã có sẵn** chỉ bằng
Session-Key:

```
GET https://api-34201740-152e-401e-ad8f-5c72eeaba386.sendbird.com/v3/users/{seller_id}/my_group_channels?members_exactly_in={buyer_id}&limit=5
Session-Key: <SENDBIRD_SESSION_KEY>
```

Ưu tiên kênh `custom_type="dm"`. Thử tay 22/10 trên 5 kênh: 5/5 trả đúng một kênh, đúng `channel_url`.
Gặp 401 ở `chat/channel` thì `call_with_retry` đã vô hiệu cache JWT; bot **không gọi lại G2G** mà đi
thẳng sang Sendbird. Người mua chưa từng có kênh và G2G cũng lỗi thì bước chat hỏng, chuyển sang gửi
lại (mục 4). Chưa thử tạo kênh bằng Sendbird.

### 2.4 Bước 3 — gửi hai tin qua REST Sendbird

```
POST https://api-34201740-152e-401e-ad8f-5c72eeaba386.sendbird.com/v3/group_channels/{channel_url}/messages
Session-Key: <SENDBIRD_SESSION_KEY>
App-ID: 34201740-152E-401E-AD8F-5C72EEABA386
Content-Type: application/json
```

| Tin | Body |
|---|---|
| 1 — thẻ đơn hàng | `{"message_type":"MESG", "message":"Sold Order Item {id} https://www.g2g.com/g2g-user/sale/order/item/{id}", "user_id":"{seller_id}", "custom_type":"order-item", "data":"{\"source\":\"api\",\"order_item_id\":\"{id}\",\"buyer_id\":\"{buyer_id}\",\"seller_id\":\"{seller_id}\"}"}` |
| (nghỉ 3 giây) | |
| 2 — lời nhắn | `{"message_type":"MESG", "message":"<message.txt>", "user_id":"{seller_id}"}` |

Trường `data` là **chuỗi JSON** (Sendbird nhận string), không phải object. `App-ID` không bắt buộc
(mục 2.1), mã gửi kèm cũng không sao.

**Đọc phản hồi** (`_parse_sendbird`, từ 22/10). Sendbird báo lỗi bằng HTTP 4xx/5xx kèm
`{"error": true, "code", "message"}`. Bot coi là thành công chỉ khi HTTP 200, không có `error`, và (với
lệnh gửi) có `message_id`. Ngược lại ném `SendbirdError`; mã `4003xx` hoặc `400401` (khoá thiếu, sai,
bị thu hồi) ném `SendbirdKeyError` và bắn `OPS ALERT [g2g-sendbird-key]` (6 giờ một lần, tự gỡ khi gọi
được lại). `SendbirdError` cố ý **không** kế thừa `AuthError`, để lỗi Sendbird không làm vô hiệu JWT
G2G.

Trên Sendbird, thẻ đơn do bot gửi có `data.source = "api"`. Thẻ do nhân viên tạo khi bấm nút chat trên
trang đơn G2G có `source = "vue"` và nội dung `Initiate chat via Sold Order Item …` — dùng trường này
để phân biệt tin của bot với tin của người.

G2G không có đường gửi file trong chat ở bot: bằng chứng đi `GET order/upload_url` → POST S3 →
`POST order/item/{id}/delivery_proof`, gắn thẳng vào đơn ([proof_mechanism.md](proof_mechanism.md) mục 2).

### 2.5 Nhánh Selenium (dự phòng, `G2G_USE_API=false`)

`handle_g2g` mở trang đơn, bấm `a[data-attr='order-item-chat-btn']` → G2G mở tab chat mới → gán
`innerText` cho editor `.ProseMirror.toastui-editor-contents`, bắn sự kiện `input`/`change` → bấm
nút có `<i>send</i>` → đóng tab. Lỗi bước chat bị nuốt hoàn toàn (`except: pass`).

### 2.6 Xử lý lỗi và những chỗ không nhìn thấy được

**Từ 22/10** (`_send_chat` + `handle_g2g_api`):

- Bước chat chạy **một lượt** trong task giao hàng. Lỗi gì cũng gói thành `ChatError` kèm `progress`
  (`buyer_id`, `channel_url`, `card_sent`) và đơn chuyển sang gửi lại (mục 4).
- **Không thử lại ngay khi gặp lỗi auth.** Bản cũ thử lần 2 ngay sau `Chat auth error`, lấy JWT từ
  dịch vụ auth — mà dịch vụ auth có thể vẫn cầm đúng JWT vừa chết (bundle "fresh" 13 phút), và gọi G2G
  bằng JWT chết làm G2G đá phiên đăng nhập. Nay lượt sau cách ít nhất 60 giây, và nhờ `progress` đã có
  `buyer_id`/`channel_url` nên thường không cần gọi G2G nữa.
- Thiếu `buyer_id` hay thiếu Session-Key là lỗi (trước đây `return` lặng lẽ và bước chat được tính là
  xong).
- Đã gửi thẻ đơn thì lần sau không gửi lại thẻ, chỉ gửi lời nhắn.
- Logger `g2g.api` dùng `setup_logger`: dòng `Chat sent to buyer …`, `Uploaded … → …`, `Dùng kênh có
  sẵn trên Sendbird` hiện trong log worker. (Trước 22/10 logger này không có handler: INFO bị bỏ,
  WARNING ra stderr không có giờ. `eldo.api` cũng vậy, đã sửa cùng lúc.)

**Trước 22/10** có bốn chỗ làm chat G2G hỏng mà không ai biết: không đọc phản hồi Sendbird; thiếu
`buyer_id`/khoá thì `return` lặng lẽ; logger không có handler; khoá tĩnh có thể bị thu hồi mà không có
dấu hiệu. Số đo dưới đây là của giai đoạn đó.

**Số đo log** (22/10/2026 ~10:38, `/tmp/g2g_worker.log` trên `.220`, log bắt đầu 14/10 13:16):

| Mẫu `grep -c` | Số dòng |
|---|---|
| `Completed:` | 2.551 |
| `Chat sent` | 0 (dòng INFO của `g2g.api` bị bỏ, không có nghĩa là chưa gửi) |
| `Uploaded ` | 0 (cùng lý do) |
| `Chat (failed\|not sent\|auth error\|send failed)` (`-E`) | 9 |

Chín dòng đó thuộc 5 đơn: `…K9HZ` (16/10, `chat_channel: HTTP 500`), `…B6MD` và `…INCJ` (17/10,
`HTTP 502`), `…LUY5` (17/10, curl timeout 30 giây), `…R0GS` (20/10, `Chat auth error (attempt 1)`;
5 giây sau đơn `Completed`, không rõ lần thử thứ 2 có tới Sendbird không). Bốn đơn đầu chắc chắn
không có tin nhắn.

**Tin có tới người mua — đã kiểm 22/10/2026 ~11:00** (lệnh GET chỉ đọc lên Sendbird từ `.220`):
lấy 20 đơn `Completed:` gần nhất trong log worker (08:29–10:56), đối chiếu với thẻ đơn `source=api`
trong 50 kênh mới nhất của seller → **20/20 đơn có thẻ đơn**, gửi trước dòng `Completed:` 3–4 giây
(đúng nhịp nghỉ 3 giây giữa hai tin), và **20/20 có lời nhắn `message.txt` ngay sau thẻ**. Tức là ở
thời điểm đo, chat G2G chạy bình thường; chỉ log của bot là không cho thấy điều đó. Từ 22/10 dòng
`Chat sent to buyer` có trong log, không cần kiểm thẳng Sendbird nữa trừ khi nghi ngờ.

Cách kiểm lại (chạy trên `.220`, đọc khoá từ `.env`, không in khoá, không in tin của khách):

```
GET https://api-34201740-152e-401e-ad8f-5c72eeaba386.sendbird.com/v3/users/{seller_id}/my_group_channels?limit=50&order=latest_last_message&show_member=true
GET https://api-34201740-152e-401e-ad8f-5c72eeaba386.sendbird.com/v3/group_channels/{channel_url}/messages?message_ts={now_ms}&prev_limit=15&next_limit=0
(chỉ cần header Session-Key)
```

Tìm tin của seller có `custom_type="order-item"` và `data.source="api"`; `data.order_item_id` có hậu tố
`-1`, còn id trong log worker thì không. Lệnh này đọc hội thoại thật với khách, nên hỏi chủ trước khi
chạy. `400303` ở lệnh đầu nghĩa là khoá đã bị thu hồi.

---

## 3. Eldorado — TalkJS

### 3.1 Chuỗi xác minh: ba lớp token

| Lớp | Lấy ở đâu | Sống bao lâu | Dùng cho |
|---|---|---|---|
| 1. Token Eldorado | Seller API Bearer (hoặc gói cookie khi lùi), qua `GET localhost:8010/auth/eldo` | 900 giây, client cache tối đa 300 giây | gọi `www.eldorado.gg/api/*` |
| 2. JWT TalkJS | `GET https://www.eldorado.gg/api/conversations/me/authorize` bằng lớp 1 → `{"token": "<JWT>"}` | khoảng 1 giờ (đo 15/10) | mở phiên TalkJS, gọi backend `/say` |
| 3. `sessionId` TalkJS | khung `/session/renew` trên WebSocket | theo kết nối WS | tham số `?sessionId=` của `/say` |

- Lớp 1: chi tiết ở [marketplace_auth.md](marketplace_auth.md) mục "Seller API mode". Từ 14/10/2026
  bot chạy bằng Bearer.
- Lớp 2: route `conversations/me/authorize` **nằm ngoài Swagger** của Seller API nhưng nhận Bearer
  (200, thử 14/10 và chạy thật 15/10). Claim `sub` của JWT TalkJS là id người dùng TalkJS, trùng
  seller id của tài khoản Eldorado (`EldoradoAPIClient.get_talkjs_user_id`).
- Seller API **không có route chat nào**. Swagger 11/10 chỉ có
  `POST boostingOffers/boostingRequests/{id}/createConversationForSeller` (mở hội thoại cho yêu cầu
  boosting) và nhóm `notifications/me`. Chat đi được là nhờ route web ở trên cộng TalkJS trực tiếp.

### 3.2 Id hội thoại

`GET https://www.eldorado.gg/api/orders/me/{orderId}` → trường `talkJsConversationId` (route ngoài
Swagger). Worker lấy một lần ở bước `deliver` rồi dùng lại cho bước sau; bước `deliver` đã bỏ qua thì
gọi lại để lấy.

TalkJS có **hai dạng id**:

- id ngoài: `talkJsConversationId`, user id trong JWT. Dùng trên WebSocket.
- id nội bộ: `sha1(id_ngoài)[:20]` (10 byte đầu, hex). Dùng trong URL và payload của backend `/say`.
  `nymId` = id nội bộ của user + hậu tố `_n`. Hàm `talkjs_internal_id`; vector khoá trong
  `tests/test_talkjs.py` (`python tests/test_talkjs.py`, không cần mạng). Test đỏ thì đừng sửa
  vector — tìm ai đụng vào hàm băm.

### 3.3 Mở phiên WebSocket

```
wss://realtime.talkjs.com/v1/49mLECOW/realtime/{talkjs_user_id}
Origin: https://www.eldorado.gg
User-Agent: <DEFAULT_USER_AGENT>
(ping 30 giây, timeout ping 10 giây)
```

Mọi khung là mảng JSON. Yêu cầu: `[request_id, method, path, body, headers]`. Trả lời:
`[request_id, status, body]`. `_receive_loop` ghép trả lời với yêu cầu theo `request_id`.

```json
→ [1, "POST", "/session/renew", {"token": "<JWT TalkJS>"}, {}]
← [1, 200, {"sessionId": "...", ...}]
```

Trả lời đầy đủ được log ở dòng `Session renew response:` (500 ký tự đầu). Không có `sessionId` thì
gửi chữ vẫn được nhưng **không gửi đính kèm được**.

### 3.4 Gửi chữ

```json
→ [n, "POST", "/conversations/{talkJsConversationId}/messages", {"type": "UserMessage", "text": "<message.txt>"}, {}]
← [n, 200, {"id": "msg_...", ...}]
```

Chờ trả lời tối đa 30 giây; hết giờ hoặc status khác 200 thì `send_text_message` trả `None`.

Gửi file qua WebSocket thì TalkJS đòi `content:[{type:"file", fileToken}]`, mà route cấp `fileToken`
chỉ mở cho secret key phía máy chủ của Eldorado. Vì vậy đính kèm phải đi đường khác (mục 3.5).

### 3.5 Gửi đính kèm: Firebase rồi `/say`

**a) Đẩy file lên Firebase Storage của TalkJS** (bucket `klets-3642`, `TalkJSClient.upload_file`).
Ba lệnh dưới đây **không kèm header xác thực nào**.

```
1. POST https://firebasestorage.googleapis.com/v0/b/klets-3642/o?name=<path đã urlencode>
   x-goog-upload-protocol: resumable
   x-goog-upload-command: start
   x-goog-upload-header-content-length: <số byte>
   x-goog-upload-header-content-type: <mime>
   x-firebase-storage-version: webjs/9.23.0
   Content-Type: application/json
   {"name": "<path>", "cacheControl": "private, max-age=86400", "contentType": "<mime>",
    "contentDisposition": "inline; filename*=utf-8''<tên>;", "metadata": {"draft": "true"}}
   → header x-goog-upload-url

2. POST <x-goog-upload-url>
   x-goog-upload-command: upload, finalize
   x-goog-upload-offset: 0
   <nội dung file>
   → 200 JSON có downloadTokens

3. PATCH https://firebasestorage.googleapis.com/v0/b/klets-3642/o/<path>?alt=media&token=<downloadTokens>
   {"metadata": {"draft": null}}          (lỗi bước này chỉ cảnh báo)
```

Ba chi tiết phải khớp với client TalkJS thật, nếu không file lên được nhưng hiển thị sai:

- `path` = `user_files/49mLECOW/{uuid4 không gạch}/{tên file}`. Đoạn giữa là UUID ngẫu nhiên mỗi lần,
  không phải conversation id.
- `contentDisposition: inline` cho `mp4 webp avi webm wav mp3 oga aac opus gif jpeg jpg png bmp pdf
  txt md log` để người mua xem ngay trong chat; đuôi khác dùng `attachment` (trình duyệt tải về).
- Tên file: bỏ dấu tổ hợp sau khi tách NFD, giống client (`đơn hàng.mp4` → `đon hang.mp4`). Tên hiển
  thị lấy từ `name` trong dict file ERP gửi, vì file tạm `/tmp/erp_evidence_XXXX.mp4` vô nghĩa với
  người mua.

**b) Báo TalkJS tạo tin đính kèm** (`TalkJSClient.send_attachment`):

```
POST https://app.talkjs.com/api/v0/49mLECOW/say/{sha1(talkJsConversationId)[:20]}/?sessionId={sessionId}
Authorization: bearer <JWT TalkJS>
x-talkjs-client-build: frontend-release-c964445
Origin: https://app.talkjs.com
Accept: application/json
Content-Type: application/json

{
  "idempotencyKey": "<uuid4 hex>",
  "entityTree": [],
  "received": false,
  "custom": {},
  "nymId": "{sha1(talkjs_user_id)[:20]}_n",
  "attachment": {
    "type": "file", "url": "<URL Firebase ở bước a3>", "size": <byte>, "filename": "<tên>",
    "subtype": "video|image|audio", "width": <px>, "height": <px>, "duration": <giây, làm tròn>
  },
  "location": null,
  "referencedMessageId": null
}
→ 200 {"ok": "<message id>"}
```

TalkJS nhận URL Firebase rồi tự sinh `fileToken`. `subtype`, `width`, `height`, `duration` đọc bằng
`shared/media_probe.py` (đọc header file, không cần ffmpeg); thiếu thì bỏ trường, không phải lỗi.

Lỗi hay gặp, cả hai xảy ra trước khi tin được tạo nên không sinh rác trong chat:

| Lỗi | Nguyên nhân |
|---|---|
| `404 "Sender does not exist"` | sai `nymId` |
| `400 "conversation_not_found"` | dùng id ngoài trong URL thay cho id nội bộ |

Header `x-talkjs-client-build` ghim bản frontend TalkJS. Khi backend bắt đầu từ chối, lấy bản mới:
tải `https://app.talkjs.com/app/49mLECOW/user/0/inbox/chats`, đọc thẻ
`<script src=".../browser-bundle-release-<hash>.js">`, sửa `TalkJSConfig.client_build`.

### 3.6 Xử lý lỗi trong worker

- `talkjs_client` là **một đối tượng dùng chung** cho mọi task của worker. JWT TalkJS và kết nối WS
  được giữ lại giữa các đơn; chỉ khi một lần gửi thất bại mới đóng WS, xin JWT mới qua
  `conversations/me/authorize` rồi nối lại.
- **Bằng chứng** (`_talkjs_send_attachment_with_retry`): mỗi file thử tối đa 3 lần, mỗi lần thử upload
  lại file lên Firebase. Không đủ file → đơn `FAILED` với `PROOF_PENDING:` và `retry_data` chỉ còn bước
  `proofs`; vòng `recover_auth_failed` (60 giây một lượt) gửi lại, tối đa 5 lượt, sau đó đổi thành
  `PROOF_STUCK:` để người gửi tay bằng nút "Đã giao" trên ERP. (Có từ commit `533b305`.)
- **Lời nhắn** (`_talkjs_send_with_retry`): thử tối đa 3 lần, cùng cách làm mới token. Hỏng hết (hoặc
  đơn không có `talkJsConversationId`) thì `handle_eldo_api` trả lỗi chat; nếu bằng chứng đã lên, đơn
  `COMPLETED` + `CHAT_PENDING` và được gửi lại riêng lời nhắn (mục 4). Trước 22/10 lời nhắn hỏng là
  mất luôn. `message.txt` không tồn tại thì bước chat được tính là xong.
- Lỗi 401/403 ở lệnh gọi Eldorado (lớp 1) → đơn `FAILED` với `AUTH_EXPIRED:`, tự thử lại khi auth
  hồi phục.

### 3.7 Nhánh Selenium (dự phòng, `ELDO_USE_API=false`)

- JWT TalkJS lấy từ tham số `authToken=` trong `src` của `iframe[name*='talkjs']`; user id từ claim
  `sub` (hoặc tham số `id=`).
- Id hội thoại: tham số `syncPlease` của iframe (base64 JSON, trường `externalConversationId`), không
  có thì tìm `"conversationId":"<uuid>"` trong mã trang.
- File gửi bằng `input[type=file]` bên trong iframe rồi bấm `.confirm-send`; lời nhắn vẫn đi WebSocket
  như mục 3.4.

### 3.8 Đã chạy thật

- **15/10/2026**, đơn `19183B38-FD3E-4B26-E157-08DF29E7A504` (D4 item, $14.99), chế độ Seller API:
  05:51:12 `fast_delivery` → `PUT orders/me/{id}/deliver` OK; 05:51:37 task gửi bằng chứng →
  `/session/renew` → tải video 7,2 MB lên Firebase → `/say` → tin nhắn → `Completed`.
- **22/10/2026** 10:10:50–51 (`/tmp/eldo_worker.log`): `Đã lên Firebase: bandicam … .mp4 (1955712
  bytes)` → `Đã gửi đính kèm … (video) → -P4W_khN4vi_dTB-QpHd` → `Message sent:
  msg_5kjRhWjAR5t2YO9VxSbW7k`.
- Logger `talkjs` có handler (`setup_logger`), nên mọi bước TalkJS hiện trong `/tmp/eldo_worker.log`
  với tiền tố `[talkjs]`. Logger `eldo.api` có handler từ 22/10 (dòng `Delivered via API`).

### 3.9 Quy định của Eldorado

Quy định seller của Eldorado ghi rằng tin gửi qua API có thể **không được tính là phản hồi ban đầu**
nếu hội thoại không có trao đổi thật, và đơn vẫn có thể bị huỷ
(`https://support.eldorado.gg/en/articles/8408997-seller-rules`). Tin tự động của bot không thay được
việc nhân viên trả lời khách.

---

## 4. Gửi lại lời nhắn: `CHAT_PENDING` (từ 22/10/2026)

Áp dụng cho cả hai sàn, mã chung ở `workers/base_worker.py`.

- **Hàng đã giao nên đơn vẫn `COMPLETED`.** Bước chat hỏng thì ghi
  `error_message = "CHAT_PENDING:luot <n>: <lý do>"` và `retry_data = {"chat": {...}}`. Không dùng
  `FAILED`/`RETRY_PENDING` vì đơn đã xong trên sàn và với ERP. Dashboard tự hiện đơn này trong danh
  sách lỗi 2 ngày gần nhất (nó liệt kê mọi đơn có `error_message`).
- **Vòng hồi phục sẵn có của mỗi worker** (60 giây một lượt: `recover_pending_retries` ở G2G,
  `recover_auth_failed` ở Eldorado) quét đơn `COMPLETED` có tiền tố `CHAT_PENDING:` đã tới hạn và gửi
  lại **riêng lời nhắn**, tuần tự từng đơn. Không bấm giao, không gửi lại bằng chứng.
- **Giãn cách:** lượt 2 sau 1 phút, lượt 3 sau 5 phút, lượt 4 sau 30 phút, lượt 5 sau 60 phút. Hỏng ở
  lượt 5 thì đổi thành `CHAT_STUCK:` và bắn `OPS ALERT [chat-stuck:<sàn>:<đơn>]` — phải nhắn tay trên
  sàn.
- **G2G đi tiếp từ chỗ dừng:** `retry_data.chat` giữ `api_id` và `progress` (`buyer_id`, `channel_url`,
  `card_sent`). Có đủ `buyer_id` + `channel_url` thì lượt gửi lại không gọi G2G lần nào, chỉ gọi
  Sendbird; đã gửi thẻ đơn thì không gửi lại thẻ.
- **Eldorado** gọi lại `GET orders/me/{id}` để lấy `talkJsConversationId` rồi gửi qua WebSocket TalkJS.
- Lấy auth lỗi (dịch vụ auth chưa sẵn sàng) thì dừng lượt quét đó, không tính là một lượt hỏng.
- **G2G từ chối JWT (401/403) trong lúc gửi lại thì dừng cả lượt quét** sau khi ghi lượt hỏng cho đơn
  đó: đơn kế tiếp sẽ lấy lại đúng JWT vừa chết từ dịch vụ auth, gọi G2G lần nữa là có thể bị đá phiên.
- Gửi xong thì xoá dấu (`error_message = ""`, `retry_data = NULL`). ERP gửi lại task giao cho đơn đang
  `CHAT_PENDING` mà lần này chat thành công thì `complete_order` cũng xoá dấu, tránh gửi trùng.
- `message.txt` bị xoá trong lúc chờ thì bỏ lượt gửi lại và xoá dấu.
- **Có thể trùng tin:** nếu Sendbird/TalkJS đã nhận tin nhưng câu trả lời bị mất (timeout), lượt sau
  gửi lại tin đó. Chấp nhận — thừa một lời cảm ơn tốt hơn thiếu.

Xem nhanh các đơn đang chờ:

```bash
cd /opt/BotPasteDon && venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect("file:data/orders.db?mode=ro", uri=True)
for r in c.execute("SELECT platform, order_id, error_message, updated_at FROM orders "
                   "WHERE status='COMPLETED' AND (error_message LIKE 'CHAT_PENDING:%' "
                   "OR error_message LIKE 'CHAT_STUCK:%') ORDER BY updated_at DESC LIMIT 20"):
    print(*r)
EOF
```

---

## 5. Hằng số phải sửa khi sàn đổi

| Hằng | Giá trị | Ở đâu | Dấu hiệu đã lỗi thời |
|---|---|---|---|
| Sendbird App ID | `34201740-152E-401E-AD8F-5C72EEABA386` | `shared/g2g_api.py` `SENDBIRD_APP_ID` | Sendbird trả lỗi app |
| Session-Key Sendbird | 40 ký tự, dùng từ 20/05/2026 | `.env` `SENDBIRD_SESSION_KEY` | `OPS ALERT [g2g-sendbird-key]` trong `/tmp/g2g_worker.log` (Sendbird trả `4003xx`). Thay khoá: lấy từ HAR khi mở trang chat G2G, sửa `.env`, restart **chỉ** `worker_g2g` — khoá không qua dịch vụ auth nên KHÔNG restart auth |
| TalkJS app id | `49mLECOW` | `workers/talkjs_client.py` `TalkJSConfig.app_id` | WS không mở được |
| TalkJS client build | `frontend-release-c964445` | `TalkJSConfig.client_build` | `/say` từ chối |
| Firebase bucket | `klets-3642` | `TalkJSConfig.firebase_url` | bước a1 không trả `x-goog-upload-url` |
| Firebase SDK header | `webjs/9.23.0` | `upload_file` | như trên |

---

## 6. Chẩn đoán nhanh trên `.220`

```bash
# Eldorado: các bước TalkJS của đơn gần nhất
grep '\[talkjs\]' /tmp/eldo_worker.log | tail -20
grep -E 'PROOF_PENDING|PROOF_STUCK|thiếu bằng chứng|Gửi đính kèm lỗi|WS msg failed' /tmp/eldo_worker.log | tail

# G2G: tin đã gửi, kênh lấy qua Sendbird, chat chờ gửi lại
grep -E 'Chat sent to buyer|Dùng kênh có sẵn|Chat chưa gửi được|Gửi lại chat' /tmp/g2g_worker.log | tail

# Cảnh báo: khoá Sendbird hỏng, chat hết lượt gửi lại
grep -h 'OPS ALERT \[\(g2g-sendbird-key\|chat-stuck\)' /tmp/g2g_worker.log /tmp/eldo_worker.log | tail

# Token lớp 1 của Eldorado còn sống không
curl -s localhost:8010/health | python3 -m json.tool | grep -E 'auth_mode|bearer'
```

---

## 7. Chỗ hở: đã vá và còn lại

**Đã vá ngày 22/10/2026** (test cô lập: `tests/test_marketplace_chat.py`, 16 phép kiểm):

| Việc | Đã làm |
|---|---|
| G2G không kiểm kết quả Sendbird | `_parse_sendbird`: lỗi → `SendbirdError`; khoá hỏng → `SendbirdKeyError` + ops alert (mục 2.4) |
| Logger `g2g.api`, `eldo.api` không có handler | dùng `setup_logger` |
| Khoá Sendbird bị thu hồi thì chat chết im lặng | đọc mã `4003xx`/`400401` → `OPS ALERT [g2g-sendbird-key]` |
| `POST chat/channel` lỗi làm mất chat; tên kênh tự dựng sai 1/3 | lùi sang Sendbird `members_exactly_in`; bỏ tên tự dựng (mục 2.3) |
| Chat hỏng không được gửi lại (cả hai sàn) | `CHAT_PENDING` + vòng hồi phục (mục 4) |
| Thử lại chat G2G ngay sau lỗi auth (nguy cơ G2G đá phiên) | bỏ; lượt sau cách ≥60 giây và thường không cần gọi G2G (mục 2.6) |
| `full_delivery` chết ở cả `g2g_api.py` và `eldo_api.py` (bản Eldo gọi hàm không tồn tại) | xoá |
| `seller_api_migration_plan.md` bảng 2.3, `proof_mechanism.md` mục 3 ghi sai | sửa |

**Còn lại:**

| Việc | Ảnh hưởng | Ghi chú |
|---|---|---|
| Khoá Sendbird vẫn tĩnh, thay tay | bị thu hồi thì chat G2G ngừng tới khi có người thay | nay có cảnh báo; chưa tìm route G2G cấp khoá để dịch vụ auth tự lấy |
| Người mua chưa có kênh + `chat/channel` lỗi | lượt đó hỏng, chờ gửi lại | chưa thử tạo kênh bằng Sendbird |
| Có thể trùng tin khi câu trả lời bị mất | khách nhận hai lời cảm ơn | chấp nhận |
| Scanner, status_sync chưa restart sau đổi logger | dòng INFO của `g2g.api`/`eldo.api` trong log của chúng chỉ có từ lần restart sau | không ảnh hưởng chức năng |
| Nhánh Selenium (`handle_g2g`, `handle_eldorado`) | chat lỗi vẫn bị nuốt | không chạy trên `.220` |

---

## 8. File tham chiếu

| File | Vai trò |
|---|---|
| [shared/g2g_api.py](../shared/g2g_api.py) | `create_chat_channel`, `find_chat_channel`, `send_chat_message`, `_parse_sendbird`, `_resolve_channel`, `_send_chat`, `ChatError`, `SendbirdError` |
| [workers/base_worker.py](../workers/base_worker.py) | `read_message`, `mark_chat_pending`, `due_chat_retries`, `complete_order` |
| [tests/test_marketplace_chat.py](../tests/test_marketplace_chat.py) | Sendbird/G2G/TalkJS giả + DB tạm: đọc phản hồi, kênh dự phòng, `CHAT_PENDING` ở hai worker |
| [shared/g2g_auth.py](../shared/g2g_auth.py) | `G2GAuthData` (JWT, cookie, `sendbird_session_key`) |
| [shared/config.py](../shared/config.py) | `SENDBIRD_SESSION_KEY` |
| [workers/g2g_worker.py](../workers/g2g_worker.py) | `handle_g2g_api` bước `chat`, `_resend_g2g_chat`, `recover_pending_retries`; `handle_g2g` nhánh Selenium |
| [shared/eldo_api.py](../shared/eldo_api.py) | `get_order_detail` (`talkJsConversationId`), `get_talkjs_auth`, `get_talkjs_user_id` |
| [workers/talkjs_client.py](../workers/talkjs_client.py) | WebSocket, `send_text_message`, `upload_file`, `send_attachment`, `talkjs_internal_id` |
| [workers/eldorado_worker.py](../workers/eldorado_worker.py) | `handle_eldo_api`, `_talkjs_send_with_retry`, `_talkjs_send_attachment_with_retry`, `_resend_eldo_chat`, `recover_auth_failed` |
| [shared/media_probe.py](../shared/media_probe.py) | kích thước, thời lượng video/ảnh cho đính kèm |
| [tests/test_talkjs.py](../tests/test_talkjs.py) | vector băm id TalkJS, `media_probe` |
| [message.txt](../message.txt) | lời nhắn gửi khách |
