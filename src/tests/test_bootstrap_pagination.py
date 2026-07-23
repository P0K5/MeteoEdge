"""Regression test for scripts/bootstrap_session.sh cursor pagination (issue #793).

The board (P0K5/MeteoEdge project #4) has 429+ items, but the original items
query used ``items(first: 100)`` with no ``pageInfo``/``after`` cursor loop, so
every issue past the first API page silently never got an ``ITEM_ID_ISSUE_``
entry -- no matter how many times the script was re-run.

This test drives the real script against a mock ``gh`` that serves a
multi-page board (250 items across 3 pages, including a high-numbered issue
that only appears on the last page) and asserts that *every* item is written,
which is only possible if the cursor loop walks all pages.

No network / no real ``gh`` is used.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap_session.sh"

# 250 issue-backed items spread over 3 pages of 100/100/50. Issue #781 lives on
# the final page on purpose: it is the exact class of high-numbered issue #793
# reported as missing, and asserting it appears proves the last page was walked.
PAGE1_NUMBERS = list(range(1, 101))            # 1..100
PAGE2_NUMBERS = list(range(101, 201))          # 101..200
PAGE3_NUMBERS = list(range(201, 250)) + [781]  # 201..249 + 781  (50 items)
ALL_NUMBERS = PAGE1_NUMBERS + PAGE2_NUMBERS + PAGE3_NUMBERS  # 250 unique

MOCK_GH = r'''#!/usr/bin/env python3
"""Minimal `gh api graphql` stand-in for the bootstrap pagination test."""
import json
import sys


def parse_f_args(argv):
    out = {}
    i = 0
    while i < len(argv):
        if argv[i] == "-f" and i + 1 < len(argv):
            key, _, val = argv[i + 1].partition("=")
            out[key] = val
            i += 2
        else:
            i += 1
    return out


def issue_node(n):
    return {"id": f"PVTI_item_{n}", "content": {"number": n, "title": f"Issue {n}"}}


def main():
    argv = sys.argv[1:]
    fargs = parse_f_args(argv)
    query = fargs.get("query", "")

    if "projectsV2" in query:
        print(json.dumps({"data": {"repository": {"projectsV2": {
            "nodes": [{"id": "PVT_TESTPROJECT", "title": "Test", "number": 4}]}}}}))
        return

    if "fields(first" in query:
        print(json.dumps({"data": {"node": {"fields": {"nodes": [
            {},  # non-single-select field: no 'name' -> parser skips it
            {"id": "FIELD_STATUS", "name": "Status", "options": [
                {"id": "o_backlog", "name": "Backlog"},
                {"id": "o_ready", "name": "Ready"},
                {"id": "o_inprog", "name": "In progress"},
                {"id": "o_inrev", "name": "In review"},
                {"id": "o_done", "name": "Done"},
            ]},
        ]}}}}))
        return

    if "items(first" in query:
        cursor = fargs.get("cursor", "")
        if not cursor:
            numbers, has_next, end = PAGE1, True, "CURSOR_1"
        elif cursor == "CURSOR_1":
            numbers, has_next, end = PAGE2, True, "CURSOR_2"
        elif cursor == "CURSOR_2":
            numbers, has_next, end = PAGE3, False, None
        else:
            raise SystemExit(f"mock gh: unexpected cursor {cursor!r}")
        nodes = [issue_node(n) for n in numbers]
        if not cursor:
            # a draft/non-issue board item on page 1: content has no 'number'
            nodes.append({"id": "PVTI_draft", "content": {}})
        print(json.dumps({"data": {"node": {"items": {
            "pageInfo": {"hasNextPage": has_next, "endCursor": end},
            "nodes": nodes,
        }}}}))
        return

    raise SystemExit(f"mock gh: unrecognized query:\n{query}")


if __name__ == "__main__":
    main()
'''


def _write_mock_gh(bin_dir: Path) -> Path:
    src = MOCK_GH.replace("PAGE1", repr(PAGE1_NUMBERS)) \
                 .replace("PAGE2", repr(PAGE2_NUMBERS)) \
                 .replace("PAGE3", repr(PAGE3_NUMBERS))
    gh = bin_dir / "gh"
    gh.write_text(src)
    gh.chmod(0o755)
    return gh


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_bootstrap_walks_all_board_pages(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mock_gh = _write_mock_gh(bin_dir)

    workdir = tmp_path / "work"
    workdir.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}" + env.get("PATH", "")
    env["GH_BIN"] = str(mock_gh)

    result = subprocess.run(
        ["bash", str(BOOTSTRAP)],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"bootstrap failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    out_file = workdir / ".claude" / "session-context.env"
    assert out_file.exists(), "session-context.env was not written"
    text = out_file.read_text()

    written = {
        int(line.split("_")[3].split("=")[0])
        for line in text.splitlines()
        if line.startswith("ITEM_ID_ISSUE_")
    }

    # Every board item across all 3 pages must be present -- the crux of #793.
    assert written == set(ALL_NUMBERS), (
        f"expected {len(ALL_NUMBERS)} items, got {len(written)}; "
        f"missing={sorted(set(ALL_NUMBERS) - written)}"
    )
    # More than one page worth: proves the loop did not stop after page 1.
    assert len(written) == 250
    # The high-numbered issue that only exists on the final page.
    assert "ITEM_ID_ISSUE_781=PVTI_item_781" in text
    # Draft/non-issue board items are skipped.
    assert "PVTI_draft" not in text
    # Static IDs still resolve (project + status field) alongside pagination.
    assert "GITHUB_PROJECT_ID=PVT_TESTPROJECT" in text
    assert "STATUS_OPT_IN_PROGRESS=o_inprog" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
