---
name: pr-review
description: Tech Lead PM protocol for reviewing a developer PR — AI-reviewer-first triage, review dimensions, the non-negotiable CI green gate, and mandatory post-review board actions. Use whenever reviewing, approving, or merging any PR.
---

# PR Review Protocol (Tech Lead PM)

Full governance rules: `.claude/instructions/governance.md`.

## 1. AI-first triage — always start here

Before any manual analysis, read the latest `AI / NVIDIA NIM review` GitHub
Check and its PR comment summary (reviewer model: GLM-5.2 via NVIDIA NIM;
prompt: `agents/reviewer_prompt_glm52.md`).

- **Verdict PASS:** validate the checklist items briefly, then proceed toward approval.
- **Verdict BLOCK:** read each blocking item and either spawn a mid-dev/junior-dev
  agent to fix the specific items, or request changes referencing the findings.

**Manual deep-dive is ONLY required when:**

1. The PR touches trading logic, position sizing, EMOS calibration, guardrail
   events, DB schema migrations, systemd units, or deployment scripts.
2. The AI reviewer output is incomplete, missing context, or clearly incorrect.
3. The PR has no linked issues (no acceptance criteria to verify against).

Do not re-read the full diff if the AI reviewer already summarized impact and
blast radius — start from its output, not from scratch.

## 2. Review dimensions

- **Correctness:** solves the linked issue; ALL acceptance criteria met; edge
  cases handled; no regressions.
- **Architecture & design:** consistent with existing patterns; no unwanted
  coupling; abstractions appropriate; state managed correctly.
- **Code quality:** project conventions followed; logic easy to follow; no
  magic numbers or duplicated logic; errors handled explicitly.
- **Performance & security:** no N+1 queries or blocking operations; input
  validated; secrets and PII handled correctly.
- **Test coverage:** happy path, edge cases, failure modes; tests fail when
  the implementation is broken.
- **Documentation currency:** if the PR adds/changes/removes env vars, DB
  tables/columns, run modes, log paths, deployment steps, CLI args, or API
  endpoints, then `docs/OPERATIONS.md` and/or `docs/DB_SCHEMA.md` must be
  updated — otherwise it's a `[blocker]`.

## 3. Review output format

- Overall assessment first: **approve**, **request changes**, or **needs discussion**.
- Group comments by file or concern; label each `[blocker]`, `[suggestion]`,
  `[question]`, or `[nitpick]`.
- Blockers must be resolved before approval. Every genuine `[suggestion]` MUST
  be logged as a new issue in Backlog — never left as a PR comment only.
- Junior PRs: detailed, teaching-oriented feedback. Mid PRs: focus on
  correctness and architecture, trust them on style.

## 4. CI gate — approval requires green checks (NON-NEGOTIABLE)

Before posting ANY approval:

1. Retrieve check runs for the PR's **head SHA** (`mcp__github__actions_list`
   or equivalent).
2. Every check must be `completed` with `conclusion: success` — including
   `CI / lint`, `CI / test`, and `AI / NVIDIA NIM review`.
3. `in_progress`/`queued` → wait and re-check. `failure`/`cancelled` → comment
   identifying the failing check, move linked issues back to **In progress**
   (board-status skill), request fixes. Do NOT approve.

Never approve while CI is running, never approve with a failing check, never
push directly to master.

**Frontend PRs** also require Designer approval before merge; Designer change
requests are blockers.

## 5. Post-review actions (review is NOT complete without these)

Pre-approval checklist:

- [ ] All acceptance criteria from the linked issue met
- [ ] All CI checks green on the head SHA (verified, not assumed)
- [ ] Correctness, architecture, quality, performance reviewed
- [ ] Test coverage adequate for the scope
- [ ] Docs updated to match PR scope

Then update the board (board-status skill) for every linked issue:

| Verdict | Action |
|---|---|
| Request changes | Move linked issues to **In progress**; comment on the PR listing which issues moved back and why |
| Approve | Leave statuses as-is (auto-Done on merge) |
| Needs discussion | Move linked issues to **In progress**; post open questions on each affected issue |
