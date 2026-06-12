# Handoff — 2026-06-10 (cuối session Phase 5 + ERP hardening + ops cleanup)

> Handoff cũ 2026-06-08 (Phase 4 + status_sync feature) đã merged xong, di
> chuyển vào [decisions.md](decisions.md) + [task-log.md](task-log.md). File
> này phản ánh state hiện tại cho AI tiếp theo.

## Đã làm xong trong session

- **Phase 5 G2G backend refresh** — `POST sls.g2g.com/user/refresh_access`
  (`curl_cffi` impersonate `chrome120`), body 4-field từ JWT.sub + 3 cookies.
  Discovered qua CDP sniff + JS bundle decompile. Deployed lên prod `.220`,
  verified. Selenium CDP fallback giữ nguyên. Commit `452adf8`,
  documented trong [docs/marketplace_auth.md](../docs/marketplace_auth.md).
- **Worker retry loop (PR1)** — orders fail evidence sẽ retry indefinitely
  (`RETRY_PENDING` state, max 100 attempts, exponential backoff
  60s×5 → 5m×5 → 30m×5 → 1h cap). `cleanup_old_orders` chỉ xóa COMPLETED.
- **Coordinator dispatch retry (PR2)** — Discord/worker dispatch fail giờ
  queue vào `pending_dispatches`, retry loop 30s.
- **ERP `status_update` hardening trên dev `.228`** — 4 layers:
  PROTECTED check → BLOCK current=`In Delivery` (manual_required) →
  `_SAFE_TRANSITIONS` whitelist → `frappe.db.set_value` apply (bypass
  `save()` + workflow validation, raw SQL nên không cần `set_user`
  escalation). Verdict types: `updated/no_change/protected/manual_required/
  unsafe_transition/ignored/no_so`. WS Activity Log audits non-noisy
  outcomes. Tested 7/7 verdicts pass trên dev.
- **`delivery_callback` dead-code branch** — tried fix nhưng exposed
  WorkflowPermissionError ("In Delivery → Delivered" không có direct
  transition). Reverted + multi-paragraph DEAD CODE warning comment
  ghi rõ 2 defect layers (Guest perm + missing transition). Commit
  `c4afd80` (ref file only).
- **GMT+7 logging** — `shared/logging_config.py` thêm `LOG_TZ =
  timezone(+7)` + `datefmt="%Y-%m-%d %H:%M:%S"`. Restart all services.
- **Scripts cleanup** — 21 scratch scripts xóa (3 `git rm` + 18 untracked
  `rm`), 7 ref_223_*.py + reference_scraper.py xóa. 20 scripts còn lại
  organized 5 categories trong [docs/operations.md](../docs/operations.md)
  "Scripts Catalog".
- **start.sh / stop.sh rewrite** — cleanup() chạy mặc định (--no-clean
  để skip), filter bash launcher khỏi self-match, [N/9] numbering,
  status_sync + dashboard.server bao gồm.
- **Eldo profile token stagger qua VNC re-login** — main +30d, bak2 +28.2d.
  bak1 pending re-login.
- **status_sync run trial** — start trên 220 PID 1473056, 17 cycles ~8h.
  Stop lại vì prod ERP chưa có hardened code → 4xx errors spam.
- **g2g_scanner_api `_do_extract` Step 3 retry fix** — `get_order_detail`
  giờ retry 3 attempts (2s/4s backoff). Fix bug "order 1781067547077COBM
  mất do curl timeout 30s sau khi Steps 1+2 đã commit delivering trên G2G".
  **CHƯA commit, CHƯA deploy** — chờ user duyệt.
- **Docs updates** — `docs/architecture.md` + `docs/operations.md` +
  `docs/marketplace_auth.md` reflect current code (Phase 5, ERP hardening,
  status_sync details, config knobs, CLI flags, GMT+7).

## Đang dở / chưa làm

- **`scanners/g2g_scanner_api.py` Step 3 retry fix** — code đã edit local,
  chưa SCP lên `.220`, chưa commit. Cần manual SCP + restart g2g_scanner.
- **`gege_custom` repo push từ dev `.228` → CI → prod `.100`** — bot prod
  status_update vẫn run code cũ → real SO lookups fail HTTP 403 Guest +
  HTTP 417 MandatoryError. User cần `git diff` + commit + push trong dev
  ERP để CI deploy hardened version lên prod. Khi xong → restart status_sync
  trên `.220`.
- **bak1 Eldo profile re-login qua VNC** — main+bak2 đã re-login để stagger
  token expiry. bak1 còn lại (user nói "sẽ login sau").
- **`scripts/check_all_processes.py` "8 services" string** — vẫn outdated
  từ 2026-06-08, nên update thành "9 services" (đếm cả status_sync).

