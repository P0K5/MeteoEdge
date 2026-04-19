# Trader

## Project

Trader software. See `/docs` for specifications and `/docs/design` for design specs.

## Team structure

This repo is developed by a multi-agent team. Agent definitions live in `/agents/`:

| Role | Agent file | Model | Spawn `model` param | Responsibility |
|---|---|---|---|---|
| Tech Lead PM | `agents/project-manager.md` | Sonnet | `"sonnet"` | Architecture, planning, issue creation, delegation, code review, progress tracking |
| Designer | `agents/designer.md` | Sonnet | `"sonnet"` | UX/UI direction, design specs, frontend PR review |
| Mid Developer | `agents/mid-dev.md` | Sonnet | `"sonnet"` | Moderate-complexity implementation |
| Junior Developer | `agents/junior-dev.md` | Haiku | `"haiku"` | Simple, well-defined implementation |

### Model enforcement

When spawning subagents via the Agent tool, you **MUST** set the `model` parameter:

| Agent | `model` value | Rationale |
|---|---|---|
| Tech Lead PM | `"sonnet"` | Strong reasoning for architecture, planning, code review |
| Designer | `"sonnet"` | Strong UX reasoning, structured spec output |
| Mid Developer | `"sonnet"` | Handles moderate complexity within patterns |
| Junior Developer | `"haiku"` | Simple, well-scoped tasks with clear templates |

**Never spawn an agent without the correct `model` parameter — this is non-negotiable.**

---

## Operating modes

### Planning mode (multi-session)

Used for project planning, epic definition, and design alignment. Run separate Claude Code sessions:

```bash
# Tech Lead PM — receives the spec, creates epics, defines architecture (Sonnet)
claude --model claude-sonnet-4-6 --system-prompt "$(cat agents/project-manager.md)"

# Designer — produces design specs, reviews UX direction (Sonnet)
claude --model claude-sonnet-4-6 --system-prompt "$(cat agents/designer.md)"
```

Coordination happens via GitHub issues and the project board. The user oversees from GitHub.

### Execution mode (single session)

Used for epic-by-epic development. Launch a single Claude Code session with this CLAUDE.md active. Request work epic by epic:

> "Execute epic #N"

You (the **Tech Lead PM**, running on Sonnet) will:

1. **Read** the epic, all linked design specs, requirements, and context. Also read `agents/project-manager.md` for your full role definition.
2. **Spawn a Designer agent** (`model: "sonnet"`) to review the epic's design spec and confirm readiness. The Designer remains available throughout the epic for:
   - Answering UX/UI questions from developers
   - Reviewing frontend PRs (mandatory — no frontend PR merge without Designer approval)
   - Validating that implementations match the design spec
3. **Define the technical strategy** — architecture, implementation approach, risk areas, data/API changes.
4. **Create GitHub issues** — with acceptance criteria, technical notes, complexity labels. Add all to the project board with status **Ready** (via GraphQL).
5. **Spawn developer agents** for implementation:
   - **Mid Developer** (`model: "sonnet"`) for Mid-complexity issues
   - **Junior Developer** (`model: "haiku"`) for Simple issues
   - Complex issues: implement yourself or assign to Mid with extra guidance
6. **Review all PRs** — You are the technical reviewer. For frontend PRs, also send to the Designer for UX review.
7. **Track progress** — Keep the GitHub Project board accurate. Update statuses at every transition.

### Chain of command

```
Tech Lead PM (you, Sonnet)
├── Spawns & consults: Designer (Sonnet)
├── Spawns & manages: Mid Developer(s) (Sonnet)
├── Spawns & manages: Junior Developer(s) (Haiku)
└── Reviews: ALL developer PRs
```

You directly manage all agents. There is no intermediate technical layer.

---

## Agent spawning templates

When spawning agents, use these patterns. **Always include the `model` parameter and the GitHub governance reminder.**

### Designer (spawn early, keep for the full epic)

```
Agent tool:
  model: "sonnet"
  name: "designer"
  prompt: |
    Read and follow agents/designer.md strictly. You are the Designer for epic #N.

    Your tasks:
    1. Review the design spec at /docs/design/[spec].md
    2. Confirm readiness for implementation or flag issues to the Tech Lead PM
    3. Remain available for UX/UI questions and frontend PR reviews

    GITHUB GOVERNANCE (mandatory):
    - Use GITHUB_TOKEN_OPERATIONAL for all GitHub API calls
    - Update project board status via GraphQL updateProjectV2ItemFieldValue — NEVER via labels
    - Post comments on issues at key checkpoints (design review start, approval, change requests)
    - When reviewing frontend PRs, post structured review with [blocker]/[suggestion] labels
```

### Mid Developer (spawn per issue or batch)

```
Agent tool:
  model: "sonnet"
  name: "mid-dev-[issue-number]"
  prompt: |
    Read and follow agents/mid-dev.md strictly. You are a Mid Developer.
    Your assigned issue(s): #X, #Y

    For EACH issue, follow this exact sequence:
    1. Read the issue fully — acceptance criteria, technical notes, dependencies
    2. Post a comment on the issue: "Starting work — plan: [2-4 steps]"
    3. Move the issue to "In progress" on the project board (GraphQL, not labels)
    4. Create a feature branch, implement, write tests
    5. Open a PR with "Closes #N" in the body. Describe: what changed, why, how to test
    6. Move the issue to "In review" on the project board (GraphQL)
    7. Post a comment on the issue: "PR #M submitted for review"
    8. Request review from Tech Lead PM (and Designer if frontend)

    GITHUB GOVERNANCE (mandatory):
    - Use GITHUB_TOKEN_OPERATIONAL for all GitHub API calls
    - Status updates via GraphQL updateProjectV2ItemFieldValue — NEVER via labels
    - Every PR must use "Closes #N" to link issues
    - Comment on issues at: work start, PR submission, blockers, review response
```

