# Task Log

Mỗi dòng: `YYYY-MM-DD — tóm tắt 1-line — commit (nếu có)`. Append-only,
mới nhất ở trên cùng.

---

## 2026-10-22

- `fix(chat): kiem phan hoi Sendbird, kenh du phong, gui lai loi nhan CHAT_PENDING`
  - G2G: `_parse_sendbird` (loi → `SendbirdError`; ma 4003xx/400401 → `SendbirdKeyError` + ops alert
    `g2g-sendbird-key`); `chat/channel` loi/401/thieu `channel_url` → tim kenh co san qua Sendbird
    `members_exactly_in`; bo ten kenh tu dung `g2g_dm_{buyer}_{seller}` (sai 17/50 kenh, do 22/10).
  - Bo vong thu lai chat ngay sau loi auth (co the goi G2G bang JWT vua chet → G2G da phien);
    luot quet gui lai gap G2G tu choi JWT thi dung ca luot.
  - Ca hai san: chat hong → don van `COMPLETED`, `error_message=CHAT_PENDING:`, `retry_data={"chat"}`;
    vong hoi phuc 60s gui lai rieng loi nhan, gian 1/5/30/60 phut, 5 luot roi `CHAT_STUCK:` + alert.
  - Logger `g2g.api`, `eldo.api` dung `setup_logger` (truoc do mat dong INFO "Chat sent").
  - Xoa `full_delivery` chet o `g2g_api.py`/`eldo_api.py`.
  - Verify: `tests/test_marketplace_chat.py` (16 phep kiem, Sendbird/G2G/TalkJS gia + DB tam),
    `tests/test_talkjs.py` van xanh, import scanners/status_sync OK. Venv test Python 3.14 + dung ban
    thu vien nhu .220 (server chay Python 3.10 — ma khong dung cu phap moi hon 3.10).
  - Doc-sync: `docs/marketplace_chat.md` (tai lieu moi cung ngay), `docs/architecture.md`,
    `docs/proof_mechanism.md` (bang chung Eldorado khong con "non-fatal" tu `533b305`),
    `docs/seller_api_migration_plan.md` (bang 2.3: khong co duong REST cho loi nhan).
  - Kiem truoc do (22/10, GET chi doc len Sendbird tu .220): chat G2G chi can Session-Key; khoa tu
    20/05/2026 van song; 20/20 don Completed gan nhat co the don + loi nhan tren Sendbird.

## 2026-09-15

- `feat: remove Discord entirely — scanners push orders straight to ERP` — commit `180c0a8`, deploy .220
  - Nguyên nhân: Discord 429 `Retry-After: 3000` làm scanner G2G ngủ 50 phút (đơn D4 `1789483309848K1UR` không vào ERP).
  - Gỡ `coordinator/`, `discord_utils`, `DeliveryView`, `_notify_coordinator`, webhook config; thêm `shared/erp_client.py`;
    NOTIFIED trước push ERP; alerts log-only; start/stop/watchdog/deploy_git/dashboard bỏ coordinator; `.env` .220 gỡ biến Discord.

## 2026-06-26

- `feat(status_sync): ERP-driven reconcile + eldo dispute parity` — **CODE XONG, test 3/3, chờ commit+deploy**
  - **ERP-driven reconcile** (đóng lỗ hổng g2g list-window, prod ~578 đơn kẹt): `erp_reconcile.py::reconcile_from_erp`
    gọi ERP `get_pending_marketplace_orders` → lookup `get_order_detail` per-order → push terminal. Throttle+batch+
    back-off (last_synced_at)+dừng-429. Cadence mỗi N cycle trong g2g_sync. `erp_client.get_pending_orders` +
    config `ERP_RECONCILE_*`. `reconcile_unpushed` cũ giữ nguyên (bot-driven, vẫn dùng).
  - **Eldo dispute parity:** EL-2 eldo_sync push `disputed` kèm `report_reason=latestDispute.reason` (Eldo gộp
    cancel+dispute vào 1 state Disputed; classifier trong latestDispute); EL-3 cờ `hasBeenRefundedPostCompletion`
    → push canceled (đảo ví, belt-and-suspenders, hiện=0).
  - Test `tests/test_erp_reconcile.py` 3/3 (basic/backoff/rate-limit), ruff clean. ERP side: gege_custom
    `feat/g2g-reconcile-eldo-dispute` (EL-1 clear-on-resolve + E-1 endpoint, test 10/10, app 122/122).
  - Deploy SAU ERP. `deploy_git.py status_sync`.

