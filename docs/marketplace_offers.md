# Tạo offer currency trên Eldorado và G2G — điều tra, tạo, đo lại

Tài liệu cho người (hoặc agent AI) sắp mở bán một game/currency mới trên Eldorado.gg hay G2G.com, để khỏi
dò lại từ đầu. Ví dụ xuyên suốt là **Aion 2 Kinah**: 104 offer Eldorado tạo ngày 22–23/10/2026 (mỗi tổ hợp
Region × Faction × Server một offer, giá đứng hạng 8).

Mức độ chắc chắn:
- **Eldorado:** mọi route, đơn vị và giới hạn dưới đây đều đã chạy thật trong đợt Aion 2, trừ chỗ ghi "chưa thử".
- **G2G:** phần điều tra, đạp giá và đơn về ERP đã có căn cứ. Phần **tạo offer currency G2G chưa có ghi chép**
  (xem [3.3](#33-tạo-offer-currency--chưa-có-ghi-chép)).

Liên quan: xác thực hai sàn ở [marketplace_auth.md](marketplace_auth.md) (mục Seller API), vận hành bot ở
[operations.md](operations.md).

---

## 1. Ba hệ cùng chạm vào một offer

| Hệ | Ở đâu | Việc với offer |
|---|---|---|
| Sàn (Eldorado / G2G) | — | Offer sống ở đây: giá, tồn, min, hạn |
| Bot đạp giá `G2G-AutomationBot-v4` | control `.199` + worker `.201–.209` | Giữ giá theo đối thủ; giữ **ngân sách POST Eldorado chung cả tài khoản** (Redis `.199`) |
| BotPasteDon (repo này) | `.220` | Thấy đơn → đẩy vào ERP → giao → chat |

Offer tạo ra mà bot đạp giá không biết thì giá đứng yên. Đơn về mà BotPasteDon map sai game hay mặt hàng
thì ERP tạo Sell Order lệch (mất `game_context`, sai số lượng). Vì vậy, trước khi mở bán game mới phải
xem đủ cả ba hệ ([mục 5](#5-checklist-cho-game-mới)).

---

## 2. Eldorado

Mọi lời gọi đều tới `https://www.eldorado.gg/api/...` (luôn có `www.`; tên miền trần trả 301 và POST bị đổi
thành GET). Route công khai không cần token; route `me` dùng token Seller API (Bearer).

### 2.1 Tìm `gameId` và category

`GET /api/library?locale=en-US` (công khai) trả danh sách mục `{gameId, category, title, gameName, seoAlias, ...}`.
- Một `gameId` có nhiều mục, mỗi category một mục. Aion 2 là `360`, gồm `Account` ("Accounts"),
  `Currency` ("Kinah") và `RequestedBoosting` ("Custom Request").
- **`title` là tên category, không phải tên game.** Tên game nằm ở `gameName`. Đây là gốc của lỗi map game
  ở [2.10](#210-đơn-về-erp-qua-botpastedon--phải-xử-lý-trước-khi-bán).

### 2.2 Cây Region / Server / Faction và đơn vị

`GET /api/library/{gameId}/{category}?locale=en-US` (công khai). Route này **không có trong Swagger**; tìm
được bằng cách đọc bundle frontend (hàm `getGameCategory`). Kết quả trả về:
- `tradeEnvironments[]`: cây lồng nhau qua `childTradeEnvironments`. Mỗi nút có `id`, `name` (tên cấp,
  vd "Region"), `value` (vd "Asia") và `isHidden`. **Lá của cây là một tổ hợp bán được.** `id` của lá ghép
  theo cấp, vd Asia / Asmodian / Israphel = `2-9-5`.
- `unitSystem`: đơn vị mặc định của game. `"1000000"` nghĩa là giá tính cho 1M đơn vị.
- `deliveryMethods`: các cách giao hợp lệ. Aion 2 có `LoginMethod`, `EpicGifting`, `IslandDelivery`,
  `GamePass`, `MailTrade`, `AuctionHouse`, `InGameTrade`.

Cách Eldorado mã hoá item khác nhau theo game (đều đã thấy thật):

| Game | `tradeEnvironmentId` | Attribute | `unitSystem` |
|---|---|---|---|
| PoE1 currency (gameId 2) | `0-20` (PC / league) | `path-of-exile-orbs` = `divine-orb`… | `Unit1` |
| PoE2 currency (220) | `10` (Standard), `12` (event) | `path-of-exile-2-orbs` = … | `Unit1` |
| D4 Gold (132) | `4` ("Seasonal Realm - Softcore") | không | `Unit1000000` |
| Torchlight Flame Elementium (225) | `3`/`4`/`5` (Europe/America/Asia Season) | không | `Unit1` |
| Aion 2 Kinah (360) | `R-F-S` 3 cấp Region → Faction → Server | không | `Unit1000000` |

(`unitSystem` đọc từ offer của GegeTeam ngày 22/10/2026.) Đơn vị **gắn theo từng offer** và có thể đổi theo
thời gian: ghi chú 07/2026 ghi D4 Gold là `Unit1000000000`, nay là `Unit1000000`. Board Aion 2 cũng có offer
cấp Region mang `Unit1000`. Vì vậy phải đọc `unitSystem` của từng dòng, đừng suy từ game.

Game có attribute thì đọc thêm `GET /api/library/{gameId}/{category}/attributes/offers` (route thấy trong
bundle, **chưa thử**).

Aion 2 có 104 lá: Europe 46, Asia 18, NA East 18, Latin America 12, NA West 10. Region id: 0 NA West,
1 Europe, 2 Asia, 6 Latin America, 7 NA East.

### 2.3 Đọc board từng tổ hợp

```
GET /api/predefinedOffers/augmentedGame/offers
    ?gameId=360&category=Currency&tradeEnvironmentId=2-9-5&pageIndex=1&pageSize=100
```
- `tradeEnvironmentValue0/1/2=<value>` cho đúng cùng tập dòng như `tradeEnvironmentId`.
- `pageSize` tối đa 100. Có `recordCount` để biết còn trang sau không.
- **Route bỏ qua tham số sort** (`offerSortingCriterion=Price&isAscending=true` không có tác dụng) và trả
  theo điểm. Muốn biết hạng trên trang mua theo giá thì tự xếp `offer.pricePerUnit.amount` tăng dần.
- Offer chỉ gắn cấp Region (không có server) **không hiện** ở board của server.
- Mỗi dòng có `offer` (giá, `unitSystem`, `quantity`, `minQuantity`, `deliveryMethod`,
  `guaranteedDeliveryTime`, `description`), `user.username` và `userOrderInfo`.
- **Giới hạn đọc: 45 lượt / ~5 s theo IP**, gộp mọi route kể cả route công khai. Nhịp 1 lượt/s là an toàn:
  đợt Aion 2 chạy 866 GET ở nhịp 1,1 s, không lượt nào 429. Bắn ≥ 20 lượt/s thì bị khoá kéo dài.

Kiểm đơn vị trước khi tính giá. Aion 2: 2.700/2.700 dòng là `Unit1000000`, nên `pricePerUnit` là USD cho
1M kinah, và `quantity`/`minQuantity` tính theo 1M. Bước giá lấy từ dữ liệu: không giá nào quá 5 chữ số lẻ,
nên bước là 0,00001.

### 2.4 Offer của mình

- `GET /api/predefinedOffers/me?gameId=&category=&tradeEnvironmentId=&offerState=&pageIndex=&pageSize=`
  (Bearer). **`pageSize` ≤ 50**; 100 trả 400 "invalid values: PageSize". Lọc `tradeEnvironmentId` theo lá
  là đúng (đã thử: tổ hợp có offer → 1, tổ hợp không có → 0).
- `GET /api/predefinedOffersUser/me/stateCount` → `{activeOffers, pausedOffers, closedOffers, offlineOffers}`.
- `GET /api/predefinedOffers/{id}` (công khai) và `GET /api/predefinedOffers/{id}/private` (Bearer).

Chụp `/me` của mọi trạng thái và `stateCount` **trước và sau** đợt tạo, để chứng minh không offer cũ nào bị đổi.

### 2.5 Định giá theo hạng

Luật đã dùng cho Aion 2 (người dùng chốt 22/10/2026), chạy được cho game khác:
- Xếp board theo giá tăng dần, bỏ dòng của GegeTeam.
- **Đủ 8 dòng:** giá = dòng 8 − 1 bước. Nếu giá đó ≤ dòng 7 thì đặt bằng dòng 8 và ghi "hoà giá".
- **1–7 dòng:** bỏ dòng "treo giá" (> 2× trung vị board), giá = dòng đắt nhất còn lại + 1 bước.
  Seller hay đẩy offer lên giá cao để tạm ngưng; trên Eldorado Aion 2, soverendezvous treo 9,0 ở nhiều server.
- **Bảng trống:** bỏ qua.

Luôn dựng **bảng dry-run** cho mọi tổ hợp trước khi tạo, để người dùng duyệt. Cột gồm: Region, Server,
Faction, env id, số dòng, dòng 7, dòng 8 (hay dòng cuối), giá đề xuất, hạng dự kiến, tồn, min, ghi chú.

Tạo mà không đưa vào bot thì hạng trôi nhanh. Aion 2, 2–15 phút sau khi tạo: 67/102 offer còn hạng 8,
16 ở hạng 9, 3 ở hạng 10, 14 ở hạng 5–7 (đối thủ đổi giá liên tục).

### 2.6 Tồn, min, mô tả, thời gian giao

Đối thủ khai tồn khống (999.999, 9.989.946…), nên đừng lấy tồn của họ làm căn cứ. Cách đã dùng cho Aion 2:
- **Tồn:** offer G2G cùng server đang bán giá thật thì lấy đúng tồn G2G (chặn trần 100); còn lại để 20
  (trung vị tồn G2G thật). Offer G2G đang treo $30 = server đang ngưng bán, nên có thể không có hàng.
- **Min:** mức min phổ biến nhất trong 10 dòng rẻ nhất của board (Aion 2 hầu hết là 10).
- **Giao:** `AuctionHouse` (89% đối thủ, khớp cách giao bên G2G). `guaranteedDeliveryTime` = `Hour1`.
  Các mức thấy trên board: `Minute20`, `Hour1`, `Hour5`, `Hour12`, `Day1`, `Day3`, `Day28`.
- **Mô tả:** không nhắc tên sàn khác. Ghi đơn vị ("1 unit = 1,000,000 Kinah") và cách giao (khách treo đồ lên
  AH, mình mua; không chịu thuế AH).

### 2.7 Tạo offer

**Route:** `POST /api/predefinedOffers` (Bearer, Seller API). Payload (dựng bằng
`EldoradoCurrency.build_payload` của bot đạp giá):

```json
{
  "details": {
    "pricing": {"quantity": 70, "minQuantity": 10,
                "pricePerUnit": {"amount": 0.76999, "currency": "USD"}, "volumeDiscounts": []},
    "description": "Welcome to GEGEShop! ...",
    "guaranteedDeliveryTime": "Hour1",
    "deliveryMethod": "AuctionHouse"
  },
  "augmentedGame": {"gameId": "360", "category": "Currency",
                    "tradeEnvironmentId": "2-9-5", "attributeIdsCsv": ""},
  "category": "Currency"
}
```

Game có attribute thì `augmentedGame` thêm `"offerAttributes": [{"id": "<attrSlug>", "type": "Select",
"value": "<valueSlug>"}]` và để `"attributeIdsCsv": null` (như PoE).

**Trả về 201:**
`{"offersCreatedCount": 1, "offersUpdatedCount": 0, "offer": {id, offerState, expireDate, unitSystem, ...}}`.
Offer mới có `offerVersion` 0, Active ngay, hạn ~20 ngày (`expireDate` 18:00Z; tạo 22–23/10 → 12/11).

**Ba rào bắt buộc:**
1. **POST là upsert theo game + env + item.** Gửi vào tổ hợp đã có offer là **ghi đè** giá, tồn và mô tả của
   offer đang bán. Trước mỗi POST phải gọi `/me` lọc đúng env và thấy 0 offer. Sau POST, kiểm
   `offersUpdatedCount == 0`, khác 0 thì dừng.
2. **Ngân sách POST là chung cả tài khoản.** Đo 17/09/2026: ~10 POST trong 41 s thì bị 429; ~750 POST trong
   ~100 phút thì bị khoá 6–12 giờ. Bot đạp giá cũng POST khi xoay offer, nên mọi POST phải đi qua `ElBudget`
   (`worker/el_budget.py` của bot đạp giá; sổ Redis `el:post:ledger` trên `.199`: 750 lượt/12h, 8 lượt/60s,
   cách nhau ≥ 6 s). Thứ tự giống `worker/tasks/el_task.py::_post_write`:
   - `reserve_post()` → gửi POST;
   - 2xx → `on_post_success()`;
   - 429 → `on_post_429()` rồi **dừng mọi POST**;
   - 401/403 → `refund_post(ts)`;
   - lỗi khác giữ nguyên suất đã giữ.

   Ghi cả `worker.el_writelog.log_write` để dòng ghi vào `logs/el_writes_cluster.jsonl` của control.
   POST ngoài sổ làm bot không biết pool đã vơi; chạm trần là mọi coin Eldorado mất đường POST tới 12 giờ.
3. **Không gọi route hàng loạt** (`bulk/*`, `changePriceForAll`, `changeDeliveryTimeForAll`, `pauseAllActive`,
   `deleteAll`, `delete-all`). Chúng tác động mọi offer khớp bộ lọc, kể cả offer PoE/D4 đang bán.

**Cách chạy đã dùng:**
- Chạy từ `.199` (token lấy qua `control/auth/el_seller_api.get_bundle`, cache Redis `el:seller_token`;
  không in secret).
- Nhịp 8 s/POST, mỗi tổ hợp: đọc lại board → tính lại giá → `/me` trống → POST.
- Dừng khi: 429, lỗi "Maximum of N active offers", 2 lỗi liền nhau, pool < 200, `offersUpdatedCount` > 0.
- Tạo 1 offer thử, đo lại, rồi mới chạy hết. 104 offer mất ~14 phút.

Trần số offer Active cho mục Currency **chưa đo**: Aion 2 lên 110 offer Active không gặp lỗi. Mục Item có
trần 200 offer/game.

### 2.8 Đo lại

- Đọc lại board từng tổ hợp: offer mình có mặt, `offerState` Active, giá/tồn/min khớp payload, hạng theo giá.
- So `/me` và `stateCount` trước/sau: số offer mới = số POST 201, offer cũ không đổi `offerVersion`, giá, tồn,
  hạn, mô tả.
- So `el:post:ledger` trước/sau: tăng đúng bằng số POST đã gửi.

### 2.9 Sau khi tạo

- **Hạn:** offer tự chuyển `Paused` khi tới `expireDate` và biến khỏi board, không có cảnh báo. Tạo cả loạt
  một lúc thì cả loạt hết hạn **cùng ngày**. Gia hạn bằng cách ghi lại offer (bot đạp giá đang ghi giá thì
  hạn tự đẩy xa).
- **Giá:** offer ngoài bot đạp giá thì đứng yên. Muốn giữ hạng thì đưa vào bot. Aion 2 đã làm ngày 23/10/2026,
  commit `de479c1` của `G2G-AutomationBot-v4`:
  - Mỗi tổ hợp một `EldoradoCurrency` trong `shared/config/eldorado.py`, gom vào `EL_STRUCTURE["<GAME>"]` →
    dashboard tab ELDORADO có menu game riêng.
  - **Key phải chứa `_el`** (`is_eldorado()` dò chuỗi này) và **không trùng tiền tố nhóm G2G**. Aion 2 dùng
    `aion2el_<vùng>_<server>_<a|e>_el`, vì nhóm `all-aion2` và trang aion2.gegeteam.xyz lọc theo `aion2_`.
  - Nhóm worker mới trong `worker/main.py::_expand_group` (`all-el-aion2`) và nối vào `COINS` trong
    `.env.worker` của worker cập nhật (`.204`). Scraper `.201` (`COINS=all-el`) tự nhận.
  - **Gieo `offer_qty` và `min_qty` vào `settings.json` TRƯỚC khi bật**, qua `POST /api/settings/{coin}` của
    control. `PUT /details` và POST ghi lại cả offer từ hai ô này; để trống thì tồn thành 1000 của
    `DEFAULT_SETTINGS` (= 1 tỉ kinah). Aion 2 gieo thêm `el_main: false`, `min_stock: 0`; coin để tắt,
    `price_to = 0` thì bot không ghi gì.
  - Gieo offer id vào Redis `el:offer_id:<coin>` (hạn 30 ngày). Không gieo thì bot tự tìm qua `/me`.
  - Deploy: pull trên `.199`, chép tệp lên 9 worker, restart control → `.201` → `.204`. Restart `.204` mất
    ~2 phút (SIGTERM hết hạn 90 s rồi bị SIGKILL).

### 2.10 Đơn về ERP qua BotPasteDon — phải xử lý trước khi bán

Đơn Eldorado đi qua `scanners/eldorado_scanner_api.py::_map_order_data`. Game mới thường vấp ba chỗ.
Aion 2 dính cả ba ở đơn đầu tiên (`A11F9438…`, 23/10/2026 → `SO-261023-R1CG2IBY` trên .100: game
"Custom Request", `game_context` NULL, server "Europe", 10.000.000 × 0,000001). Đã sửa cùng ngày:

| Chỗ vấp | Nguyên nhân | Cách sửa (Aion 2) |
|---|---|---|
| **Game** | Không có attribute thì lấy tên ở `/library`; `shared/eldo_api.py::_fetch_game_library` dựng dict `gameId → title`, mục sau ghi đè mục trước, mà `title` là tên category ("Custom Request") | Thêm `"<gameId>": "<Game Title đúng như ERP>"` vào `ELDO_GAME_ID_TITLES` (`"360": "Aion 2"`) |
| **Server / tên hàng** | Vòng `for te in trade_env` lấy cấp env **đầu tiên** (Region); tên hàng rơi về `gameCategoryTitle` ("Kinah") | Game có env nhiều cấp vào `ELDO_SERVER_LABEL_GAMES`: gửi nhãn giống G2G `"Zikel - Asmodian [EU]"` ở cả `server` lẫn `itemName` (`ELDO_REGION_LABELS`: Europe → EU) |
| **Số lượng** | Chỉ category "Gold" được quy về đơn vị 1M; category khác thì `qty = purchaseQuantity × unit` | (gameId, category) đếm theo 1M vào `ELDO_MILLION_UNIT` (`("360", "Currency")`) → 10 × 0,76699 |

Kiểm trước khi sửa: lấy JSON gốc của đơn bằng `EldoradoAPIClient.get_order_detail` trên `.220`;
so `_map_order_data` bản cũ/mới trên đơn thật của các game khác (Aion 2: 13/13 đơn không lệch); test
offline `tests/test_eldo_order_map.py`.

Bên ERP .100: Aion 2 có một context "Aion 2 - Global" và một Currency Item "Kinah". Đơn G2G Aion 2
về với tiêu đề hàng là nhãn server, số lượng theo Mil Kinah, không gắn Currency Item (giao ngoài ERP).

---

## 3. G2G

Đọc công khai qua `https://sls.g2g.com` (không cần JWT). Ghi (sửa offer) dùng JWT của phiên seller.

### 3.1 Điều tra cấu trúc (công khai)

- Tìm game: `/v3/offer/keyword/search?q=<tên>` → `brand_id` (Aion 2: `lgc_game_37781`).
- Các mảng: `/v3/offer/brand/{brand_id}/categories?country=VN` → `seo_term` (Aion 2 Kinah: `aion-2-kinah`).
- Region của mảng: `/v3/offer/keyword_relation/region?seo_term=…`. Lọc offer bằng `region_id=<uuid>`
  (Aion 2: Asia `7d971a93-e541-4055-8fcd-eb942b65701a`, EU `ac3f85c1-7562-437e-b125-e89576b9a38e`).
- Server/faction là `filter_attr=<collection_id>:<dataset_id>`. Aion 2 dùng `lgc_37781_dropdown_NN`
  (NN 11 = NA East + West, 12 = EU, 13 = Asia), dataset dạng `lgc_37781_dropdown_13_64777`.
  - Cây bộ lọc đầy đủ: route API đòi token, nên đọc từ HTML SSR `www.g2g.com/categories/<seo_term>`.
  - Hoặc search không `filter_attr` rồi gom `offer_attributes[].dataset_id` theo `collection_id`.
- Offer: `/offer/search?seo_term=…&region_id=…&filter_attr=…&page_size=100&page=N&currency=USD&country=VN&v=v2`.
  Trần offset 1600. `converted_unit_price` là giá USD; `unit_price` là tiền gốc của seller.
- Đơn vị: `unit_name` (Aion 2: "Mil Kinah" = 1M), `qty_metric`. Tiêu đề offer currency là nhãn server,
  vd "Israphel - Asmodian [Asia]".

### 3.2 Offer của mình

`/offer/search?seo_term=<seo>&seller=GeGeTeam&page_size=100&include_offline=1` trả đủ `offer_id`, `title`,
`filter_attributes`, giá, `available_qty`, `min_qty`, `created_at`. Tham số `seller_id=`/`username=` bị bỏ qua.
Chi tiết `/offer/{offer_id}` có `delivery_method_details` và `delivery_speed`; trả 4041 khi offer offline.

Aion 2 (22/10/2026): 81 offer live, cùng giao `auction_house` và thủ công. 48 offer treo $30 (tạm ngưng),
33 offer giá thật (tồn trung vị 20).

### 3.3 Tạo offer currency — chưa có ghi chép

Offer Kinah G2G của GeGeTeam **không do Claude tạo**, và không memory hay transcript nào ghi cách tạo.
`created_at` cho thấy chúng được tạo theo lô bằng công cụ hàng loạt: 55 offer lúc ~15:16 ngày 10/10/2026 và
24 offer lúc ~12:00 ngày 13/10, mỗi offer cách nhau ~1 s; thêm 2 offer lẻ ngày 12/10 và 15/10.

> **TODO:** người đã tạo đợt này bổ sung công cụ/cách làm (form web, nhập file, hay API) vào đây.

Đã biết, chỉ cho offer **boosting** (form cũ, khác currency): bot `.227` (`BotReupLowestG2G`) tạo bằng
`scripts/create_placeholder_offers.py`, POST form legacy `/sell/submitListing`. Route này không trả id offer,
nên **không được retry** (gửi lại sẽ đẻ offer trùng).

### 3.4 Đạp giá offer G2G

- Bot đạp giá sửa giá bằng `PUT https://sls.g2g.com/offer/{offer_id}?v=v2` (JWT), body gồm `seller_id`,
  `offer_id`, `unit_price`, `min_qty`, và `qty` khi chủ điền cột Stock.
- **PUT ghi đè `min_qty`** thành `ceil(1/giá)` nếu không điền ô Min Qty.
- Thêm game mới cho bot:
  - mỗi offer một `G2GCoin` trong `shared/config/g2g.py`;
  - một nhóm trong `worker/main.py::_expand_group`, rồi chép lên control và worker, restart control + worker đó.
- **Bẫy:** key coin không được chứa `_el`, vì `is_eldorado()` dò chuỗi này (vd `_elyos` bị coi là coin Eldorado).

### 3.5 Đơn G2G về ERP

G2G scanner gửi đúng tên game ("Aion 2"), `itemName` = tiêu đề offer (nhãn server), số lượng theo đơn vị sàn
(Mil Kinah). ERP ghép mặt hàng theo nhãn này, nên tên Currency Item phải trùng nguyên văn nhãn sàn.

---

## 4. Ví dụ Aion 2 Kinah — số đo

| | Eldorado | G2G |
|---|---|---|
| Game / mảng | `gameId 360`, `Currency`, title "Kinah" | `lgc_game_37781`, `aion-2-kinah` |
| Tổ hợp | 104 lá env (không attribute) | 81 offer live của GeGeTeam (không có Latin America) |
| Đơn vị | `Unit1000000`, USD/1M, bước 0,00001 | "Mil Kinah" |
| Giao | `AuctionHouse`, `Hour1` | `auction_house`, thủ công |
| Offer mình | 104 Active (tạo 22–23/10), hết hạn 12/11/2026 | 81 live (48 treo $30) |

Đợt tạo Eldorado (22/10 23:37 → 23/10 00:04):
- 104 POST đều 201, 0 lượt 429; pool `ElBudget` 750 → 646.
- `stateCount` Active 6 → 110; 13 offer cũ không đổi.
- Giá lúc tạo lệch bảng dry-run (board đọc trước đó 25–40 phút) ở 52/103 tổ hợp, nên phải đọc lại board
  ngay trước mỗi POST.

Tệp kết quả nằm trên `.199`, thư mục `~/el_aion2_20261022/`:
- `dryrun_aion2_el.{csv,md}`: bảng dry-run;
- `created.jsonl`: id và giá lúc tạo từng offer;
- `writes.jsonl`: mọi POST;
- `verify_all.json`: kết quả đo lại;
- `gets.log`: mọi GET.

---

## 5. Checklist cho game mới

1. **Eldorado — cấu trúc:** `/library` → `gameId`, category; `library/{gameId}/{category}` → cây env, đếm
   lá, `unitSystem`, `deliveryMethods`. Quá ~150 tổ hợp thì hỏi lại, vì đụng ngân sách POST và trần offer.
2. **Board:** đọc từng lá ở 1 lượt/s; kiểm `unitSystem` mọi dòng và số chữ số lẻ của giá (ra bước giá);
   đếm ca ≥ 8 dòng / < 8 dòng / trống.
3. **Offer mình:** `/me` (`pageSize` ≤ 50) + `stateCount`, chụp làm mốc "trước".
4. **Dry-run:** giá theo [2.5](#25-định-giá-theo-hạng), tồn/min/mô tả/giao theo [2.6](#26-tồn-min-mô-tả-thời-gian-giao);
   đưa người dùng duyệt.
5. **Đơn về ERP:** vá `ELDO_GAME_ID_TITLES` và cách lấy server/mặt hàng/số lượng
   ([2.10](#210-đơn-về-erp-qua-botpastedon--phải-xử-lý-trước-khi-bán)); kiểm ERP có Game Title, context và
   Currency Item tương ứng.
6. **Tạo 1 offer thử** qua `ElBudget`, đo lại (offer, board, `/me`, ledger), rồi mới tạo hết với các rào ở [2.7](#27-tạo-offer).
7. **Sau khi tạo:** quyết có đưa vào bot đạp giá không; ghi lịch kiểm `expireDate`.
8. **G2G:** điều tra như [3.1](#31-điều-tra-cấu-trúc-công-khai); cách tạo offer currency xem [3.3](#33-tạo-offer-currency--chưa-có-ghi-chép).

---

## 6. Script tham chiếu

Bản sao các script đã chạy cho Aion 2 nằm ở [refs/eldorado_currency_offers/](refs/eldorado_currency_offers/).
Chúng import mã của **bot đạp giá** (`ElBudget`, token Seller API, `EldoradoUpdater`), nên chỉ chạy trên `.199`:

```bash
ssh controller
mkdir -p ~/el_<game>_<yyyymmdd> && cd ~/el_<game>_<yyyymmdd>   # chep cac script vao day
export PYTHONPATH=~/Desktop/automation/G2G-AutomationBot-v4
PY=~/Desktop/automation/G2G-AutomationBot-v4/.venv/bin/python
$PY p01_library.py        # tim gameId
$PY p06_tree.py           # cay env -> tree.json
$PY p08_scan.py before    # combos.json, board_all.json, /me truoc, offer G2G
$PY p09_dryrun.py         # dryrun_*.csv/md
$PY p10_post_one.py <env> <ton> <min>          # chay kho: in payload
$PY p10_post_one.py <env> <ton> <min> --post   # tao 1 offer thu
$PY p11_verify.py <offer_id> <env>             # do lai offer thu
$PY p12_create_all.py --limit 1                # chay thu duong hang loat
nohup $PY p12_create_all.py > run_all.log 2>&1 &   # tao het, xong thi co run_all.done
$PY p08_scan.py after meonly                   # /me + stateCount sau
```

Trước khi dùng cho game khác, sửa các chỗ đang gắn cứng cho Aion 2: `gameId "360"`/`"Currency"` (p01, p06,
p08, p10, p12), mô tả + cách giao (`DESC`, `DELIVERY_*` trong p10/p12), ánh xạ region G2G và tồn mặc định (p09).
Không sửa repo, `.env` hay `settings.json` trên `.199`.
