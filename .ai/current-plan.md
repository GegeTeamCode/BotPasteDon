# Plan — Eldorado auth: thêm chế độ Seller API (Bearer token) cạnh chế độ cookie

> Lập 2026-10-14. Chủ duyệt làm 14/10 ("hãy tiến hành cập nhật"). Bối cảnh, số đo và
> lộ trình đầy đủ: `docs/seller_api_migration_plan.md` (plan này = Bước 1 + Bước 2 ở đó).
> Plan cũ ở file này (G2G proof upload, Phase B đã commit `36dc4bb`) nằm trong git history.

## Goal

Dịch vụ auth lấy được token Eldorado bằng Client ID + Client secret (Seller API, token
sống 900s, không cần trình duyệt) và phát cho scanner/worker/status_sync qua
`GET /auth/eldo`; client gắn `Authorization: Bearer`. Bật bằng `ELDO_AUTH_MODE=seller_api`
trong `.env`. Mặc định vẫn là `cookie` — deploy bản này mà chưa đổi `.env` thì hành vi
không đổi.

## Allowed files

- `shared/config.py` — 3 biến env mới
- `auth/main.py` — `EldoAuth` (đường lấy token, `is_fresh`), `handle_health`, log khởi động
- `shared/eldo_auth.py` — `EldoAuthData`, `build_headers`, `_fetch`
- `.env.example` — tên biến, giá trị rỗng
- `tests/test_eldo_seller_auth.py` — test cô lập với máy chủ Eldorado giả (mới)
- `docs/marketplace_auth.md`, `docs/operations.md`, `docs/architecture.md`,
  `docs/seller_api_migration_plan.md` — doc-sync
- `.ai/current-plan.md`, `.ai/task-log.md`

## Do not touch

- Đường cookie hiện có của Eldorado (`_try_backend_refresh`, `_backend_refresh_with`,
  capture Camoufox, guard chống mất cookie, xoay profile) — giữ nguyên từng dòng; nó là
  đường lùi.
- `shared/eldo_api.py` — KHÔNG đổi route trong plan này. Chuyển sang `/api/v1/orders/me/*`
  là Bước 3 của lộ trình, làm sau khi thử trên đơn thật.
- Mọi thứ của G2G; `scanners/`, `workers/`, `status_sync/`; payload ERP.
- `.env` trên server, `chrome_profile_eldo*`, `data/orders.db`.

## Steps

1. `shared/config.py`: `ELDO_AUTH_MODE` (`cookie` | `seller_api`), `ELDO_SELLER_CLIENT_ID`,
   `ELDO_SELLER_CLIENT_SECRET`.
2. `auth/main.py`:
   - `_eldo_seller_token()` — `POST /api/authentication/seller/token` body JSON
     `{clientId, clientSecret}`; `_eldo_seller_probe()` — `GET
     /api/v1/orders/me/seller-api-eligibility` (route có trong Swagger);
     `_eldo_seller_credential_expiry()` — đọc hạn credential (best-effort).
   - `EldoAuth._capture_seller_api()` trả bundle `{auth_mode, bearer_token, expires_at,
     user_agent, seller_id, logged_in, api_verified, cookies: {}, xsrf_token: ""}`.
   - `EldoAuth.capture()`: ở chế độ `seller_api` thử token trước; **không lấy được thì
     rơi xuống đường cookie cũ** và bắn ops alert (đơn là tiền thật — không để bot mù vì
     một lần cổng token lỗi).
   - `EldoAuth.is_fresh()`: bundle token còn mới khi token còn > 360s (client cache 300s
     + 60s đệm), để client không bao giờ cầm token đã hết hạn.
   - `handle_health`: thêm `auth_mode`, `bearer`, `bearer_expires_in`,
     `seller_credential_expires_in`. Không bỏ trường cũ nào (dashboard đang đọc).
3. `shared/eldo_auth.py`: `EldoAuthData.bearer_token/expires_at`; `build_headers` gắn
   Bearer và bỏ Cookie/XSRF/nsure/build-time khi có token; `_fetch` không cache quá hạn token.
4. `.env.example` + doc-sync.
5. `py_compile` 3 file `.py`; `python tests/test_eldo_seller_auth.py` (44 phép kiểm, cần venv có
   `requirements.txt`).

## Acceptance

- `ELDO_AUTH_MODE` vắng hoặc `cookie`: `/auth/eldo` và `/health` trả đúng như trước
  (chỉ thêm 4 trường mới trong `health.eldo`).
- `ELDO_AUTH_MODE=seller_api` + credential đúng: `/auth/eldo` trả `bearer_token`,
  `health.eldo.bearer=true`, không tiến trình Camoufox nào được mở, scanner đọc được đơn.
- `ELDO_AUTH_MODE=seller_api` + credential sai/hết hạn: log `OPS ALERT [eldo-seller-api]`,
  `/auth/eldo` vẫn trả bundle cookie (nếu còn phiên cookie).
- Secret không xuất hiện trong log, trong response `/health`, hay trong git.

## Risks

- **Chưa thử được bằng token** (phải thử trên một đơn Paid thật sau khi bật):
  `GET v1/orders/me/{id}/delivery-details/{detailId}`, `PUT orders/me/{id}/deliver`, gửi
  bằng chứng qua TalkJS. Nếu một trong ba bị từ chối → đặt lại `ELDO_AUTH_MODE=cookie`,
  restart auth.
- Rơi xuống cookie là tự động và có alert, nên trong giai đoạn chuyển phải giữ ≥1 profile
  cookie còn sống (bak1 hết 04/11/2026).
- Lộ `ELDO_SELLER_CLIENT_SECRET` = mất quyền điều khiển offer và đơn. Chỉ nằm trong
  `/opt/BotPasteDon/.env` trên .220, quyền 600.
- `auth/main.py` là vùng nhạy (AGENTS.md) — cần review trước khi merge.

## Deploy + verify

1. Chủ thêm 3 dòng vào `/opt/BotPasteDon/.env` trên .220 (xem `docs/operations.md`).
2. `git commit` → `git push origin main` → `python scripts/deploy_git.py auth` (dịch vụ
   auth), rồi scanner/worker/status_sync Eldorado để nạp `shared/eldo_auth.py` mới.
3. `curl -s localhost:8010/health` → `eldo.auth_mode == "seller_api"`, `eldo.bearer == true`.
4. Theo dõi một đơn Paid thật đi trọn vòng.
