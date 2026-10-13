# Kế hoạch: chuyển phần Eldorado của BotPasteDon sang Seller API

Lập ngày 2026-10-11. Cập nhật 2026-10-14.

| Bước | Trạng thái |
|---|---|
| 0 — Chủ tạo credential | xong 14/10 (hạn 365 ngày, nằm trong `.env` trên `.220`, quyền 600) |
| 1 — Dịch vụ auth phát token | **xong**, deploy 14/10 04:36 (`bf12ea7`) |
| 2 — Client gắn header Bearer | **xong**, deploy 14/10 04:36 (`bf12ea7`) |
| 3 — Đổi sang route đơn hàng `v1` | chưa (cố ý để sau Bước 4) |
| 4 — Thử trên một đơn Paid thật | **một phần**: các lệnh đọc đã thử thật 14/10; deliver và gửi TalkJS chờ đơn thật đầu tiên |
| 5 — Bật hẳn, dọn dẹp | **đã bật `seller_api` 14/10 05:24**; dọn dẹp sau hai tuần chạy ổn |

Kết quả thử thật ngày 14/10 (credential thật, gọi bằng chính `EldoradoAPIClient` trên `.220`):
`statesCount`, `users/me`, `library`, danh sách đơn theo trạng thái (Paid/Delivered/Received/
Completed/Disputed), chi tiết một đơn, **`v1/orders/me/{id}/delivery-details/{detailId}`** (trên một
đơn Delivered, trả đúng giá trị) và `conversations/me/authorize` đều 200 với header chỉ gồm
`accept`, `authorization`, `user-agent`. Auth log: `Seller API token OK (expires in 899s, probe HTTP
200)`, hạn credential đọc được (364,8 ngày).

Tài liệu này dành cho người nhận việc sửa repo này. Phần G2G không liên quan và không đổi.
Cơ chế xác thực hiện hành được mô tả ở `docs/marketplace_auth.md`; kế hoạch này thay phần
Eldorado của tài liệu đó khi làm xong.

---

## 1. Vì sao làm

**Hiện trạng.** Dịch vụ `auth.main` (cổng 8010) giữ ba profile trình duyệt
(`chrome_profile_eldo`, `_bak1`, `_bak2`), đọc `__Host-EldoradoRefreshToken` trong
`cookies.sqlite` rồi gọi `POST /api/authentication/refreshTokens` để lấy IdToken mới. Scanner,
worker và status_sync lấy gói cookie qua `GET /auth/eldo` và gửi kèm mọi lệnh gọi. RefreshToken
có hạn 30 ngày; hết hạn thì phải có người đăng nhập tay qua VNC. Restart nhầm lúc phiên chỉ
còn trong RAM là mất phiên (xem cảnh báo `refresh_token_expires_in` trong `docs/operations.md`).

**Cái mới.** Đầu tháng 10/2026 Eldorado mở Seller API: seller tạo một cặp Client ID và Client
secret (hạn tối đa 365 ngày), bot đổi cặp đó lấy token sống 15 phút và gọi API bằng header
`Authorization: Bearer`. Không cần trình duyệt, không cần cookie.

**Được gì.** Bỏ việc đăng nhập tay hằng tháng, bỏ Camoufox cho Eldorado, và restart dịch vụ
auth không còn nguy cơ giết phiên.

---

## 2. Điều đã kiểm chứng (đo ngày 11/10/2026)

Đo bằng một credential thử hạn 1 ngày của tài khoản GegeTeam, gọi từ một script Python thường
(không trình duyệt, không cookie, không `curl_cffi`).

### 2.1 Xác thực

| Việc | Kết quả |
|---|---|
| Lấy token | `POST https://www.eldorado.gg/api/authentication/seller/token`, body JSON `{"clientId": "...", "clientSecret": "..."}` → `{"accessToken": "...", "expiresIn": 899, "tokenType": "Bearer"}` |
| Tuổi thọ token | 900 giây. Không có refresh token; hết hạn thì xin lại y như lần đầu |
| Gọi API | header `Authorization: Bearer <accessToken>`; không cần `Cookie`, `x-xsrf-token`, `nsure-device-id`, `x-client-build-time` |
| Tên miền | luôn dùng `www.eldorado.gg` (repo đã dùng đúng). Tên miền trần trả 301 và nhiều thư viện đổi POST thành GET |
| Nhiều token cùng lúc | xin 2 token cách nhau 1 giây, cả hai cùng gọi được |
| Trong token | JWT có claim `userId` là id của seller |

### 2.2 Các route BotPasteDon đang gọi (`shared/eldo_api.py`)

"Trong Swagger" nghĩa là route có trong đặc tả chính thức của Seller API. Route ngoài Swagger
hiện vẫn chạy bằng token nhưng Eldorado không cam kết giữ.

