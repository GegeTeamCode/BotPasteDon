# Task Log

Mỗi dòng: `YYYY-MM-DD — tóm tắt 1-line — commit (nếu có)`. Append-only,
mới nhất ở trên cùng.

---

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
