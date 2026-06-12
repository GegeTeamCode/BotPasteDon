---
name: review-diff
description: >-
  Review a staged git diff and emit an APPROVE/REJECT verdict with blocking
  issues as file:line, plus decide whether the diff must be escalated to Claude
  Opus. Use when reviewing changes before commit, doing a first-pass code
  review, running the Reviewer mode, or deciding if a diff needs Opus review.
---

# Review a diff (GLM first-pass)

Read-only. Run `git diff --staged`. Optionally read `.ai/coding-standards.md`
and `.ai/current-plan.md` for context. Never edit files — describe fixes in
words; Code mode implements them.

## Output format

```
Verdict: APPROVE or REJECT

Blocking issues (must fix before commit):
- file:line — what's wrong and why

Non-blocking suggestions:
- file:line — optional improvement

If REJECT: list specific reasons. Do NOT write code.
```

## What to check — BotPasteDon specific

- **Logic / edge cases / error handling**; no hardcoded secrets or credentials.
- **SSH safety**: paramiko (never `subprocess.run(["ssh",...])`); no `pkill -f`
  in a multi-command SSH session (self-match trap); background launch via
  `Transport.open_session()` + `setsid`, not `nohup … &` over `exec_command`.
- **DB**: every access in `shared/database.py` wrapped by its `threading.Lock`.
- **HTTP**: `curl_cffi` with `impersonate` for marketplace APIs; `aiohttp` for
  internal calls. Logging via `setup_logger()` (no `basicConfig`).
- **Conventions**: snake_case functions, PascalCase classes, `[G2G]`/`[ELDO]`
  platform tags in logs. Conventional-commit subject ≤72 chars.

## Escalate to Claude Opus when the diff touches ANY of

- `auth/main.py` — auth capture/refresh, profile rotation
- `shared/database.py` — schema changes / migration
- `scanners/main.py`, `status_sync/*` — webhook payload (money fields
  `total_price`, `earning`, `channel_fee`)
- `coordinator/*` — dispatch logic (money flow)
- diff is **large** (many files / hundreds of lines) or **pre-merge/deploy**

If so, end with: `⚠️ Touches sensitive areas — get Claude Opus /ultrareview
before merge.` **Skip Opus** for: small fixes, docs-only, test-only, refactors
with no behavior change. Target ratio ~95% GLM / 5% Opus.

## Running the Opus pass

1. In Roo: `git add` the change. 2. Open clean Claude Code (Pro), run
`/ultrareview`. 3. Opus returns APPROVE/REJECT + reasons — it does NOT edit.
4. On REJECT → back to Code mode, paste reasons, GLM fixes in scope. 5. Re-review
until APPROVE → merge/deploy.