| Hàm | Route đang gọi | Trong Swagger | Thử bằng token |
|---|---|---|---|
| `get_pending_orders`, `list_orders_by_state` | `GET orders/me/seller/orders` | có, nhưng **đã đánh dấu lỗi thời**; bản thay là `GET v1/orders/me/seller/orders` | cả hai bản trả 200 với đúng bộ tham số bot đang gửi |
| `deliver_order` | `PUT orders/me/{id}/deliver` | có, **đã đánh dấu lỗi thời**; bản thay là `PUT v1/orders/me/{id}/deliver` | chưa thử (lệnh này đổi trạng thái đơn thật) |
| `get_order_detail` | `GET orders/me/{id}` | không | 200 |
| `get_states_count` | `GET orders/me/statesCount` | không | 200 |
| `get_delivery_detail_value` | `GET v1/orders/me/{id}/delivery-details/{detailId}` | không | 200 (thử 14/10 trên một đơn Delivered) |
| `get_talkjs_auth` | `GET conversations/me/authorize` | không | 200, trả token TalkJS |
| `get_user_profile` | `GET users/me` | không | 200 |
| `_fetch_game_library` | `GET library?locale=en-US` | không | 200 |

Seller API còn có thêm các route đơn hàng bot chưa dùng: `POST v1/orders/me/{id}/cancel`,
`POST v1/orders/me/{id}/extend-delivery-time`, `POST v1/orders/me/{id}/delivery-details/correction`,
và `GET v1/orders/me/sold-order-data` (CSV có phí sàn và thu nhập ròng từng đơn).

Lưu ý về tài liệu của Eldorado: trang dashboard in ví dụ `PUT /api/orders/me/{orderId}/delivered`.
Route đó trả 404. Route đúng là `.../deliver`.

---

## 3. Việc cần làm

Mỗi bước là một commit riêng, triển khai bằng `scripts/deploy_git.py <service>` và kiểm xong mới
sang bước sau.

### Bước 0 — Chủ tạo credential

1. Vào `https://www.eldorado.gg/dashboard/seller-api`, tạo một credential hạn 365 ngày.
2. Lưu Client ID và secret vào trình quản lý mật khẩu. Không dán vào chat, không chụp ảnh.
3. Để trống ô credential thứ hai (tài khoản chỉ được 2), dành cho lúc xoay credential.

Cùng một credential dùng chung cho ba dự án (BotPasteDon, bot đạp giá `.199`, item bot `.221`).
Mỗi dự án tự xin token của mình.

### Bước 1 — Dịch vụ auth phát token

| File | Việc |
|---|---|
| `shared/config.py` | thêm `ELDO_AUTH_MODE` (`cookie` mặc định, hoặc `seller_api`), `ELDO_SELLER_CLIENT_ID`, `ELDO_SELLER_CLIENT_SECRET` đọc từ `.env` |
| `auth/main.py` | trong `EldoAuth`, thêm đường lấy token: gọi route ở mục 2.1, giữ token trong RAM, xin lại khi còn dưới 120 giây. Ở chế độ `seller_api`, `handle_auth_eldo` trả `{"auth_mode": "seller_api", "bearer_token": ..., "expires_at": ..., "user_agent": ..., "seller_id": ...}` và không đụng tới Camoufox hay `cookies.sqlite` |
| `auth/main.py` (`handle_health`) | thêm `eldo_auth_mode` và `seller_credential_expires_in`. Ở chế độ `seller_api`, `refresh_token_expires_in` bằng `null` là bình thường và restart an toàn |

Xong khi: `curl localhost:8010/auth/eldo` trả token hợp lệ, và không có tiến trình Camoufox nào
được mở cho Eldorado.

Đã làm 14/10, khác bản kế hoạch gốc ở ba điểm:

- **Tự lùi về cookie.** Ở chế độ `seller_api`, nếu Eldorado không cấp token thì auth bắn
  `OPS ALERT [eldo-seller-api]` và phát gói cookie như cũ, thay vì để bot mù. Token đang cầm còn
  sống hơn 60 giây thì dùng tiếp, chưa lùi.
- **Token chỉ được phát khi còn hơn 360 giây** (client cache 300 giây cộng 60 giây đệm), nên auth
  xin token mới khoảng 9 phút một lần.
- **Trường `/health` mới** là `auth_mode`, `bearer`, `bearer_expires_in`,
  `seller_credential_expires_in` (không có `eldo_auth_mode`). Hạn credential đọc qua
  `GET /api/client-credentials`, cảnh báo `eldo-seller-cred-expiring` khi còn dưới 14 ngày.

Kiểm thử: chạy mã thật với một máy chủ Eldorado giả trên localhost, 44 phép kiểm đạt (token thành
công, 401, 500, mất kết nối, probe 403/500, token hết hạn, thiếu credential, `/health`, client).
Chưa chạy với credential thật.

### Bước 2 — Client gắn header Bearer

| File | Việc |
|---|---|
| `shared/eldo_auth.py` | `EldoAuthData` thêm trường `bearer_token`; `build_headers` đặt `Authorization: Bearer ...` khi có token và bỏ `Cookie`, `x-xsrf-token`, `nsure-device-id`, `x-client-build-time`. `EldoAuthManager.TTL` đang là 300 giây, nhỏ hơn tuổi thọ token nên giữ nguyên |
| `shared/eldo_api.py` | gặp 401 thì `invalidate()` rồi lấy lại từ dịch vụ auth (đường xử lý `AuthError` sẵn có) |

