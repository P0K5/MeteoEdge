# Continuous Improvement Loop (reflect skill)

How the meta-learning loop from issue #747 works, how to run it, and how to
review its proposals. Scope follows the issue's review comment: **v1 is a
manual/weekly batch pass — no per-session CI plumbing.**

## What it does

The `reflect` skill (`.claude/skills/reflect/SKILL.md`) batch-analyzes recent
local Claude Code session transcripts for prompt inefficiencies — avoidable
clarifications, redundant tool calls, repeated context re-establishment,
instruction-induced verbosity — and opens **one consolidated proposal PR**
with diffs against the prompt surface (`.claude/agents/`, `.claude/skills/`,
`.claude/instructions/`, `agents/pm.md`, CLAUDE.md templates).

Nothing is ever auto-merged. Every proposal needs a human decision.

## Running a reflection pass

In a Claude Code session at the repo root:

```
/reflect            # or: "run a reflection pass over the last 10 sessions"
```

The skill drives four stages, all local-first:

1. **Sanitize** — `scripts/sanitize_transcript.py` redacts keys, tokens, and
   PII from `~/.claude/projects/<project>/*.jsonl` copies. Raw transcripts
   never leave the machine. Redaction is best-effort: treat sanitized copies
   as sensitive, keep them out of git, and delete them after the run
   (if any are ever attached as CI artifacts, set retention ≤ 7 days).
2. **Measure** — `scripts/reflection_metrics.py` logs tokens-per-completed-issue
   to `docs/reflection/metrics.jsonl` (committed; this is the convergence metric).
3. **Triage (cost guard)** — `scripts/reflect_triage.py` runs the cheap first
   pass on DeepSeek (reuses the `DEEPSEEK_API_KEY` already wired
   into CI). Claude is used only to turn confirmed findings into diffs.
4. **Propose** — one PR on branch `reflect/<date>` with the proposal JSON
   (`docs/reflection/proposals/<date>.json`), the metrics entry, and file
   diffs for high/medium-confidence proposals.

## Reviewing a proposal PR

Reject any proposal that fails these gates:

1. **Evidence:** cites specific transcript file + turn indices with a short
   quote. No citation → no merge. (Primary defense against false positives.)
2. **Impact:** states expected token impact ("removes ~800 tokens per
   junior-dev spawn"). Vague benefit claims don't count.
3. **Surface:** touches only the allowed optimization surface. Changes to the
   reflect skill itself, the sanitizer/triage/metrics scripts, or
   `agents/reviewer_prompt_deepseek.md` are forbidden (no self-modification of
   the loop).
4. **No behavior change:** scope/authority boundaries of agents must carry
   over verbatim — reflection optimizes wording and structure, not permissions.

Merge selectively — it's normal to take 2 of 5 proposals. Intentional
verbosity (mandated governance comments, teaching-oriented junior feedback)
is the known false-positive class; the skill marks suspected cases
`confidence: low`.

## Closing the loop

After a proposal PR merges, the next reflection run recomputes
tokens-per-completed-issue and reports the delta against the previous entry
in `metrics.jsonl`. If the number didn't move, revisit (or revert) the merged
proposals — accumulating prompt edits without measured benefit is exactly the
drift this loop exists to prevent. The pre-restructure baseline lives in
`docs/reflection/baseline.md`.

## v2 (deferred)

- Per-session automation via a local `SessionEnd` hook firing
  `repository_dispatch` with a sanitized payload — only after the analysis
  prompt has proven itself across several manual runs.
- Improving the reflect skill itself (bootstrapping problem deferred).
