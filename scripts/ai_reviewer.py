#!/usr/bin/env python3
"""
AI Code Reviewer — context pipeline + DeepSeek call

Workflow env vars required:
  DEEPSEEK_API_KEY      — DeepSeek API key
  GITHUB_TOKEN          — GitHub token for API calls
  PR_NUMBER              — PR number being reviewed
  PR_HEAD_SHA            — head commit SHA
  REPO_OWNER             — repo owner
  REPO_NAME              — repo name

Optional:
  DEEPSEEK_BASE_URL     — default https://api.deepseek.com
  DEEPSEEK_MODEL        — default deepseek-chat
  AI_REVIEW_DIFF_MAX_CHARS — diff cap in chars (positive int), default 300000
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration — loaded from env vars once at startup
# ---------------------------------------------------------------------------

REQUIRED_ENV = [
    "DEEPSEEK_API_KEY",
    "GITHUB_TOKEN",
    "PR_NUMBER",
    "PR_HEAD_SHA",
    "REPO_OWNER",
    "REPO_NAME",
]

DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"

GITHUB_API = "https://api.github.com"
# 4000 (~1K tokens) was too small for any non-trivial PR: it silently BLOCKed
# PR #1098 (a 10-file, 67,962-char diff) because GitHub returns diff hunks in
# path order, and 16,878 chars of backtest_results/*.md reports exhausted the
# budget before the reviewer ever saw src/config.py or src/http_client.py --
# the two files it then complained it couldn't verify. 80000 (~20K tokens) is
# still well within DeepSeek's context window (leaves room for the rest of
# the prompt: PR metadata, graphify neighbors, linked-issue body, policy
# text) but covers realistic multi-file feature PRs whole, instead of
# truncating mid-file.
#
# Raised again to 300000 (#1283): a mostly relocation-style refactor (PR
# #1282, ~258K chars of diff) was BLOCKed with "diff truncated, cannot
# complete review" at 80000. deepseek-chat has a 128K-token context and ~260K
# chars of code is roughly 80K tokens, so 300000 fits. The cap is overridable
# via env AI_REVIEW_DIFF_MAX_CHARS (see get_diff_max_chars()). If DeepSeek
# nevertheless rejects the request as exceeding the model context, _run()
# retries ONCE with DIFF_FALLBACK_MAX_CHARS (the previous 80000 cap) and says
# so in the review summary.
#
# Raising this budget fixes each incident it's raised for, but not the root
# cause: GitHub returns diff hunks in path order, and generated paths
# (graphify-out/, regenerated on every code-changing PR per CLAUDE.md's
# `graphify update .` convention and capable of hundreds of thousands of
# lines -- graphify-out/graph.json alone was +233448/-243790 lines in PR
# #1116; backtest_results/, the exact path that caused the original
# #1098/#1107 incident) both sort ahead of src/ alphabetically, so they
# reliably exhaust any flat character budget before the reviewer ever sees
# the actual code (#1118). GENERATED_PATH_PREFIXES below excludes those
# paths from the diff content entirely instead of relying on a bigger budget
# to outrun them; DIFF_MAX_CHARS remains a safety cap on what's left after
# that filtering, not the primary defense.
DIFF_MAX_CHARS = 300_000
DIFF_FALLBACK_MAX_CHARS = 80_000
DIFF_MAX_CHARS_ENV = "AI_REVIEW_DIFF_MAX_CHARS"
SUMMARY_MAX_CHARS = 65535

# ---------------------------------------------------------------------------
# PR body / linked-issue body budgets (#1243)
#
# Before this fix, the packet builder discarded the PR body entirely after
# regexing closing keywords out of it (the model never saw a single character
# of the description, so "the PR body only contains the closing keyword" was
# the reviewer accurately describing its own truncated input, not a real
# finding), and capped each linked issue's body at a flat 500 characters
# (`issue_body[:500]`) -- roughly one paragraph, cutting most issues in this
# repo mid-sentence.
#
# Budgets below were sized against real issue bodies in this repo, measured
# 2026-10-05: #1230 is 8,002 chars, #1236 is 8,644 chars (the two issues
# #1243's acceptance criteria cite by name). Both fit with >1.8x headroom
# under ISSUE_BODY_MAX_CHARS. PR_BODY_MAX_CHARS is larger than any PR body
# observed in this repo's history (the #1241 body that triggered the original
# false BLOCK was 3,511 chars) because, unlike issue bodies, a PR body is
# never fetched elsewhere in the packet, so under-budgeting it has no
# fallback.
#
# Interaction with DIFF_MAX_CHARS: these budgets are additive with the diff
# cap, not sliced out of it. Worst case for a single-issue PR is
# DIFF_MAX_CHARS (300,000) + PR_BODY_MAX_CHARS (20,000) +
# ISSUE_BODY_MAX_CHARS (16,000) + AC_TEXT_MAX_CHARS (4,000, itself a slice of
# the issue body already counted once) ~= 336,000 chars, roughly 84K tokens
# at the ~4 chars/token ratio DIFF_MAX_CHARS's own sizing already assumes --
# still under deepseek-chat's 128K-token context with room for the rest of
# the packet (metadata, graphify context, policy summary). A PR closing
# several issues multiplies ISSUE_BODY_MAX_CHARS per issue, so a PR closing
# many large issues *plus* carrying a near-cap diff can still exceed context;
# that case is covered by the existing DeepSeekContextLengthError retry in
# _run(), which already falls back to a smaller diff cap on a 400. The PR
# body and per-issue budgets are NOT reduced on that retry (they are small
# relative to the diff, so shrinking the diff alone is expected to be
# sufficient in practice) -- if that assumption turns out wrong in a future
# incident, shrink these too in the same retry path.
PR_BODY_MAX_CHARS = 20_000
ISSUE_BODY_MAX_CHARS = 16_000
AC_TEXT_MAX_CHARS = 4_000


def _budget(text: str, max_chars: int) -> tuple:
    """Return (possibly-truncated text, was_truncated)."""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True

# Paths whose diffs are excluded from the AI review packet because they are
# machine-generated and not meaningful for a human/AI code review -- they
# still appear in the "## Changed Files" listing (so the reviewer knows they
# changed, per the `graphify update .` policy) but without diff content.
GENERATED_PATH_PREFIXES = ("graphify-out/", "backtest_results/")


def is_generated_path(filename: str) -> bool:
    return filename.startswith(GENERATED_PATH_PREFIXES)


def get_diff_max_chars() -> int:
    """Return the diff cap: env AI_REVIEW_DIFF_MAX_CHARS if it is a positive
    int, else DIFF_MAX_CHARS (invalid values fall back with a log line)."""
    raw = os.environ.get(DIFF_MAX_CHARS_ENV, "").strip()
    if not raw:
        return DIFF_MAX_CHARS
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        print(
            f"[ai_reviewer] Invalid {DIFF_MAX_CHARS_ENV}={raw!r} (need a positive "
            f"integer); using default {DIFF_MAX_CHARS}",
            file=sys.stderr,
        )
        return DIFF_MAX_CHARS
    return value


class DeepSeekDegradedError(RuntimeError):
    """Raised when the DeepSeek backend reports the model function as DEGRADED."""


class DeepSeekContextLengthError(RuntimeError):
    """Raised when DeepSeek rejects the request (HTTP 400) because the input
    exceeds the model context window."""


_CONTEXT_LENGTH_RE = re.compile(
    r"context[ _-]?length|context window|maximum context|too many tokens|"
    r"exceeds? the (?:model'?s? )?(?:maximum|max|context)|"
    r"reduce the length of the messages",
    re.IGNORECASE,
)


class DeepSeekTimeoutError(RuntimeError):
    """Raised when the DeepSeek backend keeps timing out after all retries."""


DEEPSEEK_CONNECT_TIMEOUT = 15
DEEPSEEK_READ_TIMEOUT = 150
DEEPSEEK_MAX_ATTEMPTS = 3
DEEPSEEK_RETRY_BACKOFF_SECONDS = (10, 30)
# 429/500/502/503/504 are all treated as transient gateway/rate-limit noise
# and retried the same way as a client-side timeout, rather than hard-failing
# on the first bad response. A 5xx body that explicitly says DEGRADED is
# handled separately below (no retry — the model itself is reporting down).
DEEPSEEK_RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)


def _env(key: str) -> str:
    """Return env var value; raise with a masked error message on missing key."""
    val = os.environ.get(key, "")
    if not val:
        raise RuntimeError(f"Required environment variable '{key}' is not set or empty.")
    return val


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_get(path: str, token: str, *, accept: str = None) -> requests.Response:
    headers = _github_headers(token)
    if accept:
        headers["Accept"] = accept
    url = f"{GITHUB_API}{path}"
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp


def fetch_pr_metadata(owner: str, repo: str, pr_number: str, token: str) -> dict:
    resp = _github_get(f"/repos/{owner}/{repo}/pulls/{pr_number}", token)
    return resp.json()


def fetch_pr_files(owner: str, repo: str, pr_number: str, token: str) -> list:
    resp = _github_get(f"/repos/{owner}/{repo}/pulls/{pr_number}/files", token)
    return resp.json()


def fetch_pr_diff(owner: str, repo: str, pr_number: str, token: str) -> str:
    resp = _github_get(
        f"/repos/{owner}/{repo}/pulls/{pr_number}",
        token,
        accept="application/vnd.github.v3.diff",
    )
    return resp.text


def fetch_issue(owner: str, repo: str, issue_number: int, token: str) -> dict | None:
    try:
        resp = _github_get(f"/repos/{owner}/{repo}/issues/{issue_number}", token)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        # Log the error to stderr instead of silently swallowing it
        # This helps detect permission regressions or API issues
        error_detail = ""
        if hasattr(exc, "response") and exc.response is not None:
            body = exc.response.text[:2000]
            error_detail = f" — {exc.response.status_code}: {body}"
        print(
            f"[ai_reviewer] Failed to fetch issue #{issue_number}: {exc}{error_detail}",
            file=sys.stderr,
        )
        return None


# ---------------------------------------------------------------------------
# Linked-issues resolver
# ---------------------------------------------------------------------------

CLOSING_PATTERN = re.compile(
    r"(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE
)

# Non-closing references: deliberately-sequenced multi-PR issues (one of N,
# closing keyword reserved for the final PR) and design-input PRs legitimately
# link an issue without wanting GitHub to auto-close it on merge. #1001
# proposed "Part of #N" as a sanctioned pattern; a live reviewer run (seen on
# PR #1306) independently suggested "Refs #N" as its own remedy for a PR it
# had just blocked for "missing issue linkage" -- a PR that followed that
# advice would then fail a strict closing-keyword-only check. Both forms (and
# the common "Related to #N" / "See #N") are recognized here as valid, if
# non-closing, linkage.
# Matches the keyword plus a comma/"and"/"&"-separated list of #N tokens
# that follows it (e.g. "Refs #1264, #1266"), not just a single number --
# the live remedy a reviewer run suggested for PR #1306 was exactly that
# two-issue list.
REFERENCE_PATTERN = re.compile(
    r"(?:refs?|references?|related(?: to)?|part of|see(?: also)?)\s+"
    r"(#\d+(?:\s*(?:,|and|&)\s*#\d+)*)",
    re.IGNORECASE,
)
_HASH_NUMBER_RE = re.compile(r"#(\d+)")


def extract_linked_issue_numbers(text: str) -> list:
    if not text:
        return []
    return [int(m) for m in CLOSING_PATTERN.findall(text)]


def extract_referenced_issue_numbers(text: str) -> list:
    """Non-closing references (`Refs #N`, `Part of #N`, `Related to #N`,
    `See #N`, including a comma/"and"-separated list after one keyword) --
    valid issue linkage that does not auto-close on merge."""
    if not text:
        return []
    nums = []
    for clause in REFERENCE_PATTERN.findall(text):
        nums.extend(int(n) for n in _HASH_NUMBER_RE.findall(clause))
    return nums


# ---------------------------------------------------------------------------
# Acceptance-criteria extractor (#1243)
#
# The previous version matched ANY line containing the substring "acceptance
# criteria" (so a sentence merely mentioning the phrase could false-trigger
# it) and capped output at ac_lines[:10] -- counting every wrapped
# continuation line of a bulleted item as if it were its own top-level
# criterion. Issue #1230's real "## Acceptance criteria" section wraps most
# bullets across 2-4 physical lines, so the old cap exhausted after roughly
# the first 2-3 bullets and silently dropped the rest -- the exact failure
# #1243 reports. This version triggers only on a genuine heading line (ATX
# `#`..`######`, or a standalone bold `**Acceptance Criteria**` / `__..__`,
# with an optional trailing colon) instead of a substring match anywhere in
# the body, stops at the next genuine heading (same detection) instead of any
# line merely starting with `#`, keeps nested/indented bullets (handled via
# per-line .strip()), and replaces the line-count cap with a character budget
# (AC_TEXT_MAX_CHARS) that states explicitly when it truncated instead of
# dropping content with no trace.
# ---------------------------------------------------------------------------

_BOLD_OR_ITALIC_STRIP_RE = re.compile(r"^[*_]{1,2}|[*_]{1,2}$")
_ATX_HEADING_PREFIX_RE = re.compile(r"^#{1,6}\s*")
_ATX_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
_STANDALONE_BOLD_HEADING_RE = re.compile(r"^(?:\*\*[^*]+\*\*|__[^_]+__):?$")


def _heading_text(line: str) -> str:
    """Normalize a candidate heading line to bare text for comparison:
    strip ATX `#` markers, then surrounding bold/italic markers, then a
    trailing colon. Returns '' if the line isn't heading-shaped at all
    (callers compare the result against a target phrase)."""
    s = line.strip()
    s = _ATX_HEADING_PREFIX_RE.sub("", s, count=1) if s.startswith("#") else s
    s = _BOLD_OR_ITALIC_STRIP_RE.sub("", s)
    return s.strip().rstrip(":").strip()


def _is_ac_heading(line: str) -> bool:
    return _heading_text(line).lower() == "acceptance criteria"


def _is_markdown_heading(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if _ATX_HEADING_RE.match(s):
        return True
    if _STANDALONE_BOLD_HEADING_RE.match(s):
        return True
    return False


def extract_acceptance_criteria(issue_body: str) -> tuple:
    """Return (ac_text, was_truncated) for the first 'Acceptance criteria'
    section found in issue_body, recognizing `## Acceptance criteria`,
    `**Acceptance criteria**`, and `__Acceptance criteria__` (with or
    without a trailing colon) as the section heading. Nested/indented
    bullets and wrapped continuation lines are kept (not just top-level
    `-`/`*`/`[` items). Stops at the next heading of any of those same
    forms. ac_text is capped at AC_TEXT_MAX_CHARS; if none extracted,
    returns ("(none extracted)", False).
    """
    if not issue_body:
        return "(none extracted)", False

    collected = []
    in_ac = False
    for line in issue_body.splitlines():
        if not in_ac:
            if _is_ac_heading(line):
                in_ac = True
            continue
        if _is_markdown_heading(line):
            break
        stripped = line.strip()
        if stripped:
            collected.append(stripped)

    if not collected:
        return "(none extracted)", False

    text = "\n".join(collected)
    return _budget(text, AC_TEXT_MAX_CHARS)


# ---------------------------------------------------------------------------
# Graphify context
# ---------------------------------------------------------------------------

def load_graph(repo_root: Path) -> dict:
    graph_path = repo_root / "graphify-out" / "graph.json"
    if not graph_path.exists():
        return {}
    try:
        with graph_path.open() as fh:
            return json.load(fh)
    except Exception:
        return {}


def get_neighbors_for_file(graph: dict, file_path: str) -> list:
    """Return list of neighboring node labels related to file_path."""
    if not graph:
        return []

    nodes = graph.get("nodes", [])
    edges = graph.get("links", graph.get("edges", []))

    # Normalise path — drop leading ./ and convert \ to /
    norm = file_path.lstrip("./").replace("\\", "/")

    # Find node IDs whose source_file matches (partial suffix match)
    matched_ids = set()
    for node in nodes:
        src = node.get("source_file", "").lstrip("./").replace("\\", "/")
        if src == norm or src.endswith("/" + norm) or norm.endswith("/" + src):
            matched_ids.add(node["id"])

    if not matched_ids:
        return []

    # Build id→label map
    id_to_label = {n["id"]: n.get("label", n["id"]) for n in nodes}

    # Collect neighbor IDs from edges
    neighbor_ids = set()
    for edge in edges:
        src_id = edge.get("source")
        tgt_id = edge.get("target")
        if src_id in matched_ids and tgt_id not in matched_ids:
            neighbor_ids.add(tgt_id)
        elif tgt_id in matched_ids and src_id not in matched_ids:
            neighbor_ids.add(src_id)

    return [id_to_label.get(nid, nid) for nid in sorted(neighbor_ids)]


def build_graphify_context(graph: dict, changed_files: list) -> str:
    lines = []
    for f in changed_files:
        filename = f.get("filename", "")
        neighbors = get_neighbors_for_file(graph, filename)
        if neighbors:
            lines.append(f"**{filename}** → {', '.join(neighbors[:20])}")
        else:
            lines.append(f"**{filename}** → (no graph neighbors found)")
    return "\n".join(lines) if lines else "(no graphify data)"


# ---------------------------------------------------------------------------
# Policy summary from CLAUDE.md
# ---------------------------------------------------------------------------

POLICY_SECTIONS = [
    "CI must be green",
    "no direct pushes to master",
    "tests required",
    "no secrets",
    "trading safety",
    "Status is managed via the `Status` field",
    "Closes #N",
    "PR merge gate",
    "Secret handling",
]

def load_policy_summary(repo_root: Path) -> str:
    claude_md = repo_root / "CLAUDE.md"
    if not claude_md.exists():
        return "(CLAUDE.md not found)"

    lines = claude_md.read_text(errors="replace").splitlines()
    selected = []
    for line in lines:
        for keyword in POLICY_SECTIONS:
            if keyword.lower() in line.lower():
                selected.append(line.strip())
                break

    # Also collect agent files for governance context
    agents_dir = repo_root / "agents"
    agent_notes = []
    if agents_dir.is_dir():
        for agent_file in sorted(agents_dir.glob("*.md")):
            agent_notes.append(f"- {agent_file.name}")

    summary = "\n".join(selected[:30]) if selected else "(no matching policy lines found)"
    agents_txt = "\n".join(agent_notes) if agent_notes else "(none)"
    return f"Key policy rules:\n{summary}\n\nAgent files present:\n{agents_txt}"


# ---------------------------------------------------------------------------
# Build review packet
# ---------------------------------------------------------------------------

def build_diff_from_files(changed_files: list) -> str:
    """Assemble the review diff from each file's own patch hunk
    (`fetch_pr_files()`'s `patch` field) instead of truncating a single
    flat full-PR diff blob. Generated paths (`GENERATED_PATH_PREFIXES`) are
    skipped entirely so they can never crowd out real code, no matter how
    large `DIFF_MAX_CHARS` is (#1118). GitHub omits `patch` for binary files
    and for diffs too large for a single file entry -- those are silently
    skipped too, since there is no meaningful hunk to show.
    """
    parts = []
    for f in changed_files:
        filename = f.get("filename", "")
        if is_generated_path(filename):
            continue
        patch = f.get("patch")
        if not patch:
            continue
        parts.append(f"diff --git a/{filename} b/{filename}\n{patch}")
    return "\n".join(parts)


def build_review_packet(
    pr_meta: dict,
    changed_files: list,
    graph: dict,
    linked_issues: list,
    policy_summary: str,
    diff_max_chars: int = None,
) -> str:
    if diff_max_chars is None:
        diff_max_chars = get_diff_max_chars()
    # PR Metadata
    pr_number = pr_meta.get("number", "?")
    pr_title = pr_meta.get("title", "")
    author = pr_meta.get("user", {}).get("login", "unknown")
    base_branch = pr_meta.get("base", {}).get("ref", "?")
    head_branch = pr_meta.get("head", {}).get("ref", "?")

    pr_body = pr_meta.get("body") or ""
    closing_nums = extract_linked_issue_numbers(pr_body)
    # Non-closing references (`Refs #N`, `Part of #N`, `Related to #N`,
    # `See #N`) are valid linkage too -- a deliberately-sequenced multi-PR
    # issue defers the closing keyword to its final PR, and the reviewer's
    # own suggested remedy on a live BLOCKed run was "Refs #N" (#1243
    # follow-up, PR #1306/#1305 live evidence). Dedup: a number already
    # counted as closing is not repeated as referenced-only.
    reference_nums = [
        n for n in extract_referenced_issue_numbers(pr_body) if n not in closing_nums
    ]
    all_linked_nums = closing_nums + reference_nums
    closing_txt = ", ".join(f"#{n}" for n in closing_nums) if closing_nums else "(none)"
    reference_txt = ", ".join(f"#{n}" for n in reference_nums) if reference_nums else "(none)"

    packet_parts = [
        "## PR Metadata",
        f"- PR #{pr_number}: {pr_title}",
        f"- Author: {author}",
        f"- Base: {base_branch} ← {head_branch}",
        "- Issue linkage — DETERMINISTIC FACT, computed by regex over the PR "
        "body below. This is ground truth, not an assessment: do not "
        "independently re-derive, dispute, or second-guess it from the diff, "
        "the branch name, or your own reading of the PR body text.",
        f"    - Closing keywords (auto-closes on merge): {closing_txt}",
        f"    - Non-closing references (Refs/Part of/Related to/See — valid "
        f"linkage, does NOT auto-close): {reference_txt}",
        "    - A PR with at least one number in EITHER list above has "
        "satisfied the issue-linking requirement. Do not treat an empty "
        "closing-keywords list as 'no linked issue' when the references "
        "list is non-empty.",
        "",
        "## PR Description",
    ]
    # The PR body itself — previously discarded after regexing closing
    # keywords out of it, so the model never saw a word of the description
    # (#1243). An empty body is a genuine policy violation (governance rule
    # 2: every PR description must state what/why/how-to-test) and is
    # called out explicitly rather than left as a blank section.
    if not pr_body.strip():
        packet_parts.append(
            "(PR body is empty — this is a policy violation: PR descriptions "
            "must state what changed, why, and how to test, per governance "
            "rule 2. This is a legitimate basis to flag Acceptance criteria "
            "and CI integrity.)"
        )
    else:
        pr_body_text, pr_body_truncated = _budget(pr_body, PR_BODY_MAX_CHARS)
        packet_parts.append(pr_body_text)
        if pr_body_truncated:
            packet_parts.append(
                f"\n[... PR body truncated to {PR_BODY_MAX_CHARS} characters "
                "for the model's context budget. Content beyond this point "
                "was NOT shown to the reviewer — treat any criteria you "
                "cannot verify from this excerpt as unverifiable, not failed.]"
            )
    packet_parts += ["", "## Changed Files"]
    for f in changed_files:
        fname = f.get("filename", "?")
        status = f.get("status", "")
        additions = f.get("additions", 0)
        deletions = f.get("deletions", 0)
        suffix = " (generated, diff omitted)" if is_generated_path(fname) else ""
        packet_parts.append(f"- {fname} [{status}] +{additions}/-{deletions}{suffix}")

    # Diff — built from each file's own patch hunk, excluding generated
    # paths, then capped at diff_max_chars (default DIFF_MAX_CHARS, env
    # override AI_REVIEW_DIFF_MAX_CHARS) as a safety net (#1118, #1283).
    diff = build_diff_from_files(changed_files)
    truncated_diff = diff if len(diff) <= diff_max_chars else diff[:diff_max_chars] + "\n... (diff truncated)"
    packet_parts += [
        "",
        "## Diff",
        "```diff",
        truncated_diff,
        "```",
        "",
        "## Graphify Context",
        build_graphify_context(graph, changed_files),
    ]

    # Linked issues
    packet_parts += ["", "## Linked Issues"]
    if not all_linked_nums:
        packet_parts.append(
            "(no closing keywords or non-closing references in PR body — a "
            "genuine candidate for a policy violation, but judge from the PR "
            "Description above: a PR that is itself design input with no "
            "code change may legitimately need none. Do not override the "
            "deterministic linkage fact stated in PR Metadata.)"
        )
    else:
        # Surface EACH linked number that failed to fetch, not just the
        # all-or-nothing case — a PR linking multiple issues where only one
        # fetch fails used to show no explanation for the missing one
        # (#1243 follow-up: a single missing input must not silently read as
        # "acceptance criteria unmet" for an issue that was never actually
        # unreachable in full).
        fetched_nums = {issue.get("number") for issue in linked_issues}
        missing_nums = [n for n in all_linked_nums if n not in fetched_nums]
        if missing_nums:
            missing_txt = ", ".join(f"#{n}" for n in missing_nums)
            packet_parts.append(
                f"(issue(s) {missing_txt} unavailable — access restricted or "
                "fetch failed; linkage already confirmed in PR Metadata above. "
                "Do not block on acceptance criteria you cannot see for these "
                "— mark unverifiable, not unmet.)"
            )
    for issue in linked_issues:
        issue_num = issue.get("number", "?")
        issue_title = issue.get("title", "")
        issue_body = issue.get("body") or ""
        linkage_type = "closing" if issue_num in closing_nums else "referenced, non-closing"

        ac_text, ac_truncated = extract_acceptance_criteria(issue_body)
        if ac_truncated:
            ac_text += (
                f"\n[... acceptance criteria truncated to {AC_TEXT_MAX_CHARS} "
                "characters; criteria beyond this point are unverifiable, "
                "not unmet.]"
            )

        if not issue_body.strip():
            body_block = "(issue body is empty)"
        else:
            body_text, body_truncated = _budget(issue_body, ISSUE_BODY_MAX_CHARS)
            body_block = body_text
            if body_truncated:
                body_block += (
                    f"\n[... issue body truncated to {ISSUE_BODY_MAX_CHARS} "
                    "characters for the model's context budget. Treat "
                    "criteria beyond this point as unverifiable, not unmet.]"
                )

        packet_parts += [
            f"### Issue #{issue_num} ({linkage_type}): {issue_title}",
            f"Body:\n{body_block}",
            f"Acceptance Criteria:\n{ac_text}",
            "",
        ]
    packet_parts += ["", "## Policy Summary", policy_summary]

    return "\n".join(packet_parts)


# ---------------------------------------------------------------------------
# DeepSeek call
# ---------------------------------------------------------------------------

def call_deepseek(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_content: str,
) -> str:
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 2000,
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, DEEPSEEK_MAX_ATTEMPTS + 1):
        is_last_attempt = attempt == DEEPSEEK_MAX_ATTEMPTS

        try:
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=(DEEPSEEK_CONNECT_TIMEOUT, DEEPSEEK_READ_TIMEOUT),
            )
        except requests.exceptions.Timeout as exc:
            if is_last_attempt:
                raise DeepSeekTimeoutError(
                    f"DeepSeek model {model} timed out after {DEEPSEEK_MAX_ATTEMPTS} attempts: {exc}"
                ) from exc
            _wait_before_retry(attempt, "timed out")
            continue

        if resp.ok:
            data = resp.json()
            return data["choices"][0]["message"]["content"]

        body = resp.text[:2000]
        # Surface degraded/unavailable backend as a distinct exception so
        # callers can fail closed with a specific reason instead of a bare
        # RuntimeError.
        if resp.status_code in (400, 503) and "DEGRADED" in body:
            raise DeepSeekDegradedError(f"DeepSeek model {model} is DEGRADED: {body}")

        if resp.status_code == 400 and _CONTEXT_LENGTH_RE.search(body):
            raise DeepSeekContextLengthError(
                f"DeepSeek model {model} rejected the input as exceeding the model "
                f"context: {body}"
            )

        if resp.status_code in DEEPSEEK_RETRYABLE_STATUS_CODES:
            if is_last_attempt:
                raise DeepSeekTimeoutError(
                    f"DeepSeek model {model} kept returning HTTP {resp.status_code} "
                    f"after {DEEPSEEK_MAX_ATTEMPTS} attempts: {body}"
                )
            _wait_before_retry(attempt, f"returned HTTP {resp.status_code}")
            continue

        raise RuntimeError(f"DeepSeek API {resp.status_code} {resp.reason}: {body}")


def _wait_before_retry(attempt: int, reason: str) -> None:
    backoff = DEEPSEEK_RETRY_BACKOFF_SECONDS[
        min(attempt - 1, len(DEEPSEEK_RETRY_BACKOFF_SECONDS) - 1)
    ]
    print(
        f"[ai_reviewer] DeepSeek call {reason} (attempt {attempt}/{DEEPSEEK_MAX_ATTEMPTS}), "
        f"retrying in {backoff}s...",
        file=sys.stderr,
    )
    time.sleep(backoff)


def parse_verdict(response_text: str) -> str:
    """Return 'PASS' or 'BLOCK' from the DeepSeek response."""
    upper = response_text.upper()
    if "VERDICT: PASS" in upper:
        return "PASS"
    if "VERDICT: BLOCK" in upper:
        return "BLOCK"
    # Default to BLOCK if verdict is ambiguous
    return "BLOCK"


# ---------------------------------------------------------------------------
# GitHub Check Run + PR comment
# ---------------------------------------------------------------------------

def create_check_run(
    owner: str,
    repo: str,
    head_sha: str,
    token: str,
    verdict: str,
    review_text: str,
) -> dict:
    conclusion = "success" if verdict == "PASS" else "failure"
    title = f"AI Review: {verdict}"
    summary = review_text[:SUMMARY_MAX_CHARS]

    payload = {
        "name": "AI / DeepSeek review",
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {
            "title": title,
            "summary": summary,
        },
    }
    url = f"{GITHUB_API}/repos/{owner}/{repo}/check-runs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def post_pr_comment(
    owner: str,
    repo: str,
    pr_number: str,
    token: str,
    review_text: str,
) -> dict:
    body = f"## AI Code Review (DeepSeek)\n\n{review_text}"
    payload = {"body": body}
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def create_failure_check_run(
    owner: str,
    repo: str,
    head_sha: str,
    token: str,
    error_message: str,
) -> None:
    """Best-effort: create a failing check run with the error message."""
    payload = {
        "name": "AI / DeepSeek review",
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "failure",
        "output": {
            "title": "AI Review: ERROR",
            "summary": error_message[:SUMMARY_MAX_CHARS],
        },
    }
    url = f"{GITHUB_API}/repos/{owner}/{repo}/check-runs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        requests.post(url, json=payload, headers=headers, timeout=30)
    except Exception:
        pass  # Swallow — we are already in an error path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Resolve env vars — fail fast with a clear message
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        # Do not print values; only names
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    api_key = _env("DEEPSEEK_API_KEY")
    deepseek_base_url = os.environ.get("DEEPSEEK_BASE_URL") or DEEPSEEK_DEFAULT_BASE_URL
    deepseek_model = os.environ.get("DEEPSEEK_MODEL") or DEEPSEEK_DEFAULT_MODEL
    github_token = _env("GITHUB_TOKEN")
    pr_number = _env("PR_NUMBER")
    pr_head_sha = _env("PR_HEAD_SHA")
    repo_owner = _env("REPO_OWNER")
    repo_name = _env("REPO_NAME")

    repo_root = Path(__file__).resolve().parent.parent

    # --- Error-safe wrapper: report failures via Check Run ---
    try:
        _run(
            api_key=api_key,
            deepseek_base_url=deepseek_base_url,
            deepseek_model=deepseek_model,
            github_token=github_token,
            pr_number=pr_number,
            pr_head_sha=pr_head_sha,
            repo_owner=repo_owner,
            repo_name=repo_name,
            repo_root=repo_root,
        )
    except Exception as exc:
        # Mask API key from error output
        error_msg = str(exc)
        safe_error = f"AI reviewer failed: {error_msg}"
        print(safe_error, file=sys.stderr)
        create_failure_check_run(
            owner=repo_owner,
            repo=repo_name,
            head_sha=pr_head_sha,
            token=github_token,
            error_message=safe_error,
        )
        sys.exit(1)


def _run(
    *,
    api_key: str,
    deepseek_base_url: str,
    deepseek_model: str,
    github_token: str,
    pr_number: str,
    pr_head_sha: str,
    repo_owner: str,
    repo_name: str,
    repo_root: Path,
) -> None:
    print(f"[ai_reviewer] Reviewing PR #{pr_number} in {repo_owner}/{repo_name}")

    # 1. Fetch PR data
    print("[ai_reviewer] Fetching PR metadata...")
    pr_meta = fetch_pr_metadata(repo_owner, repo_name, pr_number, github_token)

    print("[ai_reviewer] Fetching changed files...")
    changed_files = fetch_pr_files(repo_owner, repo_name, pr_number, github_token)
    # No separate fetch_pr_diff() call: build_review_packet() assembles the
    # diff from each file's own `patch` field (already present on the
    # changed_files entries above), filtered by GENERATED_PATH_PREFIXES,
    # instead of truncating one flat full-PR diff blob (#1118).

    # 2. Resolve linked issues — closing keywords AND non-closing references
    # (`Refs #N`, `Part of #N`) both count as valid linkage (#1243 follow-up:
    # a deliberately-sequenced multi-PR issue defers its closing keyword to
    # the final PR, and must not be fetched-and-treated as unlinked).
    pr_body = pr_meta.get("body") or ""
    closing_nums = extract_linked_issue_numbers(pr_body)
    reference_nums = [
        n for n in extract_referenced_issue_numbers(pr_body) if n not in closing_nums
    ]
    linked_nums = closing_nums + reference_nums
    print(
        f"[ai_reviewer] Found linked issue numbers: closing={closing_nums} "
        f"referenced={reference_nums}"
    )

    linked_issues = []
    for num in linked_nums:
        issue = fetch_issue(repo_owner, repo_name, num, github_token)
        if issue is not None:
            linked_issues.append(issue)

    # 3. Graphify context
    print("[ai_reviewer] Loading graph context...")
    graph = load_graph(repo_root)

    # 4. Policy summary
    policy_summary = load_policy_summary(repo_root)

    # 5. Build review packet
    review_packet = build_review_packet(
        pr_meta=pr_meta,
        changed_files=changed_files,
        graph=graph,
        linked_issues=linked_issues,
        policy_summary=policy_summary,
    )

    # 6. Load system prompt
    system_prompt_path = repo_root / "agents" / "reviewer_prompt_deepseek.md"
    if not system_prompt_path.exists():
        raise FileNotFoundError(f"System prompt not found: {system_prompt_path}")
    system_prompt = system_prompt_path.read_text()

    # 7. Call DeepSeek
    print(f"[ai_reviewer] Calling DeepSeek model {deepseek_model}...")
    try:
        try:
            review_text = call_deepseek(
                base_url=deepseek_base_url,
                api_key=api_key,
                model=deepseek_model,
                system_prompt=system_prompt,
                user_content=review_packet,
            )
        except DeepSeekContextLengthError as exc:
            # Input exceeded the model context: retry exactly ONCE with the
            # previous, smaller cap (#1283). A second failure propagates.
            print(
                f"[ai_reviewer] Input exceeded model context ({exc}); retrying once "
                f"with diff cap {DIFF_FALLBACK_MAX_CHARS}",
                file=sys.stderr,
            )
            review_packet = build_review_packet(
                pr_meta=pr_meta,
                changed_files=changed_files,
                graph=graph,
                linked_issues=linked_issues,
                policy_summary=policy_summary,
                diff_max_chars=DIFF_FALLBACK_MAX_CHARS,
            )
            review_text = call_deepseek(
                base_url=deepseek_base_url,
                api_key=api_key,
                model=deepseek_model,
                system_prompt=system_prompt,
                user_content=review_packet,
            )
            review_text += (
                f"\n\n> Note: the diff was truncated to {DIFF_FALLBACK_MAX_CHARS} "
                f"characters to fit the model context."
            )
    except DeepSeekDegradedError as exc:
        # DeepSeek backend is reporting itself degraded — fail closed rather
        # than pass automatically. A required review that could not run must
        # not report success (#1037).
        print(f"[ai_reviewer] DeepSeek degraded, review did NOT run: {exc}")
        skip_msg = (
            f"❌ **The AI review did not run.** DeepSeek model `{deepseek_model}` is "
            f"reported DEGRADED.\n\n"
            f"**This is not a verdict.** No code was reviewed. The check "
            f"fails so the merge gate stays honest.\n\n"
            f"If the degradation is genuinely transient, re-run this check. "
            f"If it persists, the model is not usable and must be replaced.\n\n"
            f"Error detail: {exc}"
        )
        create_check_run(
            owner=repo_owner,
            repo=repo_name,
            head_sha=pr_head_sha,
            token=github_token,
            verdict="UNAVAILABLE",
            review_text=skip_msg,
        )
        print("[ai_reviewer] Done — verdict=UNAVAILABLE (DeepSeek degraded)")
        sys.exit(1)
    except DeepSeekTimeoutError as exc:
        # DeepSeek kept timing out or returning a gateway/rate-limit error
        # despite retries — fail closed, same reasoning as above.
        print(f"[ai_reviewer] DeepSeek failed repeatedly, review did NOT run: {exc}")
        skip_msg = (
            f"❌ **The AI review did not run.** DeepSeek model `{deepseek_model}` did "
            f"not return a successful response after {DEEPSEEK_MAX_ATTEMPTS} "
            f"attempts.\n\n"
            f"**This is not a verdict.** No code was reviewed. The check "
            f"fails so the merge gate stays honest.\n\n"
            f"Error detail: {exc}"
        )
        create_check_run(
            owner=repo_owner,
            repo=repo_name,
            head_sha=pr_head_sha,
            token=github_token,
            verdict="UNAVAILABLE",
            review_text=skip_msg,
        )
        print("[ai_reviewer] Done — verdict=UNAVAILABLE (DeepSeek timeout)")
        sys.exit(1)

    # 8. Parse verdict
    verdict = parse_verdict(review_text)
    print(f"[ai_reviewer] Verdict: {verdict}")

    # 9. Create GitHub Check Run
    print("[ai_reviewer] Creating GitHub Check Run...")
    check_run = create_check_run(
        owner=repo_owner,
        repo=repo_name,
        head_sha=pr_head_sha,
        token=github_token,
        verdict=verdict,
        review_text=review_text,
    )
    print(f"[ai_reviewer] Check run created: {check_run.get('id')}")

    # 10. Post PR comment
    print("[ai_reviewer] Posting PR comment...")
    comment = post_pr_comment(
        owner=repo_owner,
        repo=repo_name,
        pr_number=pr_number,
        token=github_token,
        review_text=review_text,
    )
    print(f"[ai_reviewer] PR comment posted: {comment.get('id')}")

    print(f"[ai_reviewer] Done — verdict={verdict}")


if __name__ == "__main__":
    main()
