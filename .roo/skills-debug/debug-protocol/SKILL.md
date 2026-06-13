---
name: debug-protocol
description: >-
  Diagnose root cause first, then apply the smallest in-scope fix and re-verify.
  Carries BotPasteDon's accumulated gotchas (paramiko/SSH, pkill self-match,
  watchdog respawn, Camoufox capture, Eldo cold-start refresh, dashboard
  SSE/Alpine). Use when a command/test fails, debugging a bug, a process won't
  start/stop on the bot server, or the dashboard/SSE stream misbehaves.
---

# Debug protocol

1. **Root cause in 1–2 sentences before touching code.** State the actual
   mechanism, not the symptom.
2. **Fix ONLY the failing issue, within current scope.** No drive-by refactors.
3. **Re-run** the failing command to confirm green. Paste the result.
4. If it's a NEW class of bug, **append one line to "Known gotchas" below** —
   cheapest institutional memory we have.

Verify a `.py` before deploying:
`python -c "import py_compile; py_compile.compile('<file>', doraise=True); print('OK')"`

## Known gotchas (BotPasteDon)

- **SSH from Windows host**: use `paramiko`, never `subprocess.run(["ssh",...])`
  or `ssh <<<password`. Standard pattern in `docs/operations.md` → AI Operator
  Notes (`AutoAddPolicy`, `192.168.2.220`, user `root`).
- **Background launch hangs the channel**: `nohup … &` over `exec_command()`
  can hang paramiko. Use `setsid + </dev/null + & disown` via
  `Transport.open_session()`, then poll `exit_status_ready`, don't `stdout.read()`.
- **`pkill -f` self-match trap**: a bash SSH session whose cmdline contains the
  target string kills itself first. Use one `exec_command` per pattern, or
  `pgrep -f <pat> | xargs -r kill -9` (`-r` = skip on empty input).
- **bash launcher vs python service**: `pgrep -af 'auth.main'` returns 2 rows
  (the `bash -c …` launcher + the real `python -u -m auth.main`). Filter
  `^bash\s+-c` before counting instances. See `scripts/check_all_processes.py`.
- **Watchdog respawn**: to fully stop a service, kill `watchdog.py` FIRST, then
  the service — otherwise it restarts the moment you kill it. Restart watchdog
  last.
- **Camoufox capture close()-spin**: browser capture runs in an isolated
  subprocess worker so a `close()` hang can't spin the auth process. Don't move
  capture back inline. (Fixed — see auth capture worker module.)
- **Eldo cold-start refresh**: on cold start, refresh from the on-disk
  `RefreshToken` rather than stripping it. Don't reintroduce the strip.
- **LXC has no `sqlite3` CLI**: query `data/orders.db` via `venv/bin/python` +
  `sqlite3` module, not the shell `sqlite3` binary.
- **DB access**: go through the `threading.Lock` in `shared/database.py`; never
  open a raw connection that bypasses it.

### Dashboard / SSE / Alpine
- **SSE push overwrites user-controlled state**: a live `orders`/list event will
  clobber the page the user paginated to. Guard the assignment (e.g. only set
  `orders` when `orderPage === 0`); always update derived totals.
- **Register SSE client AFTER the initial burst**: append `resp` to the
  broadcast list only once the burst finishes, else the 1s broadcaster
  interleaves events mid-burst and the stream comes out of order.
- **Change-detection on `MAX(updated_at)` misses deletions**: a pruned row
  doesn't bump the max. Compare `count(*)` too before deciding "nothing changed".
- **Log-level filters must match the real logger string**: logs emit `WARNING`,
  not `WARN` — filter on the exact token. Alpine plugin directives (e.g.
  `x-intersect`) silently no-op unless that plugin script is loaded; prefer
  plain `@scroll`/`@click` when only core Alpine is on the page.
