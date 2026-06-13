---
name: deploy
description: >-
  Ship a code change to the bot server the git-based way (commit → push →
  deploy_git.py), never by SCP/rsync/editing on the server. Use when deploying,
  shipping, releasing, restarting a service on 192.168.2.220, or whenever a plan
  or summary proposes copying files to the server.
---

# Deploy to the bot server

The server `/opt/BotPasteDon` is a **git checkout of `origin/main`** — the single
source of truth. Deploy = make the server match `origin/main` and restart the
affected services. **Never** SCP/rsync files, and never edit code on the server:
both create drift, and `scripts/deploy_git.py` **aborts** when it sees
uncommitted tracked drift.

## The only correct flow

```bash
git add <files in scope>
git commit -m "<type>(<scope>): <subject>"
git push origin main
python scripts/deploy_git.py <service> [<service> ...]   # sync + restart
```

- No service arg → **sync code only, no restart** (`python scripts/deploy_git.py`).
- One/many services → sync + restart just those.
- `all` → sync + restart everything in dependency order
  (auth → workers → coordinator → scanners → dashboard).

Service names: **auth, scanner_g2g, scanner_eldo, worker_g2g, worker_eldo,
coordinator, dashboard**. Restart only what the diff actually affects (e.g. a
dashboard change → `deploy_git.py dashboard`, don't bounce auth/workers).

## What the script guarantees

- **Aborts on drift**: if the server has uncommitted tracked changes, it stops
  and prints them — commit that work back to the repo first, don't clobber it.
- **Watchdog-aware**: it handles the watchdog so respawns don't fight the
  restart. Don't manually `kill` services around a deploy — let the script do it.
- Runtime state (`.env`, `data/`, `chrome_profile_*`, `venv/`) is gitignored and
  never touched by the sync.

## If a deploy looks wrong

- "ABORT: server has uncommitted tracked drift" → someone edited on the server.
  `git -C /opt/BotPasteDon diff` to see it, commit it to the repo, then redeploy.
- Service didn't come back → check `python scripts/check_all_processes.py` and
  the per-service `/tmp/*.log`; see skill `debug-protocol` for SSH/process traps.
