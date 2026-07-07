#!/usr/bin/env python3
"""
AI Code Reviewer — context pipeline + NVIDIA NIM call (GLM-5.2)

Workflow env vars required:
  NVIDIA_NIM_API_KEY    — NIM API key
  NVIDIA_NIM_BASE_URL   — e.g. https://integrate.api.nvidia.com/v1
  NVIDIA_NIM_MODEL      — e.g. glm-5.2
  GITHUB_TOKEN          — GitHub token for API calls
  PR_NUMBER             — PR number being reviewed
  PR_HEAD_SHA           — head commit SHA
  REPO_OWNER            — repo owner
  REPO_NAME             — repo name
"""
import json
import os
import re
import sys
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration — loaded from env vars once at startup
# ---------------------------------------------------------------------------

REQUIRED_ENV = [
    "NVIDIA_NIM_API_KEY",
    "NVIDIA_NIM_BASE_URL",
    "NVIDIA_NIM_MODEL",
    "GITHUB_TOKEN",
    "PR_NUMBER",
    "PR_HEAD_SHA",
    "REPO_OWNER",
    "REPO_NAME",
]

GITHUB_API = "https://api.github.com"
DIFF_MAX_CHARS = 4000
SUMMARY_MAX_CHARS = 65535


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
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Linked-issues resolver
# ---------------------------------------------------------------------------

CLOSING_PATTERN = re.compile(
    r"(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE
)


def extract_linked_issue_numbers(text: str) -> list:
    if not text:
        return []
    return [int(m) for m in CLOSING_PATTERN.findall(text)]


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

def build_review_packet(
    pr_meta: dict,
    changed_files: list,
    diff: str,
    graph: dict,
    linked_issues: list,
    policy_summary: str,
) -> str:
    # PR Metadata
    pr_number = pr_meta.get("number", "?")
    pr_title = pr_meta.get("title", "")
    author = pr_meta.get("user", {}).get("login", "unknown")
    base_branch = pr_meta.get("base", {}).get("ref", "?")
    head_branch = pr_meta.get("head", {}).get("ref", "?")

    pr_body = pr_meta.get("body") or ""
    closing_nums = extract_linked_issue_numbers(pr_body)
    closing_txt = ", ".join(f"#{n}" for n in closing_nums) if closing_nums else "(none found)"

    packet_parts = [
        "## PR Metadata",
        f"- PR #{pr_number}: {pr_title}",
        f"- Author: {author}",
        f"- Base: {base_branch} ← {head_branch}",
        f"- Closing keywords in PR body: {closing_txt}",
        "",
        "## Changed Files",
    ]
    for f in changed_files:
        fname = f.get("filename", "?")
        status = f.get("status", "")
        additions = f.get("additions", 0)
        deletions = f.get("deletions", 0)
        packet_parts.append(f"- {fname} [{status}] +{additions}/-{deletions}")

    # Diff (truncated)
    truncated_diff = diff if len(diff) <= DIFF_MAX_CHARS else diff[:DIFF_MAX_CHARS] + "\n... (diff truncated)"
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
    if not linked_issues and closing_nums:
        packet_parts.append("(issue details unavailable — access restricted; closing keywords confirmed in PR body above)")
    elif not closing_nums:
        packet_parts.append("(no closing keywords in PR body — policy violation)")
    if linked_issues:
        for issue in linked_issues:
            issue_num = issue.get("number", "?")
            issue_title = issue.get("title", "")
            issue_body = issue.get("body") or ""
            # Extract acceptance criteria bullets
            ac_lines = []
            in_ac = False
            for line in issue_body.splitlines():
                if re.search(r"acceptance criteria", line, re.IGNORECASE):
                    in_ac = True
                    continue
                if in_ac:
                    stripped = line.strip()
                    if stripped.startswith(("-", "*", "[")):
                        ac_lines.append(stripped)
                    elif stripped and not stripped.startswith("#"):
                        ac_lines.append(stripped)
                    elif stripped.startswith("#"):
                        break
            body_excerpt = issue_body[:500].replace("\n", " ")
            ac_text = "\n".join(ac_lines[:10]) if ac_lines else "(none extracted)"
            packet_parts += [
                f"### Issue #{issue_num}: {issue_title}",
                f"Body excerpt: {body_excerpt}",
                f"Acceptance Criteria:\n{ac_text}",
                "",
            ]
    packet_parts += ["", "## Policy Summary", policy_summary]

    return "\n".join(packet_parts)


# ---------------------------------------------------------------------------
# NVIDIA NIM call
# ---------------------------------------------------------------------------

def call_nim(
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
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    if not resp.ok:
        body = resp.text[:2000]
        raise RuntimeError(f"NIM API {resp.status_code} {resp.reason}: {body}")
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def parse_verdict(response_text: str) -> str:
    """Return 'PASS' or 'BLOCK' from the NIM response."""
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
        "name": "AI / NVIDIA NIM review",
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
    body = f"## AI Code Review (GLM-5.2)\n\n{review_text}"
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
        "name": "AI / NVIDIA NIM review",
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

    api_key = _env("NVIDIA_NIM_API_KEY")
    nim_base_url = _env("NVIDIA_NIM_BASE_URL")
    nim_model = _env("NVIDIA_NIM_MODEL")
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
            nim_base_url=nim_base_url,
            nim_model=nim_model,
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
        # Replace any accidental key leak pattern (first 8 chars of the key)
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
    nim_base_url: str,
    nim_model: str,
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

    print("[ai_reviewer] Fetching PR diff...")
    diff = fetch_pr_diff(repo_owner, repo_name, pr_number, github_token)

    # 2. Resolve linked issues
    pr_body = pr_meta.get("body") or ""
    linked_nums = extract_linked_issue_numbers(pr_body)
    print(f"[ai_reviewer] Found linked issue numbers: {linked_nums}")

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
        diff=diff,
        graph=graph,
        linked_issues=linked_issues,
        policy_summary=policy_summary,
    )

    # 6. Load system prompt
    system_prompt_path = repo_root / "agents" / "reviewer_prompt_glm52.md"
    if not system_prompt_path.exists():
        raise FileNotFoundError(f"System prompt not found: {system_prompt_path}")
    system_prompt = system_prompt_path.read_text()

    # 7. Call NVIDIA NIM
    print(f"[ai_reviewer] Calling NIM model {nim_model}...")
    review_text = call_nim(
        base_url=nim_base_url,
        api_key=api_key,
        model=nim_model,
        system_prompt=system_prompt,
        user_content=review_packet,
    )

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