- `feat(status_sync): G2G cancel/resolution alert ON/OFF + report_case classify` — **ĐÃ DEPLOY prod `b16edfd`**
  - Cấu phần BOT của cụm "G2G cancel/resolution → ERP" (ERP đã commit `23a6ad4`, test 8/8 + 120/120).
  - `_sync_cases` rewrite: phân loại `report_case` (`cancel`→`cancel_requested`, còn lại→`disputed`);
    push **alert ON** khi case `open`/`escalate` + chưa alert, **OFF** khi `close` + đang alert;
    payload thêm `alert`/`report_case`/`report_reason`/`case_status`. Bỏ luồng cũ (map mọi case→`disputed`,
    chỉ push khi `open`). `notified_pushed_at` = cờ "ERP có alert active" → idempotent + retry push-fail +
    không spam 239 case đã close. Thêm `db.set_dispute_notified`; `_classify_case`+`_OPEN_CASE_STATES`.
  - Doc-sync: `docs/architecture.md` (state map + schema comment) + `.ai/decisions.md` (entry 2026-06-26 +
    supersede 2026-06-07 "mọi cancelled→Refunded").
  - **Phụ thuộc:** deploy `status_sync` SAU khi ERP lên prod. `deploy_git.py status_sync`. Plan: `.ai/current-plan.md`.

## 2026-06-13

- `feat(dashboard): SSE push realtime + Alpine.js reactive UI` — `7ed8c53`
  - Backend: expand SSE broadcaster to push 5 event types on sub-intervals
    (status 5s, auth 3s, orders 2s, log_update 1s). Incremental log reader
    via byte-offset tracking. Order change detection via updated_at + count.
    Initial data burst on SSE connect (after burst → add to broadcast list).
  - Frontend: Alpine.js reactive UI, zero setInterval polling. Polished dark
    theme with connection indicator, JWT progress bar, log level filters,
    scroll-based auto-scroll disable.
  - Review fixes: WARN→WARNING filter map, SSE orders pagination-safe,
    @scroll replaces x-intersect, SSE race condition, order deletion detection.

## 2026-06-10

- `feat(auth): two-tier G2G refresh — POST sls.g2g.com/user/refresh_access` — `452adf8`
  - Phase 5: rút JWT refresh từ ~30-60s Selenium → ~1s backend POST với
    `curl_cffi` impersonate chrome120. Selenium CDP fallback giữ nguyên.
    Discovery qua CDP sniff + JS bundle decompile.
- `feat(workers): retry indefinitely on transient failures (PR1)` — `(part of session)`
  - `RETRY_PENDING` state, max 100 attempts, exponential backoff
    60s×5→5m×5→30m×5→1h cap. `cleanup_old_orders` chỉ xóa COMPLETED.
- `feat(coordinator): queue and retry dispatch failures (PR2)` — `(part of session)`
  - Discord dispatch fail → `pending_dispatches` table, retry loop 30s.
- `feat(status_update): PROTECTED + BLOCK + whitelist + db.set_value` (gege_custom dev) — `c4afd80` (ref file only)
  - 4-layer safety hardening trong ERP webhook. KHÔNG dùng
    `set_user("Administrator")` (privilege escalation surface). Verdict
    types: updated/no_change/protected/manual_required/unsafe_transition/
    ignored/no_so + WS Activity Log audit.
- `docs(refs): annotate delivery_callback dead-code branch` — `c4afd80`
  - delivery_callback else branch hiện không reachable (workers POST coordinator
    `:8030`, không POST ERP). Tried fix Guest perm → exposed
    WorkflowPermissionError. Reverted + multi-paragraph warning comment.
- `feat(logging): GMT+7 timezone + date prefix in log format` — `(part of session)`
  - `[YYYY-MM-DD HH:MM:SS][name] LEVEL: msg`.
- `chore(scripts): cleanup 21 scratch scripts + organize 20 remaining` — `(part of session)`
  - 5 categories trong docs/operations.md "Scripts Catalog".
- `fix(start.sh/stop.sh): cleanup default, filter bash launcher, [N/9] numbering` — `(part of session)`
- `feat(ops): VNC re-login eldo main + bak2 for token stagger` — (no code commit)
- `fix(g2g_scanner): retry get_order_detail 3x on Step 3 failure` — **UNCOMMITTED**
  - Fix lost order `1781067547077COBM` (curl timeout 30s sau khi steps 1+2
    đã commit delivering trên G2G). 3 attempts + 2s/4s backoff.
- `docs: status_sync details + Phase 5 + ERP hardening + GMT+7` — **UNCOMMITTED**
  - architecture.md + operations.md + marketplace_auth.md updated.
- `docs(.ai): refresh handoff + decisions + project-context for 2026-06-10` — **UNCOMMITTED**

## 2026-06-08

- `feat(status_sync): poll marketplace state and push to ERP status_update` — `88e4c98`
  - Long-running process (30 min cycle), G2G `count-my-orders` + Eldo
    `statesCount` tripwires, first-run silent backfill, ERP `status_update`
    webhook push. Verified 11/11 mapping rules trên dev ERP `192.168.2.228`.
- `fix(database): restore mark_erp_synced, get_unsynced_orders, increment_erp_retry` — `e715b62`
  - 3 method đã có trên bot prod nhưng chưa được commit về repo, gây
    regression khi deploy local file đè lên.
- `feat(auth): two-tier Eldorado refresh — backend refresh + Camoufox fallback` — `452adf8`
  - Phase 4: replace Cognito direct refresh (fail SECRET_HASH) bằng
    `POST /api/authentication/refreshTokens` của Eldorado backend.
    Camoufox fallback navigate `/dashboard/orders/sold` + cookie-preservation
    guard.

