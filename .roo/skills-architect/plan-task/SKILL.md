---
name: plan-task
description: >-
  Turn a task into a reviewable step-by-step plan written to
  .ai/current-plan.md, grounded in the real files, with sensitive-area flags for
  Opus. Use when planning a feature or fix in Architect mode, breaking work into
  steps before any code, or deciding scope and risks.
---

# Plan a task → .ai/current-plan.md

Architect is read-only on code (markdown only). Ground the plan in reality
before writing it.

## Before planning

1. Read `.ai/handoff.md` (resume prior session if present).
2. Read `.ai/{project-context,architecture,coding-standards}.md` and
   **`.ai/spec.md`** (invariant contracts — never plan a step that breaks one).
3. Open the actual source files the task touches. Confirm the functions and
   patterns referenced really exist — don't plan against assumed names.

## Write the plan (overwrite `.ai/current-plan.md`)

```markdown
# Current Plan — <task>
## Goal            # 1–2 sentences, the observable outcome
## Allowed files   # exact paths Code may edit
## Do not touch    # sensitive/out-of-scope paths
## Steps           # checklist, each step names one file + the change
## Acceptance criteria  # how we verify (py_compile, /health, log line, DB row)
## Risks / Opus review?  # yes/no + why
```

## Sensitive-area gate (flag "Opus review: yes" if the task touches)

- `auth/main.py` — auth capture/refresh, profile rotation
- `shared/database.py` — schema/migration
- `scanners/main.py`, `status_sync/*` — ERP webhook payload (money fields)
- `coordinator/*` — delivery dispatch (money flow)

## Stop conditions

- Plan written → **stop, ask for approval** before any code.
- Trivial docs-only task → may proceed if user agrees.
- Task fans out → write `.ai/handoff.md` and suggest splitting sessions.
  Verification commands live in `.ai/test-commands.md`; there is no pytest suite.
