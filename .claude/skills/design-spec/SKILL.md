---
name: design-spec
description: Designer formats — the design spec template for /docs/design/ and the frontend PR review rubric. Use when writing or updating a design spec, or when reviewing a frontend PR for UX fidelity.
---

# Design Spec Format & Frontend PR Review Rubric

## Design spec template

Store specs in `/docs/design/` as markdown. Structure:

```markdown
# Design Spec — [Feature Name]

## User goal
[What the user is trying to accomplish]

## User flow
1. [Step-by-step flow with decision points]

## Screen layouts
### [Screen name]
- **Purpose:** [what this screen does]
- **Key components:** [list of UI elements]
- **Layout:** [spatial arrangement — ASCII diagrams or structured lists]
- **Interactions:** [what happens on click, type, hover, etc.]
- **States:** [default, loading, empty, error, success]

## Design tokens / references
- [Colors, spacing, typography — reference the design system if established]

## Accessibility notes
- [Keyboard navigation, ARIA labels, contrast requirements]

## Open questions
- [Anything unresolved]
```

Every spec must cover all states — a happy-path-only spec is incomplete.
Record agreed technical compromises inline:
`[Tech constraint: reason — agreed approach: X]`.

## Frontend PR review rubric

You are a required reviewer on all PRs touching UI components, layouts,
styles, or user-facing text. Evaluate:

- **Fidelity to spec:** layout, spacing, component usage, interaction
  behavior, and states match the approved design spec.
- **Consistency:** follows the established design language and component patterns.
- **UX quality:** intuitive interactions, helpful error messages, immediate
  and clear feedback.
- **Accessibility:** sufficient contrast, working keyboard navigation, ARIA
  labels present.
- **Edge cases:** long text, empty data, many items, small screens.

**Output format:** overall assessment first (**approve** / **request changes**
/ **needs discussion**), comments grouped by screen or component, each labeled
`[blocker]`, `[suggestion]`, or `[question]`. Flag genuine `[suggestion]`
items explicitly so the Tech Lead PM logs them as Backlog issues.

Your approval is required alongside the Tech Lead PM's — the PR cannot merge
without both.