## Quyết định quan trọng đã ghi vào `.ai/decisions.md`

Hôm nay (xem [decisions.md](decisions.md) section 2026-06-10):

- **Phase 5 G2G backend refresh** — endpoint `/user/refresh_access`,
  `curl_cffi` impersonate cho TLS fingerprint.
- **ERP `status_update`: dùng `frappe.db.set_value`, KHÔNG
  `frappe.set_user("Administrator")`** — raw SQL bypass permission lẫn
  workflow validation; Admin escalation tạo privilege surface + audit
  dilution.
- **BLOCK current=`In Delivery`** — trader đang giao + giữ inventory locks,
  bot tuyệt đối không override.
- **`_SAFE_TRANSITIONS` whitelist** — chỉ 4 source state nhận push:
  Delivered, Outstanding, Completed, Disputed. Anything else (Queued/
  Claimed/Evidence Uploaded/In Delivery) → unsafe_transition.
- **Worker retry: max 100 attempts, exponential backoff** — không drop
  orders khi auth fail / network blip.
- **Logging GMT+7 + date prefix** — log thread của bot bám timezone VN.
- **g2g scanner Step 3 retry**: 3 attempts là enough; KHÔNG thêm
  `delivering` state scanner (over-engineering).

## Việc tiếp theo (đề xuất thứ tự)

1. **User push gege_custom dev → CI → prod** để prod ERP có hardened
   `status_update` code. Test smoke với 1-2 đơn thật, expect verdict
   `no_change` / `protected` / `manual_required` thay vì 403/417.
2. **Restart status_sync trên `.220`** sau khi prod hardened. Verify cycle
   1-2 đi qua sạch (chỉ có verdicts hợp lệ + WS Activity Log).
3. **Deploy `g2g_scanner_api.py` Step 3 retry fix** lên `.220`. SCP +
   pkill g2g_scanner + restart. Verify 1-2 đơn G2G paste OK.
4. **bak1 Eldo VNC re-login** để hoàn tất stagger (mục tiêu: 3 profile
   exp lệch nhau >7 ngày, không chết đồng loạt).
5. **`check_all_processes.py` update** — đổi "8 services" → "9 services",
   thêm status_sync vào dict.

## Lưu ý / cạm bẫy

- **`set_user("Administrator")` là cám dỗ giả** — khi gặp Guest
  PermissionError trên ERP webhook, KHÔNG add `set_user("Administrator")`.
  Đúng pattern: dùng `frappe.db.set_value` (raw SQL bypass permission +
  workflow). Lý do refuse Admin: privilege escalation surface + audit
  dilution. Xem decision 2026-06-10 trong decisions.md.
- **status_sync first-run silent backfill** vẫn pattern đúng — KHÔNG bao
  giờ disable. Mass-push ~10k transitions giả sẽ spam ERP + workflow audit
  log. Chỉ enable push từ cycle 2 trở đi.
- **Discovery hành vi của Frappe**: `frappe.flags.ignore_permissions` KHÔNG
  bypass `validate_workflow → get_transitions → check_permission` trong
  `save()` pipeline. Đó là lý do dùng `db.set_value` (skip save()) cho
  whitelisted transitions.
- **Eldorado cookies bị strip khi session chết** (vẫn pattern cũ): cookie-
  preservation guard chỉ giữ được khi bundle CŨ có IdToken+Refresh. Nếu
  RefreshToken expire >30 ngày, cả 3 profile có thể chết đồng loạt
  → cần stagger expiry qua VNC re-login lệch ngày.
- **G2G `list_my_order` chỉ ~20 newest** mỗi state — status_sync không
  thể full-backfill G2G qua list endpoint. Eldo có pagination chuẩn nên
  backfill full được (1500-page cap on first-run).
- **scripts/_*.py** (underscore prefix) đã xóa hết trong session này
  ngoại trừ smoke tests (`_smoke_*.py`). Đừng tạo mới trừ khi cần probe
  / discovery cụ thể; cleanup ngay sau khi xong.

## Trạng thái runtime hiện tại (snapshot 2026-06-10 17:00 +07)

- Auth uptime ~22h, G2G fresh + has_jwt, Eldo logged_in=true, cookies=124
- Phase 5 G2G refresh: ~100% (chạy mỗi ~13min từ lúc deploy)
- 8 services up (g2g_worker, eldo_worker, coordinator, g2g_scanner,
  eldo_scanner, dashboard, auth, watchdog) + status_sync **đã stop**
- check_all_processes.py báo "ALL OK — 8 services" (đúng — vì
  status_sync đang stop có chủ ý)
- Logs ở `/tmp/{auth,g2g_worker,eldo_worker,coordinator,g2g_scanner,
  eldo_scanner,status_sync,watchdog,dashboard}.log` (GMT+7)