Xong khi: scanner chạy ở chế độ `seller_api` đọc được danh sách đơn Paid, status_sync đọc được
`statesCount`.

Đã làm 14/10: `EldoAuthData` có `bearer_token` và `expires_at`; `build_headers` chỉ gửi
`user-agent`, `accept`, `authorization` khi có token; `_fetch` không cache quá hạn token.
`shared/eldo_api.py` không sửa, vì đường xử lý `AuthError` sẵn có đã `invalidate()` khi gặp 401.

### Bước 3 — Đổi sang route đơn hàng `v1`

| Hàm trong `shared/eldo_api.py` | Đổi thành |
|---|---|
| `get_pending_orders`, `list_orders_by_state` | `GET {BASE}/v1/orders/me/seller/orders`, tham số giữ nguyên |
| `deliver_order` | `PUT {BASE}/v1/orders/me/{order_id}/deliver` |

Bản `v1` của lệnh deliver trả `OrderGetSellerPrivateDTO` thay cho `...DTOV1`. Kiểm lại những
trường worker đọc từ kết quả trả về.

Xong khi: so 20 đơn gần nhất đọc qua route cũ và route `v1`, các trường bot dùng giống nhau.

### Bước 4 — Thử trên một đơn Paid thật trước khi bật hẳn

Ba việc dưới đây chưa thử được bằng token, phải thử trên một đơn thật có người theo dõi:

1. `get_delivery_detail_value`: đọc được thông tin giao hàng khách nhập.
2. `deliver_order` qua route `v1`: đơn chuyển sang Delivered.
3. Gửi tin nhắn và đính kèm bằng chứng qua TalkJS bằng token TalkJS lấy từ
   `conversations/me/authorize`.

Nếu việc 1 hoặc 3 bị từ chối (route nằm ngoài Swagger), giữ đường cookie riêng cho đúng lệnh gọi
đó và ghi lại ở mục 5.

Xong khi: một đơn thật đi trọn vòng quét → paste ERP → giao → bằng chứng → deliver ở chế độ
`seller_api`, không có lệnh gọi nào dùng cookie.

### Bước 5 — Bật hẳn, rồi dọn sau hai tuần

- Đặt `ELDO_AUTH_MODE=seller_api` trên `.220`.
- Sau hai tuần chạy ổn: gỡ ba profile Eldorado, `eldo_login.py`, `scripts/open_eldo_vnc*.py` và
  phần cảnh báo đăng nhập lại; viết lại phần Eldorado của `docs/marketplace_auth.md` và
  `docs/operations.md`.

---

## 4. Cách lùi

Đặt lại `ELDO_AUTH_MODE=cookie` rồi restart `auth.main`. Ngoài ra auth tự lùi về cookie mỗi khi
không lấy được token. Cả hai đường lùi đều cần ít nhất một profile cookie còn sống trong hai tuần đầu. Lúc lập kế hoạch này (theo ghi chép ngày 05/10):
`chrome_profile_eldo` hết hạn 15/10, `chrome_profile_eldo_bak1` hết hạn 04/11.

---

## 5. Rủi ro

- **Sáu trong tám route bot dùng nằm ngoài Swagger** (xem bảng 2.2). Chúng chạy bằng token hôm
  nay nhưng có thể bị khoá mà không báo trước. Scanner và worker phải báo lỗi rõ khi gặp 401, 403
  hoặc 404 ở các route này, không được lặng lẽ bỏ qua đơn.
- **Lộ secret là mất quyền điều khiển offer và đơn.** Secret chỉ nằm trong `.env` trên `.220`,
  quyền đọc 600, không vào git.
- **Credential hết hạn thì bot ngừng quét đơn.** Watchdog phải cảnh báo trước ngày hết hạn dựa
  trên `seller_credential_expires_in`.
- **Tin nhắn tự động có thể không đủ.** Quy định seller của Eldorado ghi: tin nhắn gửi qua API có
  thể không được tính là phản hồi ban đầu nếu chat không có trao đổi thật, và đơn vẫn có thể bị huỷ.
- **Không có môi trường thử.** Mọi lệnh gọi đều chạm đơn thật.

---

## 6. Tham chiếu

- Đặc tả Swagger và số đo quota ngày 11/10/2026 được giữ ở repo bot đạp giá
  (`GegeTeamCode/BotDapLowestPoE`), thư mục `docs/eldorado-seller-api/` (`swagger.json`,
  `do-quota-20261011.txt`, `plan.md`).
- Trang tạo credential: `https://www.eldorado.gg/dashboard/seller-api`
- Bài hướng dẫn của Eldorado: `https://support.eldorado.gg/en/articles/16127288-how-the-seller-api-works`
- Quy định seller: `https://support.eldorado.gg/en/articles/8408997-seller-rules`
- Hỗ trợ của Eldorado: `api@eldorado.gg` (gửi Client ID, không bao giờ gửi secret)
