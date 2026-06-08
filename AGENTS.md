# Agent Rules — BotPasteDon

Production multi-process bot that automates order delivery on Eldorado.gg + G2G.com.
**Live orders represent real money.** Be cautious about anything that touches auth,
ERP webhook payloads, or marketplace state.

## Commands

This project has **no automated test suite** and **no CI**. Verification happens
through the live server and a few paramiko-based scripts.

```bash
# Syntax check before deploying any .py
python -c "import py_compile; py_compile.compile('<file>', doraise=True); print('OK')"

# Full process health check (paramikoes to 192.168.2.220)
python scripts/check_all_processes.py

# Auth /health (G2G JWT + Eldo cookies/logged_in)
ssh root@192.168.2.220 'curl -s http://localhost:8010/health | python -m json.tool'

# Recent scanner paste activity
ssh root@192.168.2.220 'tail -c 4000 /tmp/g2g_scanner.log /tmp/eldorado_scanner.log'

# Phase 4 Eldorado backend refresh cycles
ssh root@192.168.2.220 'grep -E "\[ELDO\] (Trying backend|backend refresh)" /tmp/auth*.log | tail -20'

# Order DB pending state
ssh root@192.168.2.220 'venv/bin/python -c "import sqlite3; c=sqlite3.connect(\"data/orders.db\"); print(\"DETECTED\", c.execute(\"SELECT count(*) FROM orders WHERE status=\\\"DETECTED\\\"\").fetchone()[0])"'
```

There is `tests/` but contents are ad-hoc paramiko scripts (`tests/test_g2g_api.py`,
`tests/check_pending.py`, ...). Treat them as **dev probes**, not a regression
suite. Don't pretend `pytest` is meaningful here.

## Rules

- **One scope per session.** When the work fans out, write a handoff to
  `.ai/handoff.md` and open a new session — don't let the same conversation
  chase three bugs.
- **Read the existing pattern before writing new code.** Look at
  `.ai/coding-standards.md` and the closest sibling module (e.g. study
  `shared/g2g_auth.py` before extending `shared/eldo_auth.py`).
- **Plan before code on anything non-trivial.** Write the plan into
  `.ai/current-plan.md` (Goal / Allowed files / Do-not-touch / Steps /
  Acceptance / Risks) and get user approval before editing source.
- **Deploy is paramiko, not ssh client.** Always use the python `paramiko`
  pattern documented in `docs/operations.md` → "AI Operator Notes".
  `nohup … &` over `exec_command` hangs the channel; use
  `setsid + </dev/null + & disown` via `Transport.open_session()`.
- **`pkill -f` self-match trap**: a bash session whose cmdline contains the
  target string matches itself. Use `pgrep -af <pat> | xargs -r kill -9` for
  each pattern in its own `exec_command`, never a chain.
- **Never amend or `git push --force` to `main` without explicit ask.**
- **No secrets in code.** `.env.example` lists the env vars; real secrets live
  in `.env` (not committed) on the bot server.
- **Commit messages**: conventional-commits style (`feat:`, `fix:`, `docs:`,
  `chore:`, `refactor:`). Subject ≤72 chars, body explains *why*, sign-off
  with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` when an
  agent did the bulk of the work.

## Out of scope (do not touch without explicit approval)

- `.env` files on any server — secrets, channel webhooks, ERP API keys
- `/opt/BotPasteDon/data/orders.db` rows — never `DELETE` outside of
  documented troubleshooting recipes in `docs/operations.md`
- ERP `Sell Order` workflow_state on prod ERP (`192.168.2.100`)
- Live Discord webhook URLs in `shared/config.py` SCANNER_CONFIG mappings
- `chrome_profile_eldo*` directories on the bot server — these hold the live
  Cognito session; the only sanctioned way to refresh them is the VNC
  re-login procedure in `docs/operations.md`
- `auth/main.py` Eldorado capture/refresh path without first reading the
  Phase 4 design in `docs/architecture.md` → Eldorado Auth section

## Sensitive areas — require Opus review before merge

- `auth/main.py` — Eldorado backend refresh, G2G JWT capture, profile rotation
- `shared/database.py` schema changes — production data lives here
- ERP webhook payload composition (`scanners/main.py`, `status_sync/*`) —
  affects accounting state
- `status_sync/*.py` push logic — can mass-mutate `workflow_state` across orders
- Anything in `coordinator/` that dispatches delivery tasks (touches money flow)
