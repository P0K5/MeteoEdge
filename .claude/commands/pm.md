---
description: Adopt the Tech Lead PM role for this session
---

Read `agents/pm.md` in full and adopt that role for the remainder of this
session — you are TechLead-PM. Then run session init as described in
`CLAUDE.md`:

1. Read `.claude/session-context.env` (pre-resolved GitHub Project IDs). If
   values are empty or missing, run `bash scripts/bootstrap_session.sh` first.
2. Read `.claude/model-config.env` for `BACKEND` context (fall back to
   `BACKEND=anthropic` if missing).

This is the GUI-native equivalent of launching the CLI with
`claude --system-prompt "$(cat agents/pm.md)"` — `agents/pm.md` stays outside
`.claude/agents/` because it is the orchestrating root session, not a
spawnable subagent (see the note in `CLAUDE.md` on why).