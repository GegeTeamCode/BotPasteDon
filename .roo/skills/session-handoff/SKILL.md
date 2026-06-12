---
name: session-handoff
description: >-
  Summarize the current session into .ai/handoff.md so the next session can
  resume cleanly. Use when wrapping up a session, closing out work, the context
  is getting long, or the user says "wrap up", "handoff", or "đóng session".
---

# Close the session → handoff

When a session has run long or context is filling up, write a concise summary
to `.ai/handoff.md` so a fresh session resumes without re-deriving everything.

## Procedure

1. Review what changed this session (touched files, decisions, open threads).
2. Overwrite `.ai/handoff.md` with the template below — keep it tight, this is
   read at the start of the next session, not an essay.
3. If a real architectural decision was made, also append it to
   `.ai/decisions.md`. Append a one-line entry to `.ai/task-log.md`.
4. Tell the user to start the next session by reading `.ai/handoff.md` first.

## Template

```markdown
# Handoff — <YYYY-MM-DD>

## Done
- <what shipped, with the commit if any>

## In-progress (file + line)
- <path:line — what's half-done and the exact next edit>

## Key decisions
- <decision + why, mirror into decisions.md>

## Next steps
- <ordered, smallest-first>

## Gotchas
- <traps hit this session; if it's a NEW class of bug, also add one line to
  .roo/skills-debug/debug-protocol/SKILL.md "Known gotchas">
```

Be concrete: "in-progress" must name the file and the next action, never just
"continue the feature". One goal per session — if work fanned out, say so and
suggest splitting the next session.
