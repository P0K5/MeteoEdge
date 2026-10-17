#!/usr/bin/env python3
"""Compute the tokens-per-completed-issue baseline from session transcripts.

This is the convergence metric for the reflection loop (issue #747): each
reflection proposal PR states an expected token impact, and the next run of
this script shows whether the number actually moved after merge.

Token usage is read from the per-turn `usage` fields that Claude Code writes
into transcript JSONL. Completed issues are counted from closing keywords
("Closes #N" / "Fixes #N" / "Resolves #N") seen in the transcript; pass
--issues to override when the heuristic miscounts.

Billing is backend-aware per turn (issue #931): Anthropic models report
`input_tokens` as the uncached-only portion of the prompt (cache_creation is
additive on top), while other backends run through this repo (e.g. DeepSeek
via the NVIDIA NIM proxy) report `input_tokens` as the *total* prompt, with
cache_creation/cache_read as a breakdown of that same total. See
`classify_backend` / `billed_for_usage` below.

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

# Model-name prefix for backends that use Anthropic's native usage-accounting
# convention: `input_tokens` is the uncached-prompt portion only, and
# `cache_creation_input_tokens` / `cache_read_input_tokens` are reported
# separately on top of it (additive). Confirmed against real transcripts:
# claude-opus-5 turns show input_tokens=2 alongside cache_creation=49490.
#
# Non-Anthropic backends run through this repo (e.g. DeepSeek via the
# NVIDIA NIM proxy, model "deepseek-v4-pro") instead report `input_tokens`
# as the *total* prompt size, with cache_creation/cache_read as a breakdown
# of that same total: confirmed against real transcripts, e.g.
# input_tokens=44221 == cache_creation_input_tokens(829) +
# cache_read_input_tokens(43392). Adding cache_creation back on top of that
# total double-counts it (issue #931) -- it must be dropped for this
# backend class instead.
ANTHROPIC_MODEL_RE = re.compile(r"^claude[-.]", re.IGNORECASE)


def classify_backend(model):
    """Classify a transcript `model` field as 'anthropic' or 'other'.

    Defaults to 'anthropic' when model is missing/unknown, preserving this
    script's original (Anthropic-only) behavior for older transcripts and
    for test fixtures that omit the field.
    """
    if isinstance(model, str) and ANTHROPIC_MODEL_RE.match(model):
        return "anthropic"
    if model:
        return "other"
    return "anthropic"


def billed_for_usage(usage, backend):
    """Billed tokens for one usage record, backend-aware (issue #931)."""
    input_t = usage.get("input_tokens") or 0
    output_t = usage.get("output_tokens") or 0
    cache_creation = usage.get("cache_creation_input_tokens") or 0
    if backend == "other":
        # input_tokens already equals cache_creation + cache_read here --
        # adding cache_creation again would double-count it.
        return input_t + output_t
    # Anthropic: cache reads are cheap and dropped; cache_creation is a
    # real, separate cost on top of the uncached input.
    return input_t + output_t + cache_creation


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
    billed = 0
    backend_counts = {}
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
                if not isinstance(d, dict):
                    continue
                usage = d.get("usage")
                if not isinstance(usage, dict):
                    continue
                if "input_tokens" not in usage and "output_tokens" not in usage:
                    continue
                for k in USAGE_KEYS:
                    v = usage.get(k)
                    if isinstance(v, int):
                        totals[k] += v
                backend = classify_backend(d.get("model"))
                backend_counts[backend] = backend_counts.get(backend, 0) + 1
                billed += billed_for_usage(usage, backend)
            for m in CLOSING_RE.finditer(line):
                issues.add(int(m.group(1)))
    return totals, issues, turns, billed, backend_counts


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
    billed = 0
    backends = {}
    for name in args.transcripts:
        totals, issues, turns, file_billed, file_backends = analyze_file(Path(name))
        for k in USAGE_KEYS:
            grand[k] += totals[k]
        all_issues |= issues
        n_turns += turns
        billed += file_billed
        for b, c in file_backends.items():
            backends[b] = backends.get(b, 0) + c
        print(f"[metrics] {Path(name).name}: turns={turns} "
              f"in={totals['input_tokens']} out={totals['output_tokens']} "
              f"cache_r={totals['cache_read_input_tokens']} "
              f"backend={file_backends} issues={sorted(issues)}")

    issue_count = args.issues if args.issues is not None else len(all_issues)
    # billed is computed per-turn with the backend-correct formula (#931) --
    # it is NOT grand['input_tokens'] + output + cache_creation, because
    # that single formula is only valid when every turn in the batch used
    # the same backend convention.
    per_issue = round(billed / issue_count) if issue_count else None

    entry = {
        "date": datetime.date.today().isoformat(),
        "label": args.label,
        "transcripts": [Path(n).name for n in args.transcripts],
        "turns": n_turns,
        "usage": grand,
        "backends": backends,
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
