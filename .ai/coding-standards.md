# Coding Standards — BotPasteDon

These are conventions *as observed in the current codebase* (mid-2026). The
style is pragmatic: shipped over pretty. Don't try to impose stricter rules
than what the rest of the repo already practices.

## Language & runtime

- **Python 3.10+** (uses `X | Y` union, structural pattern in places).
- No `mypy`/`pyright` in CI. Type hints are encouraged for new public
  functions but legacy code is partially-typed and that's OK.
- No formatter enforced. Match what you see in the file you're editing:
  4-space indent, snake_case, ~100-char soft wrap.

## Module layout

```
auth/         — singleton service per process (HTTP server on :8010)
scanners/     — long-running poll loops; one per --platform
workers/      — HTTP servers receiving Coordinator dispatch
coordinator/  — Discord bot + HTTP server (no Discord state in workers)
status_sync/  — long-running async cycle, async runner in main.py
shared/       — imported by everyone; no app-specific config here
scripts/      — operational scripts (start.sh, watchdog, check_*)
deploy/       — systemd units
docs/         — architecture.md + operations.md are the source of truth
```

## Naming

- Functions / variables: `snake_case`
- Classes: `PascalCase` (e.g. `G2GSync`, `EldoAuth`, `Database`)
- Module-level constants: `UPPER_SNAKE` (e.g. `ELDO_REFRESH_URL`, `JWT_TTL`)
- Private helpers (module-level): leading underscore (`_eldo_api_probe`,
  `_cleanup_profile_locks`).
- Private async helpers (often used by `run_in_executor`): leading
  underscore too.

## Logging

```python
from shared.logging_config import setup_logger
logger = setup_logger("status_sync.eldo")   # dotted, hierarchical

logger.info("Cycle done. sleep %ds", interval)
logger.warning("[ELDO] backend refresh HTTP %d | body=%s", code, body[:300])
```

- Format applied by `setup_logger`: `[HH:MM:SS][name] LEVEL: message`,
  flushed after every emit. **Don't add your own basicConfig.**
- Prefix with the platform tag (`[G2G]`, `[ELDO]`) inside the message when
  the logger name alone isn't enough to grep.
- Truncate long blobs (`body[:300]`, `jwt[:20] + "..."`); production logs
  must stay grep-friendly.

## Database access

Always go through `Database` (`shared/database.py`). Two non-negotiable
patterns:

```python
def method(self, …):
    with self._lock:
        conn = self._get_conn()
        try:
            conn.execute("…", (…,))
            conn.commit()
        finally:
            conn.close()
```

```python
def reading_method(self, …) -> List[Dict]:
    with self._lock:
        conn = self._get_conn()
        try:
            rows = conn.execute("…", (…,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
```

- `_get_conn` sets `journal_mode=WAL` + `busy_timeout=5000` + `row_factory=Row`.
- `self._lock` (threading.Lock) is required because `aiohttp` handlers run
  in the default thread but `run_in_executor` ops touch the same DB from
  worker threads.
- Migrations: add new table via `CREATE TABLE IF NOT EXISTS` in
  `_init_db()`. Adding columns to existing tables uses a `try/except`
  `ALTER TABLE` block (see the `retry_data` migration as an example).

## HTTP clients

- Marketplace APIs (G2G, Eldorado): **`curl_cffi`** with browser
  impersonation. Pattern in `shared/g2g_api.py` / `shared/eldo_api.py`:

  ```python
  from curl_cffi import requests as cffi
  r = cffi.get(url, headers=auth.build_headers(), timeout=30,
               impersonate="chrome120")
  ```

- Internal calls (`/auth/g2g`, ERP webhook, worker dispatch): regular
  `aiohttp.ClientSession` is fine.
- Always pass `timeout=` explicitly. Never block on a default timeout.

## Auth integration

- Consumers DO NOT call Eldo/G2G login directly. They go through
  `shared/g2g_auth.py` / `shared/eldo_auth.py` which talks to the auth
  service on port 8010 (5-min cache, `invalidate()` on 401).
- New headers needed for the marketplace must flow through
  `EldoAuthData.build_headers()` / `G2GAuthData.build_headers()` so every
  consumer gets them consistently.

## Async / sync boundary

- aiohttp handlers run in the main event loop.
- Anything that blocks (sqlite, curl_cffi sync call, Playwright sync API,
  Selenium) must go through `loop.run_in_executor(None, fn, …)`.
- Playwright sync API specifically: wrap the call in a
  `ThreadPoolExecutor(max_workers=1)` per attempt and run
  `asyncio.set_event_loop(asyncio.new_event_loop())` first thing in the
  worker. See `EldoAuth.capture()` for the canonical pattern.

## Error handling

- Auth probes / external API calls: catch broad `Exception`, log a
  WARNING with status + truncated body, return `None`/`False`. **Don't**
  raise out of refresh helpers — the caller decides what to fall back to.
- Scanner main loop never crashes on a single bad order — log + continue.
- DB methods can let `sqlite3.OperationalError` propagate (caller chose to
  hold `self._lock` knowingly).

## Model usage guidelines

- **glm-4.7 là mặc định** cho mọi việc code, debug, refactor. Mức 1x quota.
- **glm-5.1 chỉ dùng cho**: plan, task dài, code khó. Chạy off-peak (ngoài
  13:00–17:00 giờ VN). Mức 3x giờ cao điểm / 1x off-peak (đến hết 6/2026).
- **glm-5-turbo**: nâng từ glm-4.7 khi bug phức tạp hoặc code khó. Mức 3x/2x.
- **glm-4.5-air**: docs, commit messages, task nhỏ, scout. Rẻ nhất.
- **Claude Opus**: review only, KHÔNG code. Dùng `/ultrareview` trên Claude
  Code Pro. Chỉ khi diff lớn / nhạy cảm (xem escalation rules dưới).

## Review escalation rules

1. **Mọi commit** → GLM Reviewer first-pass (mode Reviewer, glm-5.1).
   Reviewer read-only, chỉ chạy `git diff --staged`.
2. **Nếu diff chạm "Sensitive areas"** trong `AGENTS.md` → **BẮT BUỘC** Claude
   Opus review trước khi merge. Các file nhạy cảm:
   - `auth/main.py` — auth capture/refresh
   - `shared/database.py` — schema changes
   - `scanners/main.py`, `status_sync/*` — webhook payload composition
   - `coordinator/*` — dispatch logic (money flow)
3. **Nếu diff > 200 dòng** đổi behavior → khuyến nghị Opus review.
4. **Refactor / docs / test chỉ** → GLM Reviewer, bỏ qua Opus.
5. **Tỷ lệ mục tiêu**: ~95% review bằng GLM / ~5% cần Opus.

## Anti-patterns to avoid

- `subprocess.run(["ssh", "..."])` or `pexpect` for remote calls — use
  paramiko.
- `pkill -f <pattern>` inside a multi-command bash session — matches the
  bash session itself. Use `pgrep -af <pat> | xargs -r kill -9` per
  pattern, each in its own `exec_command`.
- `git push --force` to `main`, `git commit --amend` after push,
  `git reset --hard` on a dirty working tree — confirm with user first.
- Hardcoding secrets — they belong in `.env` (not committed).
- Importing from a sibling app module (e.g. `from workers.x import y`
  inside `scanners/`); cross-app communication is HTTP only.
- Spinning up a Camoufox/Chrome on the same profile twice — use the
  `_LOCK_FILES` cleanup pattern from `auth/main.py`.
