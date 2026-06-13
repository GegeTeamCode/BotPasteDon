# Architectural Decisions Log

Append-only. Mỗi entry: ngày + quyết định + ngữ cảnh + alternative đã loại + lý do.
Mới nhất ở trên cùng.

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