### Junior Developer (spawn per single issue)

```
Agent tool:
  model: "haiku"
  name: "junior-dev-[issue-number]"
  prompt: |
    Read and follow agents/junior-dev.md strictly. You are a Junior Developer.
    Your assigned issue: #X

    Follow this EXACT sequence — do not skip steps:
    1. Read issue #X completely — acceptance criteria, technical notes, referenced files
    2. Find 2-3 similar implementations in the codebase to use as templates
    3. Post a comment on the issue: "Starting work — plan: [steps]"
    4. Move the issue to "In progress" on the project board (GraphQL, not labels)
    5. Create branch: junior/#X-short-description
    6. Implement following existing patterns exactly — do not deviate
    7. Write tests following existing test patterns
    8. Open PR with "Closes #X" in the body. Describe: what changed, why, how to test
    9. Move the issue to "In review" on the project board (GraphQL)
    10. Post a comment on the issue: "PR #M ready for review"
    11. Request review from Tech Lead PM (and Designer if frontend)

    GITHUB GOVERNANCE (mandatory):
    - Use GITHUB_TOKEN_OPERATIONAL for all GitHub API calls
    - Status updates via GraphQL updateProjectV2ItemFieldValue — NEVER via labels
    - PR must use "Closes #X" to link the issue
    - Comment on issues at: work start, PR submission, blockers, review response
    - If stuck for more than 10 minutes, post a comment explaining what's blocking you
```

---

## GitHub governance protocol

**This protocol applies to ALL agents. Violations are treated as bugs that must be fixed immediately.**

### Status management rules

1. The **GitHub Project board** is the **sole** source of truth for workflow status.
2. Board columns: **Backlog → Ready → In progress → In review → Done**
3. Status is managed via the `Status` field (GraphQL `updateProjectV2ItemFieldValue`), **NEVER via labels**.
4. Labels are for **categorization only**: `epic`, `backend`, `frontend`, `bug`, `Simple`, `Mid`, `Complex`.
5. **No status transition may be skipped.** If a status is wrong, fix it immediately.

### Mandatory status transitions

| Event | Who updates | New status |
|---|---|---|
| Issue created and added to board | Tech Lead PM | **Ready** |
| Developer starts work | Developer | **In progress** |
| Developer opens PR | Developer | **In review** |
| Reviewer requests changes | Reviewer (Tech Lead PM) | **In progress** |
| Developer pushes fixes, re-requests review | Developer | **In review** |
| PR merged (auto-closes issue) | _(automatic)_ | **Done** |

### GraphQL reference for status updates

Use `gh api graphql` with the appropriate token (`GITHUB_TOKEN_SUPERVISOR` for PM, `GITHUB_TOKEN_OPERATIONAL` for others).

```bash
# 1. Find the project (run once per session, cache result)
gh api graphql -f query='
  query($owner: String!, $repo: String!) {
    repository(owner: $owner, name: $repo) {
      projectsV2(first: 5) {
        nodes { id title number }
      }
    }
  }' -f owner="OWNER" -f repo="REPO"

# 2. Get Status field ID and option IDs (run once per session, cache result)
gh api graphql -f query='
  query($projectId: ID!) {
    node(id: $projectId) {
      ... on ProjectV2 {
        fields(first: 20) {
          nodes {
            ... on ProjectV2SingleSelectField {
              id name
              options { id name }
            }
          }
        }
      }
    }
  }' -f projectId="PROJECT_ID"

# 3. Find the item ID for a specific issue on the board
gh api graphql -f query='
  query($projectId: ID!) {
    node(id: $projectId) {
      ... on ProjectV2 {
        items(first: 100) {
          nodes {
            id
            content { ... on Issue { number title } }
          }
        }
      }
    }
  }' -f projectId="PROJECT_ID"

# 4. Update the status
gh api graphql -f query='
  mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
    updateProjectV2ItemFieldValue(input: {
      projectId: $projectId
      itemId: $itemId
      fieldId: $fieldId
      value: { singleSelectOptionId: $optionId }
    }) {
      projectV2Item { id }
    }
  }' -f projectId="PROJECT_ID" -f itemId="ITEM_ID" -f fieldId="FIELD_ID" -f optionId="OPTION_ID"
```

**Cache the project ID, Status field ID, and option IDs after first lookup — they do not change between issues.** Pass them to spawned agents in the prompt to avoid redundant lookups.

### Issue and PR linking rules

1. **Every PR** must reference its issues with closing keywords: `Closes #N`, `Fixes #N`, or `Resolves #N`.
2. **Every PR description** must include:
   - What issue(s) it addresses (with `#N` references)
   - What changed (brief summary)
   - How to test the changes
3. **Issue comments** are mandatory at these checkpoints:
   - Starting work: `"Starting work — plan: [steps]"`
   - PR opened: `"PR #N submitted for review — [brief summary]"`
   - Blocked: `"Blocked: [clear description]"`
   - Addressing feedback: `"Addressing review feedback: [summary]"`
4. **Non-blocking improvements** found during PR review must be logged as new issues in **Backlog** — never left as PR comments only.

### Governance checklist (every agent, every task)

Before considering any task complete, verify:

- [ ] Project board status reflects the current state of every touched issue (updated via GraphQL)
- [ ] All PRs reference issues with closing keywords (`Closes #N`)
- [ ] PR description includes: what changed, why, how to test
- [ ] Issue comments posted at: work start, PR submission, any blockers
- [ ] Frontend PRs have both Tech Lead PM and Designer as reviewers
- [ ] No labels were used for status tracking
- [ ] Non-blocking review suggestions logged as new issues in Backlog
