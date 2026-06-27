# Architectural Decisions Log

Append-only. Mỗi entry: ngày + quyết định + ngữ cảnh + alternative đã loại + lý do.
Mới nhất ở trên cùng.

---

## 2026-06-27 — Manual paste endpoint (ERP chủ động paste 1 đơn theo ID)

**Quyết định:** Mỗi scanner process (API mode) mở thêm HTTP listener
`POST /manual-paste` (G2G `:8771`, Eldo `:8772`, bind 0.0.0.0, guard header
`X-Manual-Secret`). ERP (.100) gọi vào để **paste 1 đơn cụ thể theo external order
id, BỎ QUA bộ lọc keyword**. Lý do: filter allow-all vẫn chặn `Any Gears`/`Any Items
- Aspects`/dịch vụ, nhưng owner đôi khi muốn ép 1 đơn gear/custom cụ thể vào ERP để
trader giao tay — thay vì nới filter (tạo đơn rác hàng loạt), cho phép **chủ động từng ID**.

**Cơ chế (tái dùng tối đa):** `handle_manual_paste()` dựng `order_info` rồi gọi đúng
code path của scanner — G2G `_extract_with_auth_retry` (start_deliver + mark_as_delivering
+ gate order_item_status), Eldo `extract_order_data` (get_order_detail read-only) — rồi
`send_erp_webhook` như scan thường. KHÔNG gọi `check_keywords` ⇒ không bị filter. Lưu
raw_data đầy đủ + erp_synced để `erp_retry_loop` recover nếu ERP down; ERP `new_order`
dedupe theo external_order_id ⇒ idempotent.

**ID nhập = order_item_id của G2G** (`1782...QETO`) nên fetch thẳng `/order/item/{id}`,
KHÔNG cần resolve. Eldo dùng UUID thẳng. (Verify: `_extract_order_id` ưu tiên
`order_item_id`, đúng giá trị mà owner nhìn thấy/dùng cho live API.)

**Alternative đã loại:**
- *Nới filter cho gear*: tạo ERP Sell Order rác cho mọi đơn gear, kẹt giao. Manual từng ID kiểm soát hơn.
- *Bot poll ERP (async queue)*: chịu được bot-down nhưng nhiều thành phần + không feedback tức thì. Owner chọn **đồng bộ** (chờ kết quả ngay).
- *Đặt endpoint ở dashboard :8766*: dashboard không có scanner instance (auth/api/extract). Gắn trong scanner process là chỗ có sẵn mọi thứ.

