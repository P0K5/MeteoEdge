---
name: reflect
description: Continuous-improvement reflection pass over recent agent session transcripts. Analyzes local Claude Code transcripts for token inefficiencies and proposes evidence-cited diffs to agent/skill/instruction files as one consolidated PR. Use when asked to run a reflection, /reflect, or a prompt-optimization pass.
---

# Reflect — Continuous Improvement Loop (v1)

Batch-analyze recent session transcripts, find prompt/instruction
inefficiencies, and open **one consolidated proposal PR**. Human review is
always required — nothing is auto-merged. (Issue #747; scope per its review
comment: manual/batch, no per-session CI plumbing in v1.)

## Hard rules

- **Never propose changes to this skill** (`.claude/skills/reflect/`) or to
  `scripts/sanitize_transcript.py` / `scripts/reflect_triage.py` /
  `scripts/reflection_metrics.py` — the loop must not modify itself.
- **Optimization surface (only these):** `.claude/agents/*.md`,
  `.claude/skills/*/SKILL.md` (except reflect), `.claude/instructions/*.md`,
  `agents/pm.md`, and the CLAUDE.md spawn/task templates.
  Never `agents/reviewer_prompt_glm52.md` (CI-owned) or any `src/` code.
- **Evidence requirement:** every proposed diff must cite the specific
  transcript file and turn indices that motivated it (e.g. "agent re-queried
  the project ID at turns 4, 17, 31 despite it being in the spawn prompt").
  **No citation → no proposal.**
- **Impact requirement:** every proposal must state expected token impact
  (e.g. "removes ~800 tokens of preamble per junior-dev spawn").
- **Sanitize before anything leaves the machine.** Raw transcripts contain
  trading logic and API interactions — run the sanitizer first, always.

## Workflow

1. **Collect transcripts.** Local session transcripts live in
   `~/.claude/projects/<project-dir>/*.jsonl` (the project dir is the CWD path
   with `/` replaced by `-`). Take the last N sessions (default 10, or as asked).

2. **Sanitize locally:**
   ```bash
   python scripts/sanitize_transcript.py <transcript.jsonl> ... -o /tmp/reflect-sanitized/
   ```
   Work only from the sanitized copies from here on.

3. **Update the token baseline:**
   ```bash
   python scripts/reflection_metrics.py /tmp/reflect-sanitized/*.jsonl \
       --append docs/reflection/metrics.jsonl
   ```
   This logs tokens-per-completed-issue — the convergence metric. Compare
   against previous entries: if a prior reflection PR merged, report whether
   the number moved.

4. **Cost-guard triage (default).** Run the cheap-model pass first —
   NVIDIA NIM GLM-5.2, already wired into CI, not Claude:
   ```bash
   NVIDIA_NIM_API_KEY=... python scripts/reflect_triage.py \
       /tmp/reflect-sanitized/*.jsonl -o /tmp/reflect-findings.json
   ```
   Escalate to Claude (yourself) only to turn confirmed findings into
   well-written file diffs. If the key is unavailable, do the triage yourself
   but note the cost-guard bypass in the PR body.

5. **Analyze** (triage rubric — what counts as a finding):
   - User corrections / rephrasing that clearer upfront instructions would have avoided
   - Tool calls returning empty or irrelevant results (poor query formulation)
   - Clarifying questions that better skill/agent context would have prevented
   - Repeated context re-establishment across turns (redundant preamble tokens)
   - Instructions causing over-explanation or unnecessary verbosity
   - Re-querying data that was already pre-resolved (e.g. session-context IDs)

6. **Produce proposals** in this schema (one JSON file per run, committed
   under `docs/reflection/proposals/<date>.json`):
   ```json
   {
     "run_date": "YYYY-MM-DD",
     "transcripts_analyzed": ["<sanitized filenames>"],
     "baseline": {"tokens_per_completed_issue": 0, "delta_vs_previous": null},
     "proposals": [
       {
         "id": "P1",
         "target_file": ".claude/agents/junior-dev.md",
         "category": "redundant-context | vague-instruction | poor-query | verbosity | missed-skill",
         "evidence": [{"transcript": "<file>", "turns": [4, 17, 31], "quote": "<short excerpt>"}],
         "diff_summary": "<what changes and why>",
         "expected_impact": "<e.g. ~800 tokens saved per junior-dev spawn>",
         "confidence": "high | medium | low"
       }
     ]
   }
   ```

7. **Open ONE consolidated PR** on branch `reflect/<YYYY-MM-DD>` containing
   the proposal JSON, the metrics update, and the actual file diffs for
   high/medium-confidence proposals (low-confidence ones stay JSON-only for
   discussion). PR body: per-proposal summary table with evidence links and
   expected impact. Follow `.claude/instructions/governance.md` (link an
   issue, CI green before merge). Reviewer guidance: `docs/REFLECTION.md`.

Flag intentional-looking verbosity as `low` confidence — false positives are
the known failure mode; the human reviewer decides.
