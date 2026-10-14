#!/usr/bin/env python3
"""CI guard for the PR body (issue #791). Offline: no network, all inputs are
passed as arguments / files by the workflow.

Two deterministic rules, nothing more (the AI reviewer covers the rest):

1. The body has a non-empty "## How to deploy" section: at least one ticked
   box (- [x]) or other non-comment text such as "No deploy needed". A section
   holding only the template's UNticked boxes / placeholder comments fails.
2. Every issue number in a closing keyword (Closes/Fixes/Resolves #N) also
   appears in the head branch name, the PR title, or a commit message of the
   PR. Catches a body written for the wrong PR. "Part of/Refs #N" is exempt.

NOTE: closing keywords in PROSE count (GitHub itself closes the issue on merge
wherever "Closes #N" appears). Write examples without the literal keyword+hash
(e.g. "closes issue 944") or inside a `code span` / fenced block, which this
script ignores.

Exempt: bot authors ("*[bot]"), titles starting "chore(graph)" or "chore(ci)",
and "[skip ci]" in the title. Docs PRs are NOT exempt.

Usage:
  python3 scripts/check_pr_body.py --body-file F [--branch B] [--title T]
      [--commits-file F] [--author A]
Exit 0 = OK/exempt, 1 = violation, 2 = usage error.
"""
import argparse
import re
import sys

TEMPLATE_HINT = "See .github/pull_request_template.md."

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_FENCE = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`\n]*`")
_HEADING = re.compile(r"^#{2}\s+how to deploy\b.*$", re.I | re.M)
_NEXT_H2 = re.compile(r"^#{1,2}\s+\S", re.M)
_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*"
    r"(#\d+(?:\s*(?:,|and|&)\s*#\d+)*)", re.I)
_UNCHECKED = re.compile(r"^\s*[-*]\s*\[ \]")
_CHECKED = re.compile(r"^\s*[-*]\s*\[[xX]\]\s*\S")
_EXEMPT_TITLE = re.compile(r"^\s*chore\((graph|ci)\)", re.I)


def is_exempt(author, title):
    if (author or "").lower().endswith("[bot]"):
        return True
    return bool(_EXEMPT_TITLE.match(title or "") or
                "[skip ci]" in (title or "").lower())


def deploy_section_error(body):
    text = _COMMENT.sub("", body)
    m = _HEADING.search(text)
    if not m:
        return "missing a '## How to deploy' section"
    rest = text[m.end():]
    n = _NEXT_H2.search(rest)
    section = rest[:n.start()] if n else rest
    for line in section.splitlines():
        if not line.strip() or _UNCHECKED.match(line):
            continue
        return None  # ticked box or explicit text
    return ("'## How to deploy' is empty: tick at least one box or write "
            "'No deploy needed'")


def closing_issues(body):
    text = _INLINE.sub("", _FENCE.sub("", _COMMENT.sub("", body)))
    found = []
    for m in _CLOSING.finditer(text):
        found += re.findall(r"#(\d+)", m.group(1))
    return list(dict.fromkeys(found))


def mismatch_errors(body, branch, title, commits):
    hay = "\n".join([branch or "", title or "", commits or ""])
    errs = []
    for n in closing_issues(body):
        if not re.search(r"(?<!\d)%s(?!\d)" % re.escape(n), hay):
            errs.append(
                "body closes #%s but #%s appears in none of: head branch "
                "'%s', PR title, commit messages (wrong body for this PR?)"
                % (n, n, branch or ""))
    return errs


def check(body, branch="", title="", commits="", author=""):
    """Return a list of violation strings (empty = pass)."""
    if is_exempt(author, title):
        return []
    if not (body or "").strip():
        return ["PR body is empty. " + TEMPLATE_HINT]
    errs = []
    e = deploy_section_error(body)
    if e:
        errs.append(e)
    errs += mismatch_errors(body, branch, title, commits)
    return errs


def _read(path):
    if not path:
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--body-file", required=True)
    ap.add_argument("--branch", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--commits-file", default="")
    ap.add_argument("--author", default="")
    a = ap.parse_args(argv)
    try:
        body, commits = _read(a.body_file), _read(a.commits_file)
    except OSError as exc:
        print("check_pr_body: cannot read input: %s" % exc, file=sys.stderr)
        return 2
    errs = check(body, a.branch, a.title, commits, a.author)
    if errs:
        for e in errs:
            print("PR body check FAILED: " + e)
        print(TEMPLATE_HINT)
        return 1
    print("PR body check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
