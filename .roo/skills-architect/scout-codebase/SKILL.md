---
name: scout-codebase
description: >-
  Read-only map of a subsystem before planning — infer structure, data flow,
  conventions and sensitive areas, write findings to .ai/ markdown. Use when
  onboarding to an unfamiliar module, before planning a change in code you
  haven't touched, or refreshing .ai/architecture.md after drift.
---

# Scout a subsystem (read-only)

Do NOT modify code. Produce/refresh markdown under `.ai/` only, then summarize.

BotPasteDon is a mature repo — `.ai/architecture.md`, `.ai/coding-standards.md`
and `docs/` already exist. Scouting here usually means **mapping one subsystem
deeply before planning**, not regenerating everything. Read the existing docs
first and only extend them.

## Procedure

1. Read `docs/architecture.md`, `docs/operations.md`, and the existing
   `.ai/architecture.md` so you don't re-derive known facts.
2. Trace the target subsystem end to end. The 9 services and their entry points
   are in `docs/operations.md` → Service Ports (auth 8010, workers 8001/8002,
   coordinator 8030, scanners, status_sync, dashboard 8766, watchdog).
3. Follow the data path: scanner → ERP webhook → DB (`shared/database.py`) →
   coordinator dispatch → worker delivery → status_sync push back to ERP.
4. Capture only what's NEW or changed into `.ai/architecture.md` (append a
   dated subsystem note rather than overwriting curated content).

## Always flag sensitive areas

`auth/main.py`, `shared/database.py`, `scanners/main.py`, `status_sync/*`,
`coordinator/*` — anything touching auth, the orders DB, money fields, or
dispatch. List them explicitly so the follow-up plan can gate Opus review.

## Output

End with a short summary: what the subsystem does, its entry point, the files a
change would touch, and the risks. Hand off to `plan-task` for the actual plan.
