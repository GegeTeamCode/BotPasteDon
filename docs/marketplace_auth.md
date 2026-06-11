# Marketplace Auth Token Reference

Tài liệu này document co che authentication cua hai marketplace **Eldorado.gg** va **G2G.com** o muc đầy đủ de tuong lai khong phai reverse-engineer lai. Doi tuong su dung: AI agent hoac engineer refactor cac bot 220/223/229 hoac viet client moi.

Hai marketplace dung **kien truc khac nhau** (Cognito broker vs self-issued JWT) nhung cung pattern: short-lived access token + long-lived refresh token. Bot dung cung **two-tier strategy** — backend refresh nhanh, browser fallback cham.

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
| [auth/main.py](../auth/main.py) | `EldoAuth`, `G2GAuth`, `_eldo_backend_refresh`, `_g2g_backend_refresh` |
| [shared/eldo_auth.py](../shared/eldo_auth.py) | Eldorado auth client (cache JWT + cookies tu auth service) |
| [shared/g2g_auth.py](../shared/g2g_auth.py) | G2G auth client (cache JWT tu auth service) |
| [scripts/_sniff_g2g_refresh.py](../scripts/_sniff_g2g_refresh.py) | CDP sniff template |
| [scripts/_g2g_js_grep.py](../scripts/_g2g_js_grep.py) | JS bundle decompile template |
| [scripts/_probe_g2g_refresh.py](../scripts/_probe_g2g_refresh.py) | Blind probe template |
| [scripts/_probe_refresh_access_final.py](../scripts/_probe_refresh_access_final.py) | Confirm-endpoint template |
| [scripts/_smoke_g2g_refresh.py](../scripts/_smoke_g2g_refresh.py) | Smoke test pattern (offline + decode JWT + guard rails) |