**Tác động:** G2G manual paste → đơn chuyển `delivering` trên sàn (trader giao tay) —
giống scan. Eldo an toàn hơn (không start_deliver). Config `.env`:
`MANUAL_PASTE_SECRET` + `MANUAL_PASTE_PORT_G2G/ELDO`. Phía ERP:
`site_config.manual_paste {secret,g2g_url,eldo_url}` + method `request_manual_paste` +
tab `/create`. Deploy đúng pipeline: bot main-direct (e0f8424), ERP `develop→PR→main`
(frappe #88, UI #54) → `deploy_git.py prod`. Doc: [`docs/manual_paste.md`](../docs/manual_paste.md).

---

## 2026-06-27 — Đảo scanner filter sang allow-all (gear giờ lọt ERP)

**Quyết định:** Đổi [`SCANNER_WHITELIST`](../shared/config.py) sang **rỗng** + blacklist
= `Any Gears, Any Items - Aspects, Boosting, Leveling, Account, Custom oder`. Nghĩa là
filter bị **đảo** từ "chỉ cho phép whitelist" sang **"chặn blacklist, cho phép tất cả
còn lại"**. Lý do: owner muốn **gear cụ thể** (Mageblood Utility Belt, Headhunter,
Temporalis, Widow's Web…) giờ được paste sang ERP để trader **giao tay** — trước đó
chúng bị whitelist drop ngay ở scanner (`status=DETECTED`, không vào ERP). Hai pattern
`Any Gears` / `Any Items - Aspects` vẫn bị chặn vì là listing "any/bulk" mà worker
không fulfil được (không item cụ thể → sẽ tạo ERP Sell Order rồi kẹt giao).

**Ngữ cảnh:** Đơn `1782433314325QBNQ` (Mageblood Utility Belt) điều tra thấy `DETECTED`
trong DB, `raw_data` chỉ `{"itemName": ...}`, `game=''` — dấu hiệu chuẩn của đơn rớt
whitelist ở `scan_order_list` (chưa qua `_map_order_data`). Audit 2026-06-25 đã liệt
32 đơn DETECTED tương tự; ~27 rớt whitelist (gear), 5 dính blacklist `Any Items`. Cùng
dấu vân tay.

**Alternative đã loại:**
- *Thêm tên từng loại gear vào whitelist* (Mageblood, Headhunter…): mỏng, bỏ sót mỗi
  loại gear mới, phải maintain liên tục. Allow-all đơn giản hơn.
- *Bỏ hẳn cả Boosting/Leveling/Account khỏi blacklist*: tạo ERP Sell Order rác cho dịch
  vụ team không làm. Giữ lại (chốt owner).

**Cơ chế (không đổi code, chỉ config):** [`check_keywords`](../scanners/base_scanner.py)
chạy **blacklist trước, whitelist sau**. Whitelist rỗng → skip block whitelist →
allow-all. Deploy: edit `.env` trên `.220` (gitignored runtime state — `deploy_git.py`
git reset không đụng) → restart 2 scanner (watchdog-safe). `.env` backup
`.env.bak-20260627-020841`.

**Verify** (`scripts/_verify_filter.py`, chạy trên `.220` nạp config thật): 10/10 case
đúng — DROP `Any Gears`/`Any Items - Aspects`/`Boosting`/`Account`; PASTE Mageblood/
Headhunter/Temporalis/Widow's Web/Divine/Gold.

**Tác động G2G:** mọi đơn qua filter sẽ bị scanner gọi `start_deliver` +
`mark_as_delivering` (lấy delivery info) → đơn gear chuyển sang `delivering` trên G2G,
trader phải claim + giao tay + upload proof. Eldorado không start_deliver (chỉ get
detail) nên an toàn hơn. Đơn DETECTED cũ (đã xử lý riêng theo owner) **không** tự
re-paste — `is_order_processed` cache theo order_id bất kể status.

**Doc:** [`docs/order_filtering.md`](../docs/order_filtering.md) cập nhật (TL;DR +
config table + section mới "Phương thức filter mới"). Defaults repo (`shared/config.py`,
`.env.example`) đồng bộ theo ý định mới.

---

## 2026-06-26 — ERP-driven reconcile (lỗ hổng g2g list-window) + Eldo dispute parity

**Quyết định:** Thêm `status_sync/erp_reconcile.py::reconcile_from_erp` — gọi ERP endpoint
`get_pending_marketplace_orders(g2g)` lấy đơn non-terminal của ERP → lookup `get_order_detail` per-order
→ push terminal thật. Chạy mỗi `ERP_RECONCILE_EVERY_N_CYCLES` cycle (g2g_sync). Throttle + batch +
back-off (qua `marketplace_status.last_synced_at`) + dừng khi 429. Đóng lỗ hổng list-window (status_sync
chỉ thấy ~100 đơn mới nhất → đơn cũ kẹt; prod ~578 đơn G2G). ERP stateless, bot giữ throttle/back-off.

**Ngữ cảnh:** `reconcile_unpushed` cũ là bot-driven (chỉ đẩy đơn bot ĐÃ có) → vô dụng vì bot không có đơn cũ.

**Alternative đã loại:** ERP track per-order back-off (thừa — bot dùng last_synced_at); paginate full list g2g
(vẫn thiếu vì list endpoint không trả đơn cũ); import `RateLimitError` từ g2g_api (làm module kéo curl_cffi,
mất khả năng unit-test → thay bằng `_is_rate_limit` check `status==429`).

**Eldo dispute parity (EL-2/EL-3):** eldo_sync push `disputed` kèm `report_reason=latestDispute.reason`
(Eldo gộp cancel+dispute vào 1 state `Disputed`; classifier trong latestDispute, disputedByUserRole luôn=Buyer)
→ ERP "Dispute Open" + reason. EL-3: đơn completed có cờ `hasBeenRefundedPostCompletion` → push canceled →
ERP đảo ví (belt-and-suspenders; hiện=0). ERP side (EL-1 clear-on-resolve + E-1 endpoint): gege_custom
`feat/g2g-reconcile-eldo-dispute`. Test `tests/test_erp_reconcile.py` 3/3 (basic/backoff/rate-limit).
Config: `shared/config.py` ERP_RECONCILE_*. Deploy ERP TRƯỚC → bot.

---

## 2026-06-26 — G2G cancel/resolution: push alert NON-BLOCKING + ERP tự quyết tiền/state

**Quyết định:** `status_sync/g2g_sync.py::_sync_cases` phân loại G2G case theo `report_case`
(`cancel`→`cancel_requested`, còn lại→`disputed`) và push **alert NON-BLOCKING** sang ERP: ON khi
case mở (`open`/`escalate`), OFF khi `close`. ERP chỉ set/clear field `custom_marketplace_alert` —
**KHÔNG đổi `workflow_state`** → trader vẫn giao hàng. Terminal (`cancelled`) ERP tự quyết: chưa từng
credit ví → `Cancelled`; đã credit → `Refunded` + đảo ví (mirror ALE In→Out, không đụng kho).

**Ngữ cảnh:** User báo (1a) đơn chưa completed bị khách mở Resolution → G2G "cancel request" nhưng ERP
không cảnh báo; (1b) đơn đã Completed bị G2G cancel → tiền đòi lại nhưng ERP không trừ ví. Điều tra DB
live `.220`: `report_case` = `cancel` (239) / `did_not_receive` (7) là classifier thật; `marketplace_status`
không có `cancellation_requested` → tín hiệu chỉ ở `marketplace_disputes`.

**Alternative đã loại:**
- Map case → workflow_state `Disputed` (cũ): SAI — chặn nút giao hàng + không phân biệt cancel/dispute.
- Chỉ push khi `status=='open'` (cũ): trượt vì đa số case đã `close` lúc sync; + không clear khi đóng.
- Thêm cột `last_pushed_alert`: thừa — tái dùng `notified_pushed_at` làm cờ "ERP có alert active"
  (set khi ON OK, clear khi OFF OK) → idempotent + retry khi push fail + không spam history đã close.

**Implementation:** `_classify_case` + `_OPEN_CASE_STATES={open,escalate}` + `db.set_dispute_notified`.
ERP side: `gege_custom/.ai/decisions.md` 2026-06-26 (field + `_reverse_marketplace_wallet` + tách
Cancelled/Refunded). Plan: `.ai/current-plan.md`. Deploy: ERP prod TRƯỚC → `deploy_git.py status_sync`.

---

## 2026-06-14 — ERP `status_update`: ghi Order Log thủ công + integration user BotPasteDon

**Quyết định:** Trong `gege_custom api/botpastedon.py::status_update`, sau
`frappe.db.set_value(...)` phải **ghi Order Log thủ công** + publish realtime:

```python
create_order_log(reference_doctype="Sell Order", reference_name=so_name,
                 action="State Change", from_state=current, to_state=target,
                 performed_by=(_BOT_LOG_USER if frappe.db.exists("User", _BOT_LOG_USER) else None),
                 note=f"Marketplace sync: {platform}/{mp_state}")
frappe.publish_realtime("list_update", {"doctype": "Sell Order"}, after_commit=True)
```
`_BOT_LOG_USER = "botpastedon@gegeteam.net"`.

**Ngữ cảnh:** Order card có bảng Order Log, được điền tự động bởi doc_events hook
`order_log_on_update` (utils.py) — hook này chỉ chạy trên `.save()` (on_update).
Vì `status_update` đã đổi sang `db.set_value` (để tránh 403, xem entry 2026-06-13),
nó **bỏ qua doc lifecycle → hook không bắn → transition Delivered→Completed qua
webhook KHÔNG ghi log** (và không refresh realtime list). Fix: replicate y việc
hook làm — tạo Order Log `action="State Change"` + `frappe.publish_realtime`.

**Integration user:** `Order Log.created_by` là **Link(User)**. Đặt thẳng chuỗi
"BotPasteDon" sẽ fail link validation → status_update lỗi lại. User "BotPasteDon"
**không tồn tại ở đâu** (đã tìm prod + 3 site dev: 0 user dính bot/paste; Bot
Credential chỉ có "Eldorado Bot"/"G2G Bot"). Đã **tạo User** `botpastedon@gegeteam.net`
(full_name "BotPasteDon", login disabled, user_type System User) trên **CẢ dev `.228`
LẪN prod `.100`**. Code dùng `performed_by` defensive (`if exists else None` →
fallback session user) nên không bao giờ vỡ webhook dù user vắng.

**Alternative đã loại:**

- *Quay lại `.save()` để hook tự ghi log*: kéo lại bug 403 permission. Loại.
- *Ghi "BotPasteDon" vào note, created_by giữ Guest*: cột "ai thực hiện" vẫn Guest,
  không đúng yêu cầu. Loại.
- *`set_user("BotPasteDon")` (như new_order)*: user không tồn tại → roleless; và với
  Link field thì `ignore_permissions` không bỏ qua link validation. Loại.

**Trạng thái deploy:** Code ĐÃ áp + verify trên **dev `.228`** (test rollback: Guest
tạo được Order Log, created_by hiển thị "BotPasteDon"). **Prod `.100`: user đã tạo,
nhưng code `botpastedon.py` mới (Order Log + block In Delivery + performed_by) CHƯA
deploy** — bộ phận ERP deploy trọn gói từ dev working tree.

---

## 2026-06-14 — G2G delivery: idempotent "already delivering" + skip unsupported proof

**Quyết định:** Hai sửa ở delivery dispatch (money flow):

1. **`workers/g2g_worker.py::handle_g2g_api` Step qty** — khi `submit_delivered_qty`
   báo `cannot perform action when order item status is delivering/delivered`,
   KHÔNG mark FAILED. Gọi `get_order_detail` xác nhận `delivered_qty >= qty` và
   `order_item_status` không phải cancelled/refunded → coi qty đã xong, **resume
   proof/chat** (helper `_qty_already_delivered`). Order bị hủy/hoàn (qty mismatch)
   vẫn re-raise → terminal → FAILED.
2. **`shared/g2g_api.py::_upload_proofs`** — G2G `upload_url` chỉ nhận
   `{jpg,jpeg,png,gif,mp4,mov}`, từ chối `webp/heic/no-ext` (verified live). Cô lập
   từng file: skip đuôi không hỗ trợ + per-file try/except → submit phần upload
   được. Nếu KHÔNG file nào upload được → raise terminal `proof file(s) unsupported`
   (manual) thay vì retry vô hạn / complete không proof. Content-type map thêm gif/mov.

**Ngữ cảnh:** 4 order đã giao (delivered_qty đúng, awaiting_buyer_confirmation) nhưng
DB kẹt FAILED/RETRY_PENDING. (a) 3 order: double-dispatch (scanner trùng) → lần 2
gặp "already delivering" → `_classify_error` map terminal → FAILED, dù hàng ĐÃ giao.
(b) 1 order (LVB9): proof có file `.webp` → `get_upload_url` raise → cả step proof
chết → retry vô hạn (attempt 56/100).

**Alternative đã loại:**

- *Map "cannot perform action..." → COMPLETED (đề xuất glm)*: nguy hiểm — keyword là
  prefix, khớp cả `...cancelled/refunded` → đánh dấu order hủy thành COMPLETED = lỗi
  tiền. Phải parse status + re-query `delivered_qty`.
- *Flip 4 row sang COMPLETED trực tiếp*: 3 order có `skip=None` → proof CHƯA upload;
  flip mù sẽ mất proof. Resume mới đúng.
- *Convert webp/heic→jpg*: cần Pillow (chưa cài trên server) → để sau; trước mắt skip.

**Root cause double-dispatch:** scanner trùng — đã fix ở phiên 2026-06-13 (watchdog
token + single launcher).

**Deploy:** code đã push + deploy lên bot prod `.220` (worker_g2g). Recovery 4 order:
xử riêng (LVB9 tự heal qua retry; 3 order FAILED cần re-dispatch).

---

## 2026-06-13 — ERP `status_update` REGRESSION: khôi phục `db.set_value` + block `In Delivery`

**Quyết định:** `gege_custom api/botpastedon.py::status_update` quay lại
`frappe.db.set_value("Sell Order", so_name, "workflow_state", target)` +
`frappe.db.commit()`, và **khôi phục guard** `_BLOCK_CURRENT_STATES = {"In Delivery"}`
→ trả `{"status": "manual_required"}`, KHÔNG mutate. Đây là tái khẳng định 2 quyết
định cũ (2026-06-07 PROTECTED states, 2026-06-10 set_value + block In Delivery) mà
code đã regress khỏi.

**Ngữ cảnh:** status_sync push lên prod ERP **fail 100%** (18/18 reject, 0 success).
Nguyên nhân: ai đó đã đổi `status_update` sang `frappe.get_doc(...).save()` (để fire
before_save inventory hooks) VÀ đánh mất guard `In Delivery`. Hậu quả khi chạy as
Guest (`allow_guest=True`):
- **HTTP 403 PermissionError**: `.save()` đổi `workflow_state` trigger
  `validate_workflow → check_permission` trên pre-save snapshot — snapshot KHÔNG
  carry `flags.ignore_permissions` (quirk Frappe v15) → Guest bị chặn.
- **HTTP 417 MandatoryError**: `.save()` enforce field bắt buộc `currency_item`
  (một số SO tạo qua fallback thiếu field này).

`db.set_value` ghi thẳng field → bỏ qua doc lifecycle/workflow/permission/mandatory
→ hết cả 403 lẫn 417. **An toàn vì** `In Delivery` là state webhook-reachable DUY
NHẤT cần before_save inventory hooks (`_deliver_locked_inventory` / lock release);
block nó đi thì mọi target còn lại (Completed/Refunded/Disputed/Delivered-từ-state-khác)
không đụng hook nào → bỏ hook không mất gì.

**Alternative đã loại:**

- *Giữ `.save()` + `set_user("BotPasteDon")`*: user `BotPasteDon` KHÔNG tồn tại trên
  cả dev `.228` lẫn prod `.100` (verified) → roleless → `validate_workflow` vẫn 403.
  Đúng cái 2026-06-10 đã loại.
- *`set_user("Administrator")`*: privilege escalation + audit dilution (đã loại 2026-06-10).
- *Giữ `.save()` chạy dưới integration user thật có role workflow*: giữ được inventory
  hooks nhưng phải tạo/cấp role user mới; user chọn bỏ hook (db.set_value) vì các
  transition webhook không cần hook khi đã block In Delivery.

**Verify (dev `.228`, site test.localhost, rollback — không đổi data):** Guest
`db.set_value` Claimed→Completed OK; Guest `.save()` → PermissionError (đúng);
`_BLOCK_CURRENT_STATES = {"In Delivery"}` hiện diện.

**Trạng thái deploy:** ĐÃ áp + verify trên **dev `.228`**. **Prod `.100` CHƯA** —
bộ phận ERP sẽ deploy lên prod sau và thông báo. Tới lúc đó status_sync trên prod
vẫn 403/417 (bot không sai; order được giữ + retry mỗi 30 phút).

---

## 2026-06-13 — Supervisor: GIỮ watchdog/heartbeat, watchdog tự được systemd giám sát

**Quyết định:** Mô hình giám sát process là **một launcher duy nhất**:
`botpaste.service` (Type=forking) → `scripts/start.sh` nohup 8 service →
`watchdog.py` giám sát qua heartbeat trong `data/orders.db` và respawn theo tier.
Bản thân watchdog được bọc trong `deploy/bot-watchdog.service` (`Restart=always`,
`After=`+`PartOf=botpaste.service`) để không còn là single point of failure.
start.sh khởi động watchdog bằng `systemctl start --no-block bot-watchdog`
(idempotent, có nohup fallback cho dev box); stop.sh `systemctl stop
bot-watchdog` TRƯỚC khi kill để Restart=always không hồi sinh nó giữa lúc tear-down.

**KHÔNG enable `bot-*.service` per-service song song với botpaste.service.** Hai
supervisor cùng lúc = nguồn duplicate-on-boot. (6 unit `bot-*.service` +
`start_all.sh`/`stop_all.sh` chưa từng được cài trên server — file chết, là mìn
ngầm — nên đã XÓA khỏi `deploy/` trong cùng commit này.)

**Ngữ cảnh:** Sự cố duplicate scanner 2026-06-13. Điều tra server: chỉ
botpaste.service enabled, không duplicate thực tế. Nhưng watchdog (pid nohup từ
start.sh) không được ai dựng lại nếu nó chết → toàn bộ self-heal sập âm thầm.

**Alternative đã loại:**

- *Chuyển hẳn sang systemd per-service (`Restart=on-failure`)*: loại vì systemd
  chỉ phản ứng khi process **exit**, KHÔNG bắt được trường hợp **treo** (Camoufox
  `close()`-spin, Chrome hang) — vốn rất hay xảy ra ở browser automation. Heartbeat
  bắt được treo. Ngoài ra restart 1 unit không chạy được cleanup chéo của start.sh
  (kill orphan chromedriver/camoufox, xóa SingletonLock 4 profile, free 5 port),
  và mất restart theo tier phụ thuộc (auth → workers → coordinator → scanners).
  `WatchdogSec` trong các unit cũ vô dụng vì service ghi heartbeat SQLite chứ
  không gọi `sd_notify(WATCHDOG=1)`.

**Liên quan:** Bug watchdog `find_running_pids` — token scanner trước đây là
`"scanners.main"` cho cả 2 platform nên 1 scanner sống che 1 scanner chết
(watchdog không restart cái chết). Đã fix: token kèm `--platform <plat>`.

---

## 2026-06-10 — g2g_scanner Step 3: retry `get_order_detail` 3 lần, KHÔNG scan `delivering` state

**Quyết định:** Trong `scanners/g2g_scanner_api.py::_do_extract`, wrap Step 3
(`get_order_detail` re-fetch) trong retry loop 3 attempts + backoff 2s/4s.
Nếu hết retry → log ERROR rõ "orphaned, manual recovery needed" + return None.

**Ngữ cảnh:** Order `1781067547077COBM` lost forever do curl timeout 30s ở
Step 3. Steps 1+2 (`start_deliver` + `mark_as_delivering`) đã commit
`delivering` trên G2G, nhưng bot không insert DB → buyer chờ vô hạn.

**Alternative đã loại:**

- *Thêm scan `delivering` state vào scanner để recover orphan*: user reject —
  "vấn đề thêm delivering vào scanner là không cần thiết". Lý do: retry
  `get_order_detail` là đủ vì state đã locked, không drift được.
- *Insert minimal record vào DB TRƯỚC Steps 1+2*: thêm logic 2-phase commit
  cho 1 case hiếm; over-engineering.
- *Synthesize order_data từ `order_info.raw` (pending list snapshot) khi Step
  3 fail*: thiếu fields (attributes, server, character) cần cho keyword
  matching + ERP payload → sai data còn tệ hơn.

---

## 2026-06-10 — ERP `status_update` HẠ TẦNG: `frappe.db.set_value`, KHÔNG `set_user("Administrator")`

**Quyết định:** Trong gege_custom `api/botpastedon.py::status_update`, dùng
`frappe.db.set_value("Sell Order", so_name, "workflow_state", target)` +
`frappe.db.commit()` để apply transition. KHÔNG gọi
`frappe.set_user("Administrator")` ở bất kỳ webhook nào.

**Ngữ cảnh:** Webhook `allow_guest=True` chạy as Guest. `frappe.get_doc(...)`
+ `so.save()` trigger `validate_workflow → get_transitions →
check_permission("read")` trên pre-save snapshot — snapshot KHÔNG carry
`flags.ignore_permissions` → raise PermissionError, dù đã `flags.ignore_permissions = True`.

**Alternative đã loại:**

- *`frappe.set_user("Administrator")`*: tạo privilege escalation surface
  (mọi code path downstream chạy as Admin với input từ webhook), audit
  dilution (`modified_by = Administrator` che mờ ai thực sự sửa), hook
  behavior khác biệt (một số `before_save` skip validation cho Admin),
  forget-to-reset risk.
- *`frappe.set_user("BotPasteDon")`*: scoped user nhưng BotPasteDon role
  KHÔNG có Sell Order read perm (by design) → vẫn fail
  PermissionError vì `validate_workflow` chạy permission check.
- *`so_doc.flags.ignore_permissions = True` + `so_doc.save(ignore_permissions=True)`*:
  flag KHÔNG propagate qua `_doc_before_save` snapshot inside
  `validate_workflow` — đây là Frappe v15 behavior quirk.

**Trade-off đã chấp nhận:** Skipping `save()` cũng skip `after_save` realtime
publish → ERP UI không auto-refresh trên transition này. Operator thấy
update khi manual refresh. Acceptable vì status_sync chạy 30 phút/cycle,
operator không stare cùng SO continuously. Whitelisted targets
(Completed/Disputed/Refunded) có NO branch trong `Sell Order.before_save`
nên skip pipeline an toàn.

---

## 2026-06-10 — `status_update` BLOCK khi current=`In Delivery`

**Quyết định:** Thêm `_BLOCK_CURRENT_STATES = {"In Delivery"}` vào
`status_update`. Khi `current ∈ BLOCK` → return `manual_required`, log
Warning vào WS Activity Log, KHÔNG mutate. Operator phải transition thủ công.

**Ngữ cảnh:** State `In Delivery` đại diện trader đang giao + giữ inventory
locks (lot account, stock movements). Marketplace push (`delivered`,
`completed`) đến trong khi trader chưa kịp `Evidence Uploaded` → bot tự
ý jump `In Delivery → Completed` sẽ skip toàn bộ business logic của
`_deliver_locked_inventory()` (chỉ chạy ở `In Delivery → Delivered`
transition).

**Alternative đã loại:**

- *Allow `In Delivery → Delivered` qua webhook*: workflow definition không
  có direct transition này (legal path là Evidence Uploaded → Deliver).
  Hardcode bypass sẽ skip inventory lock release.
- *Allow nếu marketplace state là `received`*: vẫn skip stock movements →
  inventory drift.

---

## 2026-06-10 — `_SAFE_TRANSITIONS` whitelist

**Quyết định:** Map cứng (source → {target}) cho transitions bot được apply
qua `status_update`:

```
Delivered     → {Completed, Disputed, Refunded}
Outstanding   → {Completed, Refunded}
Completed     → {Disputed}
Disputed      → {Refunded, Completed}
```

Anything ngoài map → return `unsafe_transition`, log Warning.

**Ngữ cảnh:** Whitelist explicit hơn workflow validation vì marketplace
push KHÔNG có context để judge transition khác. Bot không nên push từ
`Queued/Claimed/Evidence Uploaded` (chưa hoàn tất delivery flow internal).

**Alternative đã loại:**

- *Trust `workflow_state` transitions của Frappe workflow*: yêu cầu permission
  check (lý do dùng `db.set_value`); workflow cũng có transitions không
  phù hợp cho marketplace push (vd manual cancel paths).
- *Allow tất cả transitions trừ blacklist*: nguy hiểm — không biết workflow
  sẽ add state mới gì trong tương lai.

---

## 2026-06-10 — Phase 5 G2G: backend refresh `POST sls.g2g.com/user/refresh_access`

**Quyết định:** Refresh G2G JWT bằng cách POST endpoint nội bộ
`https://sls.g2g.com/user/refresh_access` với body
`{user_id, refresh_token, active_device_token, long_lived_token}`.
`curl_cffi` impersonate `chrome120` cho TLS fingerprint. Selenium CDP fallback
giữ nguyên cho lần init đầu / refresh_token expire.

**Ngữ cảnh:** Trước Phase 5, G2G JWT refresh = Selenium CDP capture mỗi
13 phút (~30-60s/lần). Phase 5 rút xuống ~1s/lần. JWT `iss=G2GSls` →
self-issued, không qua Cognito broker → không gặp `SECRET_HASH wall` của
Eldo.

**Discovery story:**

- Dump cookies + decode JWT → tìm thấy `refresh_token` / `long_lived_token`
  / `active_device_token` cookies + `sub` claim làm `user_id`.
- CDP network sniff (`scripts/_sniff_g2g_refresh.py`) → bắt được request
  `POST sls.g2g.com/user/refresh_access` 200 OK trong page navigate dashboard.
- JS bundle decompile (`scripts/_g2g_js_grep.py`) → tìm thấy exact body
  schema trong `https://www.g2g.com/js/app.<hash>.js`.
- Final confirm (`scripts/_probe_refresh_access_final.py`) → HTTP 200,
  new JWT working.

**Alternative đã loại:**

- *Tiếp tục Selenium-only*: chậm + tốn resource (browser instance liên tục).
- *Generic /auth/refresh endpoints* (14 probed): tất cả return 401/403/404/HTML
  SSR.

**Reference:** `docs/marketplace_auth.md` — endpoint contract + discovery
methodology + reusable cho marketplace mới.

---

## 2026-06-10 — Logging: GMT+7 timezone + date trong format

**Quyết định:** `shared/logging_config.py` set `LOG_TZ = timezone(+7)` +
format `[YYYY-MM-DD HH:MM:SS][name] LEVEL: msg` (thay vì `[HH:MM:SS]` cũ).

**Ngữ cảnh:** Bot deploy ở `Asia/Ho_Chi_Minh` (GMT+7, no DST). Log default
chạy UTC + chỉ hh:mm:ss → hard để correlate với marketplace timestamp +
khó debug log cross-day. Add date prefix + GMT+7 = ops dễ track timeline.

**Implementation:** `logging.Formatter` + custom `converter = _tz_converter`
trả `datetime.fromtimestamp(ts, tz=LOG_TZ).timetuple()`.

---

## 2026-06-10 — Worker retry indefinitely (max 100 attempts), exponential backoff

**Quyết định:** Workers (g2g + eldo) khi gặp `auth/network/terminal/unknown`
error sẽ mark order `RETRY_PENDING` rồi retry. Max 100 attempts, backoff
60s×5 → 5m×5 → 30m×5 → 1h cap. Recovery loop pick up due retries mỗi cycle.
`cleanup_old_orders` chỉ xóa COMPLETED (không xóa FAILED / RETRY_PENDING).

**Ngữ cảnh:** Trước fix, order fail upload proof → mark FAILED → cleanup
xóa sau 7 ngày. Buyer never gets evidence → seller bị withhold payment.

**Alternative đã loại:**

- *Cap số attempts thấp (vd 5)*: vẫn cho phép cleanup xóa, dẫn đến mất
  permanent. User chỉ định cap 100 = thực tế "indefinite" nhưng có ceiling
  tránh infinite loop bug.
- *Discord notification khi vượt cap*: user reject — không cần, chỉ log
  server-side là đủ.

**Quyết định:** Refresh Eldorado IdToken bằng cách POST đến endpoint nội bộ
`https://www.eldorado.gg/api/authentication/refreshTokens` với cookies + XSRF.
Không gọi `cognito-idp.us-east-2.amazonaws.com` trực tiếp.

**Ngữ cảnh:** Cookie `__Host-EldoradoIdToken` (JWT từ Cognito) TTL ~1h. Cần
refresh tự động mỗi cycle, không bắt user re-login (RefreshToken TTL ~30 ngày).

**Alternative đã loại:**

- *AWS Cognito OAuth2 token endpoint trực tiếp*: trả 400 BadRequest. Cognito
  client `3a4hal6jgl8gf5hnnjo06k05s5` configured với client secret →
  request thiếu `Authorization: Basic` header.
- *AWS Cognito `InitiateAuth` với `REFRESH_TOKEN_AUTH`*: trả 400 với
  `NotAuthorizedException: Client ... is configured with secret but
  SECRET_HASH was not received`. Để tính SECRET_HASH cần `client_secret`
  mà ta không có (nó sống trong Eldorado backend).
- *Camoufox visit homepage mỗi cycle*: chậm (~30s) và làm strip cookies
  của profile khi session đã chết (Eldo response set-cookie clear).

**Phát hiện endpoint:** Camoufox + `page.on("response")` listener với
`page.goto("/dashboard/orders/sold")`. Endpoint chính xác sau ~24h debug.

**Reference:** `docs/ref_223_control_auth_manager.py` (downloaded từ
`192.168.2.223:/root/G2G-AutomationBot-v4/control/auth/manager.py`) có pattern
tương tự nhưng dùng Cognito direct — chứng minh approach đó từng work cho
một client_id khác hoặc trước khi Eldo enable client secret.

---

## 2026-06-08 — Camoufox capture: cookie-preservation guard

**Quyết định:** Trong `EldoAuth.capture()`, sau khi Camoufox trả về data, so
sánh với bundle cũ — nếu bundle mới mất `__Host-EldoradoRefreshToken` HOẶC
`__Host-EldoradoIdToken` so với trước → reject, giữ `self.data` cũ.

**Ngữ cảnh:** Khi Camoufox visit Eldorado page với IdToken expired (và
backend refresh fail), Eldo response set-cookie clear → cookies bị strip.
Nếu ta nhận bundle stripped đó vào `self._last_cookies`, Cognito/backend
refresh cycle sau không có RefreshToken nữa → death spiral.

**Alternative đã loại:**

- *Probe API trước rồi mới Camoufox*: vẫn không tránh được vì backend
  refresh cần XSRF cookie mà chỉ Camoufox capture được.
- *Always re-login khi mất tokens*: yêu cầu can thiệp tay quá thường xuyên.

---

## 2026-06-08 — ELDO_SELL_PAGE = `/dashboard/orders/sold`

**Quyết định:** URL Camoufox visit để trigger refresh + capture headers
là `https://www.eldorado.gg/dashboard/orders/sold`, không phải `/sell/...`.

**Ngữ cảnh:** Phase 2 ban đầu dùng `/seller/orders` → trả SSR 404 → page JS
không boot, không refresh được. Reference 223 dùng `/sell/offer/Currency/2`
hoặc tương tự (sell-side, không phải dashboard). User chỉ định endpoint
mới `/dashboard/orders/sold` đang work tốt — preserve.

---

## 2026-06-06 — Skip `webdriver_manager` cho G2G Chrome

**Quyết định:** `auth/main.py` glob `~/.wdm/drivers/.../chromedriver` trực
tiếp, fallback `ChromeDriverManager().install()` chỉ khi binary chưa tồn tại.

**Ngữ cảnh:** `webdriver_manager.ChromeDriverManager().install()` mở
`FileLock` trên `~/.wdm/.wdm-lock-chromedriver-linux64`. FD bị leak vào
auth process → các lần `init_driver()` sau tự deadlock chính lock đó.
Triệu chứng: `/auth/g2g` hang 30s mặc dù `/health` vẫn cached.

**Alternative đã loại:**

- *Pre-clean lock file mỗi lần init_driver*: race condition khi nhiều
  thread cùng tạo driver.
- *Pin webdriver_manager version cũ*: chưa biết version nào không leak,
  và tự chứa đầy đủ binary đáng tin hơn.

---

## 2026-06-07 — status_sync first-run silent backfill

**Quyết định:** Lần đầu chạy `status_sync` trên DB clean: scan toàn bộ
state hiện tại của mỗi marketplace, insert vào `marketplace_status` nhưng
**KHÔNG push ERP** (`push=False`). Cycle 2 trở đi mới push delta.

**Ngữ cảnh:** Backfill 10k+ orders mà push tất cả → spam ERP webhook +
mass-update `workflow_state` cho rất nhiều Sell Order với "state mới" mà
thực ra là state hiện tại — gây confusion + audit log noise.

**Phát hiện first-run:** `not db.get_marketplace_state_counts(platform)`
trong `*.run_once()` (state_counts table rỗng = chưa từng chạy).

---

## 2026-06-07 — ERP state mapping: `g2g.cancelled → Refunded`, không `Cancelled`

> ⚠️ **SUPERSEDED 2026-06-26** (xem entry trên cùng): không còn "mọi cancelled→Refunded".
> ERP tự quyết theo sổ của chính nó — chưa từng credit ví → `Cancelled`; đã credit → `Refunded` + đảo ví.

**Quyết định:** Khi G2G chuyển order sang `cancelled`, ERP `workflow_state`
set thành `Refunded` (không phải `Cancelled`).

**Ngữ cảnh:** User chỉ định: G2G cancellation = refund đã được xử lý phía
G2G → ERP cần phản ánh trạng thái tài chính (đã refund) chứ không phải
trạng thái workflow (đã cancel). `Cancelled` workflow_state có nghĩa khác
trong ERP nội bộ (chưa xử lý).

---

## 2026-06-07 — ERP PROTECTED states không bị override

**Quyết định:** Webhook `status_update` không bao giờ override workflow_state
nằm trong set `{Refunded, Partially Refunded, Cancellation Requested,
Outstanding, Payment Pending}`. Trả `{"status": "protected"}`.

**Ngữ cảnh:** 5 state này do nhân viên ERP set bằng tay sau khi xử lý thủ
công (vd refund 1 phần, customer request cancel). Bot không được "đẩy
ngược" về workflow tự động.

**Implementation:** Trong `botpastedon.py` server-side webhook, check
`current in PROTECTED` trước khi `set_value`.

---

## 2026-06-07 — status_sync interval = 30 phút

**Quyết định:** `STATUS_SYNC_INTERVAL_SEC = 1800` (30 phút).

**Ngữ cảnh:** Trade-off latency vs API hammering. Marketplace state thay
đổi không gấp (vài giờ thường ok). Counts endpoint là cheap (1 API call)
nên rẻ cho tripwire. List endpoint chỉ chạy khi có delta.

---

## 2026-06-07 — Eldorado state filter: tracked = `{Delivered, Disputed, Completed, Canceled}`

**Quyết định:** status_sync chỉ poll 4 state trên. `PendingDelivery` và
`Received` bỏ qua.

**Ngữ cảnh:**

- `PendingDelivery` = đơn chưa giao → coordinator + scanner đã handle.
- `Received` = customer đã confirm → giống `Delivered` về workflow ERP,
  không cần push thêm.
- Còn lại 4 state là các trạng thái terminal/dispute thực sự cần sync.
