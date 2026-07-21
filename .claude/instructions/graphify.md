# Graphify Usage Rules — SINGLE SOURCE OF TRUTH

This is the only copy of the graphify rules. Agent definitions and CLAUDE.md
reference this file — do not re-inline it elsewhere.

A pre-built knowledge graph lives at `graphify-out/graph.json` and is committed
to the repo. It is updated incrementally on pushes to `master` and rebuilt
semantically on a weekly schedule via CI.

At the start of any session:

1. If `graphify` is not available on PATH, install it with `pip install graphifyy`.
2. Use the committed graph normally — no extraction is needed for standard
   codebase questions.

## Rules

- For codebase questions, first run `graphify query "<question>"`. Use
  `graphify path "<A>" "<B>"` for relationships between modules and
  `graphify explain "<concept>"` for focused concepts. These return a scoped
  subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If `graphify-out/wiki/index.md` exists, use it for broad navigation instead
  of raw source browsing.
- Read `graphify-out/GRAPH_REPORT.md` only for broad architecture review or
  when query/path/explain do not surface enough context.
- After modifying code locally, run `graphify update .` to keep the graph
  current (AST-only, no API cost).
- Do not run `graphify extract` unless explicitly requested, the committed
  graph is missing, or a major architecture/doc change requires a fresh
  semantic rebuild.

The weekly full-rebuild CI job uses NVIDIA NIM (GLM-5.2) via
`NVIDIA_NIM_API_KEY` for semantic extraction and community clustering.
