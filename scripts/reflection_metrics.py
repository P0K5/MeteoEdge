#!/usr/bin/env python3
"""Compute the tokens-per-completed-issue baseline from session transcripts.

This is the convergence metric for the reflection loop (issue #747): each
reflection proposal PR states an expected token impact, and the next run of
this script shows whether the number actually moved after merge.

Token usage is read from the per-turn `usage` fields that Claude Code writes
into transcript JSONL. Completed issues are counted from closing keywords
("Closes #N" / "Fixes #N" / "Resolves #N") seen in the transcript; pass
--issues to override when the heuristic miscounts.

Usage:
  python scripts/reflection_metrics.py <sanitized.jsonl> [...] \
      [--issues N] [--append docs/reflection/metrics.jsonl] [--label "epic-12"]
"""

import argparse
import datetime
import json
import re
from pathlib import Path

CLOSING_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)
USAGE_KEYS = ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens")


def walk(value):
    """Yield every dict nested anywhere inside value."""
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


def analyze_file(path):
    totals = {k: 0 for k in USAGE_KEYS}
    issues = set()
    turns = 0
    with open(path, encoding="utf-8") as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            turns += 1
            for d in walk(record):
                if "input_tokens" in d or "output_tokens" in d:
                    for k in USAGE_KEYS:
                        v = d.get(k)
                        if isinstance(v, int):
                            totals[k] += v
            for m in CLOSING_RE.finditer(line):
                issues.add(int(m.group(1)))
    return totals, issues, turns


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("transcripts", nargs="+",
                        help="Sanitized transcript JSONL files")
    parser.add_argument("--issues", type=int, default=None,
                        help="Override the completed-issue count")
    parser.add_argument("--append", default=None,
                        help="Metrics JSONL file to append the result to")
    parser.add_argument("--label", default="",
                        help="Free-text label for this measurement (e.g. epic number)")
    args = parser.parse_args()

    grand = {k: 0 for k in USAGE_KEYS}
    all_issues = set()
    n_turns = 0
    for name in args.transcripts:
        totals, issues, turns = analyze_file(Path(name))
        for k in USAGE_KEYS:
            grand[k] += totals[k]
        all_issues |= issues
        n_turns += turns
        print(f"[metrics] {Path(name).name}: turns={turns} "
              f"in={totals['input_tokens']} out={totals['output_tokens']} "
              f"cache_r={totals['cache_read_input_tokens']} issues={sorted(issues)}")

    issue_count = args.issues if args.issues is not None else len(all_issues)
    # Cache reads are cheap; the cost driver is uncached input + output.
    billed = grand["input_tokens"] + grand["output_tokens"] \
        + grand["cache_creation_input_tokens"]
    per_issue = round(billed / issue_count) if issue_count else None

    entry = {
        "date": datetime.date.today().isoformat(),
        "label": args.label,
        "transcripts": [Path(n).name for n in args.transcripts],
        "turns": n_turns,
        "usage": grand,
        "billed_tokens": billed,
        "issues_completed": issue_count,
        "issue_numbers": sorted(all_issues),
        "tokens_per_completed_issue": per_issue,
    }
    print(json.dumps(entry, indent=2))

    if args.append:
        out = Path(args.append)
        out.parent.mkdir(parents=True, exist_ok=True)
        prev = None
        if out.exists():
            lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                prev = json.loads(lines[-1]).get("tokens_per_completed_issue")
        with open(out, "a", encoding="utf-8") as fout:
            fout.write(json.dumps(entry) + "\n")
        if prev and per_issue:
            delta = per_issue - prev
            pct = 100.0 * delta / prev
            print(f"[metrics] tokens/issue: {prev} -> {per_issue} ({pct:+.1f}%)")
        print(f"[metrics] Appended to {out}")


if __name__ == "__main__":
    main()