## 2026-06-06

- `fix(auth): skip webdriver_manager to avoid filelock leak deadlock` — `a9f49ab`
  - `ChromeDriverManager().install()` leak FD vào auth process → self-deadlock
    sau N cycle. Fix bằng `_find_local_chromedriver()` glob `~/.wdm/...`.

## 2026-06-05 (pre-current-session)

- `feat(ops): retry_post_evidence.py for re-sending proof to marketplace` — `40da1d4`
- `docs(operations): add AI operator notes, scripts catalog, health schema` — `868a7fd`
- `feat(workers): download ERP evidence files and retry chat sends on auth refresh` — `6c1452c`
- `fix(auth): auto-cleanup browser locks and rotate Eldo profiles in isolated threads` — `4dd5bb1`
- `refactor: multi-process architecture with API scanners, workers, docs` — `7228585`
- (xa hơn xem `git log`)

## 2026-08-31

- Điều tra "đơn chưa trả bằng chứng" (SO-260831-J9JKWD4L / 1788181237562TQL6):
  ERP side đủ flow (Delivered/Completed + ALE + kho), worker .220 download file
  + submit qty OK, nhưng upload proof lên S3 G2G fail mạng tạm thời → bị gán
  nhãn "unsupported file type" → terminal, no retry. Quét log: 7 đơn 27/8–31/8,
  6 còn kẹt (4 trên ERP .100, 2 trên .102). Refresh `.ai/current-plan.md`:
  thêm Phase A re-push 6 đơn + bảng chứng cứ; Phase B giữ thiết kế fix 29/8.
  Chưa code — chờ duyệt.

- `fix(g2g): retry transient proof-upload failures instead of mislabeling them terminal` — `36dc4bb`
  - `_upload_proofs` tách bookkeeping `unsupported`/`failed`; 0 file upload được
    thì raise theo priority failed (retry, nhúng lỗi underneath) > unsupported
    (terminal, giữ nguyên văn). Verify classification bằng AST-isolated
    `_classify_error`: S3/connection → network, webp/heic → terminal.
  - `docs/proof_mechanism.md` sync phân loại lỗi mới (doc-sync trước commit).

## 2026-10-14

- `feat(eldorado): Seller API auth mode (Bearer token), cookie fallback` — `bf12ea7`
  - `ELDO_AUTH_MODE=seller_api`: auth đổi Client ID + secret (trong `.env`) lấy Bearer
    token 15 phút, `/auth/eldo` trả `bearer_token`; client gắn `Authorization: Bearer`.
    Không lấy được token thì alert `eldo-seller-api` và rơi xuống đường cookie cũ.
    Mặc định vẫn `cookie`.
  - `/health.eldo` thêm `auth_mode`, `bearer`, `bearer_expires_in`,
    `seller_credential_expires_in`. Không bỏ trường cũ.
  - Chưa đổi route đơn hàng sang `/api/v1` (chờ thử deliver bằng token trên đơn thật).
  - Verify: `tests/test_eldo_seller_auth.py` — mã thật chạy với Eldorado giả trên
    localhost, 44 phép kiểm. Chưa chạy với credential thật.
  - Doc-sync: `docs/operations.md` (mục "Eldorado Auth — Seller API mode" + schema
    `/health`), `docs/marketplace_auth.md`, `docs/architecture.md`,
    `docs/seller_api_migration_plan.md`.

- Deploy `efea447` 04:36 + bật `ELDO_AUTH_MODE=seller_api` 05:24 (chủ thêm credential vào `.env`).
  - Sự cố lúc deploy: restart auth sau uptime 18 ngày làm mất phiên G2G trên đĩa → G2G mù
    04:36→04:43, chủ login VNC. 3 đơn giao trễ ~5 phút (worker tự retry, đều giao đủ), không
    đơn nào mất. Cảnh báo + cách cứu đã ghi vào `docs/operations.md` → "Restart Auth Service".
  - Thử thật bằng token trên `.220`: mọi lệnh đọc của `EldoradoAPIClient` 200, kể cả
    `delivery-details/{detailId}` và TalkJS authorize. Còn chờ đơn thật: deliver + gửi TalkJS.
  - Xoá 5 bản sao `.env.bak*` cũ trên `.220` (secret, quyền 644); giá trị bộ lọc trước 27/06
    chép vào `.ai/decisions.md`.

- 2026-10-15 — Soát lại hai luồng Eldorado ở chế độ `seller_api` (`docs/seller_api_migration_plan.md` mục 2.3).
  - Kiểm 15/10 01:10: không có đơn Eldorado mới từ 14/10 03:40 (đơn mới nhất trên Eldorado trùng với DB bot).
  - Thử nửa TalkJS bằng token (không gửi gì): token TalkJS đúng user seller, WS `/session/renew` OK.
    Lệnh duy nhất còn phụ thuộc token mà chưa thử: `PUT orders/me/{id}/deliver`.
  - status_sync: 67/67 chu kỳ từ lúc `.220` khởi động lại 14/10 13:16 lấy auth ở `mode=seller_api`, đối soát Eldorado `fail=0`.
