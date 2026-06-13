# Agent Rules — BotPasteDon

Production multi-process bot that automates order delivery on Eldorado.gg + G2G.com.
**Live orders represent real money.** Be cautious about anything that touches auth,
ERP webhook payloads, or marketplace state.

## Commands

No automated test suite, no CI. Verification is via syntax check + the live server
(paramiko to `192.168.2.220`). Full command set: `.ai/test-commands.md` and
`docs/operations.md` → "AI Operator Notes".

```bash
# Syntax check before deploying any .py
python -c "import py_compile; py_compile.compile('<file>', doraise=True); print('OK')"

# Full process health check (paramiko to 192.168.2.220)
python scripts/check_all_processes.py

# Auth /health (G2G JWT + Eldo cookies/logged_in)
ssh root@192.168.2.220 'curl -s http://localhost:8010/health | python -m json.tool'
```

`tests/` holds ad-hoc paramiko probes, not a regression suite — don't pretend
`pytest` is meaningful here.

## Rules

- **One scope per session.** When work fans out, write `.ai/handoff.md` and open
  a new session (skill `session-handoff`).
- **Read the existing pattern first.** Check `.ai/coding-standards.md` and the
  closest sibling module (e.g. study `shared/g2g_auth.py` before extending
  `shared/eldo_auth.py`).
- **Plan before code on anything non-trivial.** Write the plan into
  `.ai/current-plan.md` (Goal / Allowed files / Do-not-touch / Steps /
  Acceptance / Risks) and get approval before editing source.
- **Deploy is git-based — NEVER SCP / rsync / edit files on the server.** The
  server `/opt/BotPasteDon` is a git checkout; hand-copying files creates drift
  and `deploy_git.py` aborts on drift. Flow: `git commit` → `git push origin
  main` → `python scripts/deploy_git.py <service>`. See skill `deploy`. SSH
  mechanics (paramiko, `pkill -f` self-match, watchdog respawn) are in skill
  `debug-protocol` and `docs/operations.md`.
- **Never amend or `git push --force` to `main` without explicit ask.**
- **No secrets in code.** `.env.example` lists vars; real secrets live in `.env`
  (not committed) on the bot server.
- **Default model glm-4.7.** Use glm-5.1 only for plan / hard / long tasks, and
  off-peak (outside 13:00–17:00 VN). When unsure, ask instead of guessing.
- **Commit messages**: conventional-commits (`feat:`/`fix:`/`docs:`/`chore:`/
  `refactor:`), subject ≤72 chars, body explains *why*. Sign-off
  `Co-Authored-By: <Model-Name> <email>` when an agent did the bulk (e.g.
  `Co-Authored-By: GLM-4.7 <noreply@zhipuai.cn>`).

## Workflow

Plan → Code → Debug → commit → log; review before merge. Full per-mode loop,
model map, and quota discipline live in `.ai/workflow-reference.md`. Protocols
auto-load as skills: `review-diff` (diff review + when to escalate to Opus),
`debug-protocol` (root-cause + gotchas), `session-handoff` (closing a session).

## Out of scope (do not touch without explicit approval)

- `.env` files on any server — secrets, channel webhooks, ERP API keys
- `data/orders.db` rows — never `DELETE` outside documented troubleshooting
  recipes in `docs/operations.md`
- ERP `Sell Order` workflow_state on prod ERP (`192.168.2.100`)
- Live Discord webhook URLs in `shared/config.py` SCANNER_CONFIG mappings
- `chrome_profile_eldo*` on the bot server — live Cognito session; refresh only
  via the VNC re-login procedure in `docs/operations.md`
- `auth/main.py` Eldorado capture/refresh path without first reading
  `docs/architecture.md` → Eldorado Auth section

## Sensitive areas — require Claude Opus review before merge

- `auth/main.py` — Eldo backend refresh, G2G JWT capture, profile rotation
- `shared/database.py` — schema changes (production data lives here)
- `scanners/main.py`, `status_sync/*` — ERP webhook payload (money fields
  `total_price`, `earning`, `channel_fee`); can mass-mutate `workflow_state`
- `coordinator/*` — delivery dispatch (money flow)
