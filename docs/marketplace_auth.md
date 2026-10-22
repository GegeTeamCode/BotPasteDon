# Marketplace Auth Token Reference

Tài liệu này document co che authentication cua hai marketplace **Eldorado.gg** va **G2G.com** o muc đầy đủ de tuong lai khong phai reverse-engineer lai. Doi tuong su dung: AI agent hoac engineer refactor cac bot 220/223/229 hoac viet client moi.

Hai marketplace dung **kien truc khac nhau** (Cognito broker vs self-issued JWT) nhung cung pattern: short-lived access token + long-lived refresh token. Bot dung cung **two-tier strategy** — backend refresh nhanh, browser fallback cham.

**Tu 2026-10-14, Eldorado chay bang Seller API** (client credentials → Bearer token, khong browser). Duong cookie mo ta duoi day chi con la duong lui. Tham chieu day du cua Seller API: muc [Eldorado Seller API — tham chiếu](#eldorado-seller-api--tham-chiếu). Token rieng cho chat (TalkJS, Sendbird): [marketplace_chat.md](marketplace_chat.md).

**Consumers cua auth service**: workers (`workers/g2g_worker.py`, `workers/eldorado_worker.py`) khi giao hang; scanners (`scanners/{g2g,eldorado}_scanner_api.py`) khi poll pending orders; **status_sync** (`status_sync/{g2g,eldo}_sync.py`) khi poll counts + state lists moi 30 phut. Tat ca consume qua `shared/{g2g,eldo}_auth.py` (5-min cache, auto-invalidate khi 401).

---

## Eldorado.gg

### Token lifecycle

| Token | Vi tri | TTL | Vai tro |
|---|---|---|---|
| `__Host-EldoradoIdToken` | Cookie | ~1h | JWT chua user identity. Send trong Authorization header tu auto. |
| `__Host-EldoradoRefreshToken` | Cookie | ~30 ngay (sliding) | Bearer cua refresh request. Server tu doc tu Cookie header. |
| `__Host-XSRF-TOKEN` | Cookie | session | Mirror trong header `x-xsrf-token` (CSRF protection). |
| `__Host-Eldorado*` (khac) | Cookie | various | Session bookkeeping (page id, A/B test, etc.) |

Cookies tat ca deu `__Host-` prefix → strict same-origin (no subdomain sharing).

### Backend Refresh

**Endpoint**:
```
POST https://www.eldorado.gg/api/authentication/refreshTokens
```

**Request**:
- Body: `{}` (literal empty JSON object)
- Cookies: forward toan bo bundle (server tu doc `__Host-EldoradoRefreshToken`)
- Headers required:
  ```
  origin: https://www.eldorado.gg
  referer: https://www.eldorado.gg/dashboard/orders/sold
  x-xsrf-token: <value of __Host-XSRF-TOKEN cookie>
  x-client-build-time: <int>      # build identifier, ~13 digits epoch ms
  content-type: application/json
  user-agent: <browser UA>
  ```
- `x-client-build-time` doi theo deploy cua Eldo frontend. Capture tu mot XHR cua page authenticated (`page.on("response")` listener trong Camoufox).

**Response 200**:
- Status 200 OK
- Set-Cookie headers chua **IdToken moi** (luon) + co the **RefreshToken rotated** (sometimes — backend xoay rolling)
- Body khong quan trong (thuong la empty object hoac status)

**Implementation tham khao**: [auth/main.py:`_eldo_backend_refresh`](../auth/main.py).

### Tai sao KHONG dung AWS Cognito truc tiep

Eldorado dung **AWS Cognito** lam OAuth broker (Google login → Cognito session). Logic la goi `POST cognito-idp.us-east-2.amazonaws.com/` voi:
```json
{
  "AuthFlow": "REFRESH_TOKEN_AUTH",
  "ClientId": "3a4hal6jgl8gf5hnnjo06k05s5",
  "AuthParameters": {"REFRESH_TOKEN": "<token>"}
}
```

Test thuc te → `NotAuthorizedException: SECRET_HASH was not received`. Client `3a4hal6jgl8gf5hnnjo06k05s5` co configured **client secret** → moi request phai kem `SECRET_HASH = HMAC_SHA256(client_secret, username + client_id)`. Eldorado khong expose secret nay ra frontend, nen ta khong the goi Cognito truc tiep.

→ Phai di qua Eldorado's wrapper endpoint `/api/authentication/refreshTokens`, server-side ho da co secret va tu goi Cognito ho.

### Camoufox capture flow (fallback)

Khi backend refresh fail (RefreshToken thuc su het han hoac response 401):

1. **Phase 1: home page** — `goto https://www.eldorado.gg` voi `wait_until="domcontentloaded"`. Vuot Cloudflare bot check.
2. **Phase 2: dashboard** — `goto /dashboard/orders/sold` voi `wait_for_load_state("networkidle", 15s)`.
3. **Phase 3: capture** — `page.on("response")` (Firefox khong ho tro CDP) capture:
   - All `__Host-Eldorado*` cookies
   - `nsure-device-id` + `x-client-build-time` headers tu cac XHR den `/api/orders/...`
   - Verify bang API probe `/api/orders/me/statesCount`

3 profile rotate: `chrome_profile_eldo`, `_bak1`, `_bak2`. Khi mot profile fail thi auth thu profile ke tiep.

**Cookie preservation guard**: neu capture moi mat IdToken hoac RefreshToken so voi truoc → reject, giu bundle cu. Tranh lam mat session do Camoufox loi.

### Re-login khi RefreshToken het han

RefreshToken slide ~30 ngay moi backend refresh, nhung neu bot dung > 30 ngay khong refresh thanh cong (hoac bi Eldo invalidate session), can re-login manual qua VNC. Quy trinh xem [operations.md](operations.md) muc "Eldorado Auth — backend refresh + session re-login".

### Seller API mode (`ELDO_AUTH_MODE=seller_api`, tu 2026-10-14)

Eldorado mo Seller API dau thang 10/2026: seller tao mot cap **Client ID + Client secret** tren
`https://www.eldorado.gg/dashboard/seller-api` (toi da 2 credential/tai khoan, han 1–365 ngay, secret
chi hien 1 lan). Bot doi cap do lay Bearer token — khong browser, khong cookie, khong XSRF.

| Buoc | Contract (do that 2026-10-11) |
|---|---|
| Lay token | `POST https://www.eldorado.gg/api/authentication/seller/token`, body JSON `{"clientId","clientSecret"}` → 200 `{"accessToken","expiresIn":899,"tokenType":"Bearer"}`. Credential sai/het han/revoke → 401 `Authentication failed.` |
| Token | JWT RS256, `iss=eldorado.fusionauth.io`, `gty=["client_credentials"]`, `sub`=client id, claim `userId`=seller id. Song 900s, KHONG co refresh token — het han thi xin lai |
| Goi API | `Authorization: Bearer <accessToken>`. Khong can `Cookie`, `x-xsrf-token`, `nsure-device-id`, `x-client-build-time` |
| Host | Luon `www.eldorado.gg`. Domain tran tra 301, POST bi doi thanh GET |
| Song song | Mot credential cap nhieu token cung luc duoc |

**Trong code** ([auth/main.py](../auth/main.py)):

- `_eldo_seller_token()` xin token; `_eldo_seller_probe()` goi `GET /api/v1/orders/me/seller-api-eligibility`
  (route co trong Swagger cua Seller API) de chac token duoc chap nhan; `_eldo_seller_credential_expiry()`
  doc han credential qua `GET /api/client-credentials` (best-effort, route nay ngoai Swagger).
- `EldoAuth.capture()` thu Seller API truoc. Thanh cong → `/auth/eldo` tra
  `{auth_mode:"seller_api", bearer_token, expires_at, user_agent, seller_id, cookies:{}, xsrf_token:"", logged_in:true}`.
- **Fallback**: khong lay duoc token → roi xuong duong cookie o tren (backend refresh → Camoufox) va bat
  `OPS ALERT [eldo-seller-api]`. Token dang cam con > 60s thi dung tiep, chua lui. Thu lai cong token sau
  60s (loi mang/5xx) hoac 300s (401).
- `EldoAuth.is_fresh()`: bundle token chi "fresh" khi token con > 360s (= client cache 300s + 60s dem),
  nen client khong bao gio cam token het han. Auth xin token moi moi ~9 phut.
- Client ([shared/eldo_auth.py](../shared/eldo_auth.py)): `EldoAuthData.build_headers()` thay bundle co
  `bearer_token` thi chi gui `user-agent`, `accept`, `authorization`.

**Secret** chi nam trong `/opt/BotPasteDon/.env` tren `.220` (`ELDO_SELLER_CLIENT_ID`,
`ELDO_SELLER_CLIENT_SECRET`), quyen 600; khong bao gio vao log, `/health`, hay git. Cach bat/tat/xoay:
[operations.md](operations.md) muc "Eldorado Auth — Seller API mode".

**Pham vi**: Seller API chi cam ket cac route trong Swagger (offer + don hang). Bot con dung vai route
ngoai Swagger (`orders/me/{id}`, `orders/me/statesCount`, `conversations/me/authorize`, `users/me`,
`library`) — hien van chay bang token nhung Eldorado khong cam ket giu. Bang day du:
[seller_api_migration_plan.md](seller_api_migration_plan.md) muc 2.2.

---

## Eldorado Seller API — tham chiếu

Mục này gom mọi điều đã biết về Seller API chính thức của Eldorado (ra đầu tháng 10/2026), để không
phải dò lại. Cách bot dùng nó nằm ở mục "Seller API mode" ngay trên; kế hoạch chuyển đổi và bảng route
bot gọi nằm ở [seller_api_migration_plan.md](seller_api_migration_plan.md). Số đo ghi kèm ngày đo.

### Nguồn

| Nguồn | Ở đâu |
|---|---|
| Đặc tả Swagger (OpenAPI 3.0.1, 131 path, 141 thao tác, 162 schema, bản lấy 11/10/2026) | repo bot đạp giá `GegeTeamCode/BotDapLowestPoE`, file `docs/eldorado-seller-api/swagger.json` (commit `68f0b03`); trên workstation: `/mnt/Data/Project/BotDapGia/G2G-AutomationBot-v4/docs/eldorado-seller-api/` |
| Số đo quota ghi 11/10/2026 | cùng thư mục, `do-quota-20261011.txt` |
| Swagger UI | `https://www.eldorado.gg/swagger/seller/index.html` — chỉ mở được bằng trình duyệt đã đăng nhập seller; ẩn danh và token Bearer đều 403. Muốn bản mới thì chủ lưu JSON từ trình duyệt, ghi đè file trên rồi commit riêng file đó, so diff để biết route nào đổi |
| Trang tạo credential | `https://www.eldorado.gg/dashboard/seller-api` |
| Bài hướng dẫn | `https://support.eldorado.gg/en/articles/16127288-how-the-seller-api-works` |
| Hỗ trợ | `api@eldorado.gg` — chỉ gửi Client ID, không bao giờ gửi secret |
| Tài liệu cũ | `GET /api/v1/orders/me/seller-api-docs` trả trang tài liệu 06/2025 (đăng nhập Cognito bằng email + mật khẩu rồi gửi cookie `__Host-EldoradoIdToken`). Trang này ghi rõ "no webhook registration is available" |

### Credential

- Mỗi tài khoản có tối đa **2 credential đang hoạt động**. Tên chỉ gồm `[A-Za-z0-9_-]`. Hạn chọn 1, 14,
  30, 90 hoặc 365 ngày. Secret hiện **một lần** lúc tạo.
- **Hiện dùng một credential hạn 365 ngày**, chủ tạo 14/10/2026, hết khoảng 14/10/2027. Ba bot dùng
  chung nó: BotPasteDon `.220`, bot đạp giá (control `.199`), item bot `.221`; mỗi bot tự xin token
  (kiểm 15/10: vết băm id/secret trên ba máy giống nhau, cả ba xin token HTTP 200). Một credential cấp
  nhiều token dùng song song được (đo 11/10: hai token xin cách nhau 1 giây cùng gọi 200).
- **Xoay credential** phải sửa `.env` của **cả ba bot**, không chỉ `.220`: tạo credential thứ hai →
  sửa `.env` từng bot → restart phần auth của từng bot → kiểm → rồi mới Revoke cái cũ. Cách làm trên
  `.220`: [operations.md](operations.md) mục "Eldorado Auth — Seller API mode".
- `GET /api/client-credentials` (ngoài Swagger, nhận Bearer) liệt kê credential kèm `expirationDate`;
  auth `.220` đọc nó 6 giờ một lần để cảnh báo `eldo-seller-cred-expiring` khi còn dưới 14 ngày.
  **Chưa ai thử POST/DELETE route này bằng token** — đừng thử khi chưa hỏi chủ.

### Gọi API

- Lấy token và hình dạng token: bảng ở mục "Seller API mode" (POST `.../authentication/seller/token`,
  JWT RS256 sống 900 giây, không có refresh token).
- Chỉ cần header `Authorization: Bearer <token>`. Header `swagger` mà đặc tả ghi là bắt buộc (mặc
  định `Swager request`) chỉ dành cho Swagger UI; gọi thật không cần.
- Luôn gọi `https://www.eldorado.gg`. Tên miền trần trả 301 và nhiều thư viện đổi POST thành GET.
- **Token không bị giới hạn trong các route của Swagger.** Các route web vẫn trả 200 với Bearer:
  `orders/me/{id}`, `orders/me/statesCount`, `users/me` (có email), `client-credentials`,
  `predefinedOffersUser/me/stateCount`, `library`, `conversations/me/authorize`. Dùng route ngoài
  Swagger là tự chịu rủi ro bị khoá không báo trước.
- Mẹo dò route mà không đổi gì: GET vào một route vốn là PUT/POST → 405 kèm header `Allow` nghĩa là
  route có tồn tại; 404 là không có.

### Nhóm route trong Swagger (bản 11/10/2026, tiền tố `/api`)

| Nhóm (tag Swagger) | Tiền tố route | Số thao tác |
|---|---|---|
| Đơn hàng — bản hiện hành (`UserOrderSellerPrivate`) | `v1/orders/me/*` | 11 |
| Đơn hàng — bản cũ, cả 10 route đánh dấu `deprecated` (`UserOrderSellerPrivateObsolete`) | `orders/me/*` | 10 |
| Currency — họ cũ (`PredefinedOffer*`) | `predefinedOffers`, `predefinedOffersUser/me` | 20 |
| Currency — họ mới (`CurrencyOffer*`) | `v1/currency-management/me/offers`, `v1/currency-management/offers/groups` | 20 |
| Item (`ItemOffer*`) | `v1/item-management/me/offers`, `v1/item-management/offers` | 15 |
| Item — liên kết tài khoản Roblox giao tự động | `v1/item-management/me/roblox-accounts` | 8 |
| Account (`FlexibleOffer*`) — gồm `secretDetails` là thông tin tài khoản giao khách | `flexibleOffers`, `flexibleOffersUser/me` | 19 |
| Boosting | `boostingOffers` | 11 |
| Swap — offer mua | `v1/swap-management/me/buy-offers`, `v1/swap-management/offers` | 13 |
| Trạng thái seller (`OfferUserPrivate`) | `offerUser/me/switchOnline\|switchOffline\|restrictions\|bulkUploadCsv\|csvUploadExample` | 5 |
| Thông báo | `notifications/me`, `unread-count`, `markAsRead`, `markAllAsRead` | 5 |
| Dữ liệu công khai (`OfferPublic`) | `offers/osrsPrices`, `offers/rs3Prices`, `offers/productFeed`, `offers/productFeed.xml` | 4 |

Tổng 141. Route chi tiết của các nhóm offer: tài liệu của bot đạp giá
(`docs/eldorado-seller-api/plan.md` trong repo đó) hoặc mở thẳng `swagger.json`.

### Route đơn hàng `v1/orders/me` (BotPasteDon quan tâm)

| Route | Ghi chú (từ Swagger 11/10 và đo thật) |
|---|---|
| `GET seller/orders` | Bắt buộc `displayFilter=DisplaySellingOrders`. `orderState` ∈ `Paid`, `Disputed`, `Delivered`, `Received`, `Completed`, `Canceled`, `PendingReview` (giá trị cũ `PendingDelivery` bị bỏ từ 13/08/2026, gửi lên trả 400). `pageSize` 1–50. Phân trang cursor: `cursorValue`, `cursorColumn`, `pageDirection` (`Next`/`Previous`). Lọc thêm: `query`, `gameId`, `category`, `filterByActiveWarranty`, `orderGroup` (`Regular`/`Historical`), `isAscendingDateOrder`, `ignorePendingReviewOrders`. Bản cũ `orders/me/seller/orders` nhận cùng bộ tham số, đều 200 bằng token |
| `PUT {orderId}/deliver` | Không body. Chú ý: trang dashboard của Eldorado in ví dụ `PUT /api/orders/me/{orderId}/delivered` — route đó **404**. Bot đang dùng bản cũ `PUT orders/me/{id}/deliver`, chạy thật bằng token 15/10 |
| `POST {orderId}/cancel` | Body `{reason, message}`; `reason` là enum 16 giá trị (`Out_Of_Stock`, `Buyer_Did_Not_Respond`, `Seller_Going_Offline`, `Mutual_Agreement`, `Other`, …). Bot chưa dùng |
| `POST {orderId}/extend-delivery-time` | Bot chưa dùng |
| `POST {orderId}/delivery-details/correction` | Bot chưa dùng |
| `GET order-data`, `GET sold-order-data` | CSV theo `fromDate`, `untilDate`; bản sold có cột `Amount $`, `Seller Fees $`, `Net Income $` |
| `GET invoice/generate` | theo `fromDate`, `untilDate` |
| `GET pending-review-order-count` | |
| `GET seller-api-eligibility` | auth `.220` dùng làm probe kiểm token (route nằm trong Swagger nên ít bị khoá) |
| `GET seller-api-docs` | trang tài liệu cũ, xem bảng Nguồn |

Những gì bot cần mà **Swagger không có**, hiện đi bằng route web (bảng đầy đủ ở
[seller_api_migration_plan.md](seller_api_migration_plan.md) mục 2.2):

- Xem một đơn theo id (`orders/me/{id}`), đếm đơn theo trạng thái (`orders/me/statesCount`; khoá trả về
  là `paid`, không phải `pendingDelivery` — đo 14/10).
- Thông tin giao hàng khách nhập: từ 19/08/2026 không còn nằm sẵn trong đơn, phải đọc từng mục qua
  `GET v1/orders/me/{id}/delivery-details/{detailId}`.
- **Chat**: Swagger chỉ có `POST boostingOffers/boostingRequests/{id}/createConversationForSeller`.
  Bot lấy token TalkJS qua `GET conversations/me/authorize` rồi nói chuyện thẳng với TalkJS — xem
  [marketplace_chat.md](marketplace_chat.md).
- Upload ảnh offer (`files/me/Offer`), số dư/rút tiền, tranh chấp, webhook: không có. Đặc tả không có
  chữ nào về rate limit, quota hay 429.

### Giới hạn

**Đọc (GET)** — đo 14/10/2026 08:27–08:54 từ workstation, 3.430 lượt (2.314 lượt 200, 1.115 lượt 429):

- **45 lượt mỗi cửa sổ khoảng 5 giây, tính theo IP, gộp mọi route vào một bộ đếm** (kể cả route công
  khai gọi không token). Không tính theo tài khoản hay token: lúc workstation bị 429 liên tục, `.220`
  (IP khác, cùng token) gọi 2 lượt/giây vẫn 56/56 lượt 200. Ba bot ở ba IP không chia nhau ngân sách.
- Vượt nhẹ: hết cửa sổ thì tự mở, không phạt. Bắn mạnh (từ 20 lượt/giây) thì bị khoá kéo dài, mở lại
  trong vòng 71 giây sau lượt gọi cuối.
- Body 429: `Whoa! Calm down, cowboy! Quota exceeded`. Không có header rate-limit, không có `Retry-After`.
- **1 lượt/giây là an toàn**: 14 route, mỗi route 60 giây liên tục, 867/867 lượt 200, p50 0,27 giây.
  Chủ chốt 14/10: mức này đủ, **đừng đo lại các mức từ 10 lượt/giây trở lên**. BotPasteDon hiện dùng
  dưới 1 lượt/giây.

**Ghi offer currency** — đo 11/10/2026 (22 lượt 200, 11 lượt 429). BotPasteDon không ghi offer; bảng
này để bot đạp giá `.199` và item bot `.221` tham khảo. Mọi bộ đếm tính **theo từng offer** và **độc
lập nhau**:

| Route | Lượt / 600 giây / offer |
|---|---|
| Cũ `PUT predefinedOffersUser/me/{id}/changePrice` | 3 |
| Mới `PUT v1/currency-management/me/offers/{id}/change-price` | 1 |
| Cũ `PUT predefinedOffersUser/me/{id}/changeQuantity` | 3 |
| Mới `PUT v1/currency-management/me/offers/{id}/change-quantity` | 1 (cửa sổ chưa đo riêng, giả định 600 giây) |

Đổi giá tăng `offerVersion` thêm 1, đổi số lượng thì không. Body đổi giá
`{"amount": 0.00801, "currency": "USD"}`; body đổi số lượng là số nguyên trần.

### Bẫy khi gọi

- `GET v1/currency-management/me/offers` phải kèm body JSON (thiếu → 415), ví dụ
  `{"augmentedGameFilter": {"gameId": "220", "category": "Currency"}, "offerState": "Active"}`.
  `augmentedGameFilter: {}` trả offer của mọi game; bỏ `offerState` trả mọi trạng thái (Swagger ghi bắt
  buộc nhưng thực tế không).
- Credential sai, hết hạn hoặc đã revoke: lệnh xin token trả 401 `Authentication failed.`.
- Quy định seller: tin nhắn gửi qua API có thể không được tính là phản hồi ban đầu nếu hội thoại không
  có trao đổi thật, và đơn vẫn có thể bị huỷ
  (`https://support.eldorado.gg/en/articles/8408997-seller-rules`).

### Trạng thái dùng thật (tới 22/10/2026)

- BotPasteDon `.220` chạy `ELDO_AUTH_MODE=seller_api` từ 14/10/2026 05:24. Đơn thật đầu tiên đi trọn
  vòng bằng token ngày 15/10 (`19183B38-…`: quét đơn Paid → ERP → `PUT deliver` → bằng chứng và lời
  nhắn qua TalkJS → `Completed`). Kiểm vận hành 17/10: 0 lượt 401/403/429 do Eldorado; một lần lùi
  cookie 16/10 14:58–15:02 vì DNS không phân giải được `www.eldorado.gg`.
- **Đường lùi cookie sắp hết**: profile `chrome_profile_eldo` hết hạn 15/10, `_bak2` đã đăng xuất, chỉ
  còn `_bak1` tới 04/11/2026 05:17. Sau ngày đó, nếu Seller API lỗi thì auth không còn gì để lùi.
- Chưa làm: đổi route đơn hàng sang `v1` (Bước 3 của kế hoạch).
- Chưa đo bằng token: các lệnh `bulk/*`, POST tạo offer họ mới, pause/resume, DELETE offer, quota
  item-management, trần của `PUT predefinedOffers/{id}/details`, giới hạn đọc dài hạn (theo giờ/ngày).

---

## G2G.com

### Token lifecycle

| Token | Vi tri | TTL | Vai tro |
|---|---|---|---|
| `accessToken` (JWT) | localStorage | 15 phut | JWT G2GSls self-issued. Send Authorization Bearer. |
| `refresh_token` | Cookie | ~12 ngay (sliding) | Format `<user_id>.<32-hex>`. Send trong body refresh request. |
| `long_lived_token` | Cookie | ~10 thang (sliding) | Extended session. Cung send trong body. |
| `active_device_token` | Cookie | ~8 thang (sliding) | Device fingerprint. Cung send trong body. |
| `S3ID`, `S3RM`, `G2GSESID_V4` | Cookie | various | G2G session bookkeeping (KHONG dung cho refresh). |

JWT structure (decoded):
```json
{
  "iat": 1781028019,
  "exp": 1781028919,       // 15 min
  "sub": "1001273426",     // <-- user_id (su dung trong refresh body!)
  "subType": "user",
  "subRole": ["user"],
  "aud": "https://www.g2g.com",
  "iss": "G2GSls"          // <-- G2G self-issued, KHONG phai Cognito
}
```

**G2G self-issue** (`iss=G2GSls`) — khong qua broker nao ca. Khac voi Eldo o cho ta khong gap rao can SECRET_HASH.

### Backend Refresh

**Endpoint**:
```
POST https://sls.g2g.com/user/refresh_access
```

**Request body**:
```json
{
  "user_id": "<JWT.sub>",
  "refresh_token": "<cookie value>",
  "active_device_token": "<cookie value>",
  "long_lived_token": "<cookie value>"
}
```

**Headers required**:
```
authorization: Bearer <current_jwt>
origin: https://www.g2g.com
referer: https://www.g2g.com/
content-type: application/json
cookie: <full cookie bundle>
user-agent: <browser UA>
```

**TLS fingerprint**: G2G sls.g2g.com co Cloudflare/CloudFront fronting → san request khong impersonate browser TLS se bi 403 (ngay ca khi headers + body dung). Dung `curl_cffi` impersonate `chrome120`.

**Response 200**:
```json
{
  "code": 2000,                            // 2000 = success
  "messages": [],
  "payload": {
    "access_token": "<NEW JWT>",
    "access_token_exp": 1781029619000,     // ms epoch
    "refresh_token": "<rotated value>",
    "refresh_token_exp": 1782085978000,    // sliding ~12 days
    "active_device_token": "<rotated>",
    "active_device_token_exp": 1786212719000,
    "long_lived_token": "<rotated>",
    "long_lived_token_exp": 1796580719000
  },
  "request_id": "<uuid>"
}
```

**Error responses**:
- `HTTP 500 + code 5001` → body / headers thieu hoac sai
- `HTTP 401` → JWT current khong hop le hoac da bi server kick session
- `HTTP 403` → TLS fingerprint khong khop browser (thieu impersonate chrome120)

**Implementation tham khao**: [auth/main.py:`_g2g_backend_refresh`](../auth/main.py).

### Selenium fallback (CDP capture)

Khi backend refresh fail (lan dau init, hoac refresh_token het han / kick session):

1. Mo Chrome headless voi `goog:loggingPrefs={performance: ALL}` (CDP enabled)
2. Navigate `https://www.g2g.com/g2g-user/sale?status=preparing`
3. Wait 8s cho SPA load
4. Phuong an A — **CDP performance log**: parse cac `Network.requestWillBeSent` events, tim `Authorization: Bearer eyJ...` headers gui den `sls.g2g.com`. Lay JWT moi nhat (compare `iat`).
5. Phuong an B (fallback) — **localStorage**: doc `localStorage.accessToken` truc tiep. Day la noi SPA luu JWT.
6. Validate JWT exp con > 0.
7. Extract cookies tu `driver.get_cookies()`.

Neu page redirect ve `/login`: session het han. Trigger auto-login flow (email + password + OTP qua dashboard) hoac VNC manual re-login.

### Discovery story (de tham khao methodology)

Khi viet code refresh dau tien chua biet endpoint, da lam:

1. **Dump cookies + JWT claims** ([scripts/_diag_missing_evidence.py](../scripts/_diag_missing_evidence.py) pattern) → phat hien co `refresh_token`, `long_lived_token`, `active_device_token` trong cookies va `iss=G2GSls` → confirm self-issued, khong qua Cognito.

2. **Probe endpoints** ([scripts/_probe_g2g_refresh.py](../scripts/_probe_g2g_refresh.py)) → 14 candidate URLs (`/user/refresh_jwt`, `/auth/refresh`, etc.). Tat ca return 401/403/404 hoac HTML SSR → khong tim ra blind.

3. **CDP network sniff** ([scripts/_sniff_g2g_refresh.py](../scripts/_sniff_g2g_refresh.py)) → mo Chrome headless qua profile clone, navigate dashboard, dump tat ca request URL chua keyword `refresh|token|jwt|auth`. → **Tim thay `POST sls.g2g.com/user/refresh_access` 200**.

4. **JS bundle decompile** ([scripts/_g2g_js_grep.py](../scripts/_g2g_js_grep.py)) → fetch tat ca `.js` bundle cua g2g.com, grep `refresh_access`, `refresh_token`, `accessToken`. → Tim thay trong `https://www.g2g.com/js/app.<hash>.js`:
   ```js
   const Ue = async () => {
     const e = await xe["a"].AUTH.REFRESH({
       user_id: Object(Te["f"])(),
       refresh_token: Object(Te["g"])(Te["d"]),
       active_device_token: Object(Te["g"])(Te["b"]),
       long_lived_token: Object(Te["g"])(Te["c"])
     })...
   ```
   → Lay duoc exact body schema.

5. **Probe lai voi body day du** ([scripts/_probe_refresh_access_final.py](../scripts/_probe_refresh_access_final.py)) → HTTP 200, new JWT, working.

Toan bo discovery scripts giu lai trong [scripts/_*.py](../scripts/) de tai su dung cho marketplace khac.

---

## Methodology cho marketplace moi

Khi can them platform thu 3 (vd. PlayerAuctions, MMOGah), follow flow nay:

### Buoc 1: Hieu kien truc auth

- Login bang gi? (Google OAuth, email/pw, social...)
- Co OTP / 2FA?
- Token o dau? (cookie / localStorage / sessionStorage)
- Decode JWT (neu co): `iss` la ai? Cognito? Auth0? Self-issued?
- Refresh token cookie co ten gi?

### Buoc 2: CDP network sniff

```python
# Tham khao scripts/_sniff_g2g_refresh.py
# - Clone profile (tranh fight lock voi auth service)
# - Selenium Chrome voi performance logging
# - Navigate authenticated page, wait 15s
# - Drain logs, filter URL chua refresh/token/auth/login/session
```

Output: ung vien endpoints. Lam vai lan o cac moment khac nhau (initial load, navigate route, JWT near-expire) de bat het cac time-triggered request.

### Buoc 3: Probe candidates

```python
# Tham khao scripts/_probe_g2g_refresh.py
# - GET + POST voi empty body / simple body
# - curl_cffi impersonate chrome120
# - Phan biet 404 (path khong ton tai) vs 401 (path co, sai auth) vs 500 (path co, sai body)
# - SigV4 error → endpoint la AWS API Gateway internal, khong dung cho user
```

### Buoc 4: JS bundle decompile

```python
# Tham khao scripts/_g2g_js_grep.py
# - Capture tat ca .js bundle URL tu page load
# - Fetch tung file, grep keyword (refresh_token, accessToken, endpoint path)
# - Webpack minified code thuong vẫn de doc cau truc fetch/axios
```

Output: exact body schema + headers.

### Buoc 5: Confirm voi production cookies

```python
# Tham khao scripts/_probe_refresh_access_final.py
# - Goi auth/<platform> de lay JWT + cookies hien tai
# - Construct body theo schema vua tim
# - POST voi headers exact match browser
# - Verify response: status 200, body chua new JWT, decode JWT confirm sub/exp tang
```

### Buoc 6: Implement two-tier trong auth service

Pattern (tham khao [auth/main.py](../auth/main.py)):
```python
# 1. Helper function _<platform>_backend_refresh(jwt, cookies, ua) -> Optional[dict]
#    - POST endpoint voi body construct tu cookies + jwt
#    - Validate response code
#    - Return {"jwt_token": new, "cookies": updated} hoac None

# 2. Method <Platform>Auth._try_backend_refresh(self)
#    - Validate self.data co ton tai
#    - Call helper
#    - Update self.data + self.captured_at
#    - Reset _consecutive_failures

# 3. Modify <Platform>Auth.capture(self)
#    - Day fast path le truoc:
#      if self.data and self.data.get("jwt_token"):
#          refreshed = self._try_backend_refresh()
#          if refreshed: return refreshed
#    - Fall through code Selenium cu khi fail
```

### Cac luu y quan trong

- **TLS fingerprint matters**: Dung `curl_cffi` impersonate `chrome120` (hoac firefox lat tuc theo browser thuc). Nhieu marketplace dung Cloudflare/CloudFront se tra 403 voi request stock `requests`.
- **Cookie scope**: Cookies `__Host-` prefix la same-origin strict. Cookies `Domain=.platform.com` co the dung cho subdomain. Quan tâm khi build cookie header.
- **OPTIONS preflight**: Endpoint co the requires CORS preflight. CDP capture OPTIONS truoc POST.
- **Server-side session kick**: G2G code comment khong retry voi dead JWT — server se "kick session" → buoc re-login. Khi probe, han che POST repeated voi same body.
- **Profile lock conflict**: Khi mo Chrome de sniff, copy profile ra `/tmp` thay vi dung profile prod, tranh fight voi auth service.

---

## File references

| File | Vai tro |
|---|---|
| [auth/main.py](../auth/main.py) | `EldoAuth`, `G2GAuth`, `_eldo_backend_refresh`, `_g2g_backend_refresh`, `_eldo_seller_token`, `_eldo_seller_probe`, `_eldo_seller_credential_expiry` |
| [tests/test_eldo_seller_auth.py](../tests/test_eldo_seller_auth.py) | Eldorado gia tren localhost, kiem duong Seller API cua auth (can venv co `requirements.txt`) |
| [marketplace_chat.md](marketplace_chat.md) | Token va luong chat (TalkJS, Sendbird) |
| [shared/eldo_auth.py](../shared/eldo_auth.py) | Eldorado auth client (cache JWT + cookies tu auth service) |
| [shared/g2g_auth.py](../shared/g2g_auth.py) | G2G auth client (cache JWT tu auth service) |
| [scripts/_sniff_g2g_refresh.py](../scripts/_sniff_g2g_refresh.py) | CDP sniff template |
| [scripts/_g2g_js_grep.py](../scripts/_g2g_js_grep.py) | JS bundle decompile template |
| [scripts/_probe_g2g_refresh.py](../scripts/_probe_g2g_refresh.py) | Blind probe template |
| [scripts/_probe_refresh_access_final.py](../scripts/_probe_refresh_access_final.py) | Confirm-endpoint template |
| [scripts/_smoke_g2g_refresh.py](../scripts/_smoke_g2g_refresh.py) | Smoke test pattern (offline + decode JWT + guard rails) |
