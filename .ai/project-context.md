# Project Context — BotPasteDon

- **Tên dự án:** BotPasteDon
- **Mục tiêu (1–2 câu):** Tự động hóa toàn bộ vòng đời đơn hàng trên hai
  marketplace **Eldorado.gg** và **G2G.com** — từ phát hiện đơn, thông báo
  trader trên Discord, giao hàng tự động (qua API hoặc Selenium fallback),
  cho tới đồng bộ trạng thái sang ERP nội bộ (Frappe/ERPNext).
- **Người dùng/khách hàng:** Đội trader của shop game (GegeTeam). Trader làm
  việc qua các thread Discord do bot tạo; bot tự xử lý phần còn lại.
- **Stack:**
  - Python 3.10+
  - Process model: 9 service độc lập, giao tiếp HTTP + shared SQLite
  - Async HTTP: `aiohttp` (auth/workers/coordinator/dashboard)
  - Marketplace API: `curl_cffi` với browser impersonation (chrome120/136)
  - Browser automation: `selenium` (Chrome CDP cho G2G JWT capture) +
    `camoufox` (anti-detect Firefox cho Eldorado capture)
  - Discord: `discord.py`
  - SSH/deploy: `paramiko` (KHÔNG dùng `sshpass` hay `ssh ... <<<password`)
  - Storage: SQLite WAL ở `data/orders.db`, thread-safe wrapper trong
    `shared/database.py`
  - Hạ tầng: LXC container trên Proxmox ở `192.168.2.220`, Xvfb `:99` cho
    Camoufox visible khi VNC vào re-login

- **Trạng thái hiện tại (2026-06-10):**
  - Đang chạy ổn định trên bot prod (192.168.2.220), 9 services up
  - **Phase 4 Eldo** + **Phase 5 G2G** backend refresh đều work — auth
    cycle ~13 min, JWT/cookies fresh
  - ERP prod `192.168.2.100` đã có endpoint `status_update` base + verdict
    format hardened (`no_so`, `ignored`) NHƯNG **chưa có** `db.set_value`
    bypass + `set_user`-less safety pattern → real SO lookup vẫn fail HTTP
    403 Guest perm + HTTP 417 MandatoryError. Chờ gege_custom CI push từ
    dev `.228` lên prod
  - **status_sync ĐÃ start hôm nay** (PID 1473056, 17 cycles, ~8h) rồi
    **stop lại** chờ prod ERP hardened. Code patched `botpastedon.py` đã
    live trên dev `.228` (commit `c4afd80` ref-only); chờ user push
    gege_custom repo
  - ERP dev `192.168.2.228` có hardened `status_update` đầy đủ (PROTECTED
    + BLOCK current=`In Delivery` + `_SAFE_TRANSITIONS` whitelist +
    `db.set_value` bypass + WS Activity Log audit)
  - **Logging**: timestamp GMT+7 (Asia/Ho_Chi_Minh) + date trong format
    `[YYYY-MM-DD HH:MM:SS][name] LEVEL: msg` (shared/logging_config.py)
  - **Eldorado profile token expiry stagger** đã hoàn tất qua VNC: main
    +30d, bak2 +28.2d; bak1 pending re-login
  - **Scripts cleanup**: 21 scratch scripts đã xóa, 20 còn lại organized
    trong [docs/operations.md](../docs/operations.md) "Scripts Catalog"

- **Ưu tiên:** *cheap + reliable*. Đơn hàng = tiền thật → false positives
  rẻ hơn false negatives, nhưng vẫn phải sửa nhanh. Tránh logic phức tạp
  khi có thể.

- **Ràng buộc:**
  - **Live production system.** Mọi thay đổi cần test trên dev ERP
    (192.168.2.228) hoặc bằng `--once` cycle với DB tạm trước khi đẩy lên
    bot prod.
  - **Cognito client của Eldorado có client secret** → các call AWS Cognito
    trực tiếp KHÔNG work, phải đi qua endpoint nội bộ
    `/api/authentication/refreshTokens` của Eldorado.
  - **G2G `list_my_order` chỉ trả ~20 đơn newest** mỗi state → status_sync
    không thể full-backfill G2G qua list endpoint; phải dựa vào counts
    tripwire + scan top-of-list.
  - **Không có CI/CD.** Deploy = paramiko script + restart service. Mọi
    "test" là dev probe + verify trên live.
  - **PROTECTED workflow states ERP**: `Refunded`, `Partially Refunded`,
    `Cancellation Requested`, `Outstanding`, `Payment Pending` — bot
    KHÔNG được override (do nhân viên set).

## Topology

```
┌─ Bot LXC (192.168.2.220)
│  └─ /opt/BotPasteDon → 9 services (auth/scanner×2/worker×2/coordinator/
│                                     status_sync/dashboard/watchdog)
│  └─ chrome_profile_eldo{,_bak1,_bak2} — Cognito session, RefreshToken ~30d
│  └─ chrome_profile_g2g                — Selenium login, JWT refresh 13min
│
├─ ERP prod (192.168.2.100, Frappe) → workflow_state truth source cho Sell Order
├─ ERP dev (192.168.2.228, Frappe)  → staging webhook test
└─ Reference servers (read-only learning, not deployed to):
    192.168.2.229 — Controller code mẫu (eldorado-test/)
    192.168.2.223 — Worker pattern (G2G-AutomationBot-v4/control/auth/manager.py)
                    → nguồn của Phase 4 backend refresh design
    ```

## Vibe Coding Methodology

Dự án vận hành theo workflow "Vibe Coding" (GLM Pro + Claude Pro):

- **Quy trình chuẩn:** Plan (glm-5.1, off-peak) → Code (glm-4.7) → Debug →
 Commit → Log. Chi tiết trong `.ai/workflow-reference.md`.
- **Review:** GLM Reviewer first-pass mọi diff; Claude Opus chỉ cho diff lớn
 hoặc chạm vùng nhạy cảm (auth/payment/DB/webhook).
- **Thông tin chảy qua `.ai/` files** — không qua môi riêng giữa các mode.
 Mỗi session đọc handoff.md trước khi bắt.
- **Nguồn methodology đầy đủ:**
 `D:\AI vibe coding\{DAILY-WORKFLOW,WORKED-EXAMPLES,ke-hoach-vibe-coding-2026}.md`
