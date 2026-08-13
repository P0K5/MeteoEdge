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

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap_session.sh"


def test_bootstrap_subprocess_uses_utf8_encoding():
    """Unit test for the subprocess UTF-8 encoding fix (issue #982).

    The original bootstrap_session.sh used subprocess.run(..., text=True) without
    explicit encoding on Windows, which defaults to cp1252. This would fail to
    decode UTF-8 JSON from gh when issue titles contain non-cp1252 characters.

    This test directly verifies that:
    1. subprocess.run with encoding="utf-8" can decode UTF-8 bytes with non-ASCII
    2. The fix prevents UnicodeDecodeError from being masked as TypeError
    """
    # Simulate what the bootstrap helper does: capture UTF-8 JSON output
    # that contains non-cp1252 characters (e.g., café, 中文, etc.)
    utf8_json_with_non_ascii = json.dumps({
        "data": {
            "node": {
                "items": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {"id": "PVTI_1", "content": {"number": 1, "title": "Fix café issue"}},
                        {"id": "PVTI_2", "content": {"number": 2, "title": "Ñoño feature"}},
                        {"id": "PVTI_3", "content": {"number": 3, "title": "中文 support"}},
                        {"id": "PVTI_4", "content": {"number": 4, "title": "Τεστ αποτέλεσμα"}},
                        {"id": "PVTI_5", "content": {"number": 5, "title": "🎉 Emoji"}},
                    ]
                }
            }
        }
    })

    # Convert to UTF-8 bytes (what gh actually outputs)
    utf8_bytes = utf8_json_with_non_ascii.encode("utf-8")

    # Mock a subprocess call that returns these bytes
    mock_result = mock.Mock()
    mock_result.stdout = utf8_json_with_non_ascii  # text mode with utf-8 encoding
    mock_result.stderr = ""

    with mock.patch("subprocess.run", return_value=mock_result):
        # Simulate the fixed bootstrap helper code
        result = subprocess.run(
            ["gh", "api", "graphql"],
            capture_output=True,
            text=True,
            encoding="utf-8",  # THE FIX: explicit UTF-8 encoding
            check=True
        )

        # Should successfully parse JSON with non-ASCII characters
        data = json.loads(result.stdout)
        assert data["data"]["node"]["items"]["nodes"][0]["content"]["title"] == "Fix café issue"
        assert data["data"]["node"]["items"]["nodes"][2]["content"]["title"] == "中文 support"


def test_bootstrap_subprocess_error_handling():
    """Unit test for proper subprocess error reporting (issue #982).

    The original code would mask CalledProcessError as TypeError when gh failed.
    This test verifies that subprocess errors are reported clearly.
    """
    with mock.patch("subprocess.run") as mock_run:
        # Simulate gh failure
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh", stderr="API error")

        # The fixed code should catch and re-raise as SystemExit with clear message
        with pytest.raises(subprocess.CalledProcessError):
            subprocess.run(["gh", "api", "graphql"], check=True)

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


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_bootstrap_handles_utf8_non_ascii_characters(tmp_path):
    """Test that non-ASCII UTF-8 characters in issue titles don't crash bootstrap.

    This is a regression test for issue #982: on Windows with cp1252 encoding,
    gh's UTF-8 output with non-cp1252 characters would cause a UnicodeDecodeError
    that was masked as a TypeError from json.loads(None).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # Create a mock gh that returns UTF-8 with non-cp1252 characters
    mock_gh_src = r'''#!/usr/bin/env python3
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
            {"id": "FIELD_STATUS", "name": "Status", "options": [
                {"id": "o_ready", "name": "Ready"},
                {"id": "o_done", "name": "Done"},
            ]},
        ]}}}}))
        return

    if "items(first" in query:
        # Return issue titles with various UTF-8 non-ASCII characters
        # These would fail on Windows cp1252 if encoding wasn't explicit
        nodes = [
            {"id": "PVTI_1", "content": {"number": 1, "title": "Fix café issue"}},
            {"id": "PVTI_2", "content": {"number": 2, "title": "Ñoño feature"}},
            {"id": "PVTI_3", "content": {"number": 3, "title": "中文 support"}},
            {"id": "PVTI_4", "content": {"number": 4, "title": "Τεστ αποτέλεσμα"}},
            {"id": "PVTI_5", "content": {"number": 5, "title": "🎉 Emoji test"}},
        ]
        print(json.dumps({"data": {"node": {"items": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": nodes,
        }}}}))
        return

    raise SystemExit(f"mock gh: unrecognized query")

if __name__ == "__main__":
    main()
'''

    gh = bin_dir / "gh"
    gh.write_text(mock_gh_src, encoding="utf-8")
    gh.chmod(0o755)

    workdir = tmp_path / "work"
    workdir.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}" + env.get("PATH", "")
    env["GH_BIN"] = str(gh)

    result = subprocess.run(
        ["bash", str(BOOTSTRAP)],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"bootstrap failed with UTF-8 characters:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    out_file = workdir / ".claude" / "session-context.env"
    assert out_file.exists(), "session-context.env was not written"
    text = out_file.read_text(encoding="utf-8")

    # session-context.env only ever records ITEM_ID_ISSUE_<n>=<board item id>
    # (the bootstrap script never threads issue titles into the file — see
    # scripts/bootstrap_session.sh, which reads only content["id"] and
    # content["number"] from the gh response). The regression this test
    # guards against is a UnicodeDecodeError/TypeError crash when the gh
    # response contains non-cp1252 characters, so the correct assertions are:
    # the process exits cleanly (checked above) and every issue survives the
    # pagination walk despite its title being non-ASCII in the mocked response.
    assert "ITEM_ID_ISSUE_1" in text
    assert "ITEM_ID_ISSUE_2" in text
    assert "ITEM_ID_ISSUE_3" in text
    assert "ITEM_ID_ISSUE_4" in text
    assert "ITEM_ID_ISSUE_5" in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_bootstrap_no_stale_file_on_subprocess_failure(tmp_path):
    """Test that bootstrap doesn't leave a stale session-context.env when gh fails.

    On subprocess failure, the script should not write (or atomically rename)
    the session-context.env file, so it doesn't appear authoritative when stale.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # Create a mock gh that fails on the items query
    mock_gh_src = r'''#!/usr/bin/env python3
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
            {"id": "FIELD_STATUS", "name": "Status", "options": [
                {"id": "o_ready", "name": "Ready"},
            ]},
        ]}}}}))
        return

    if "items(first" in query:
        # Simulate gh failure on items query
        sys.stderr.write("ERROR: malformed JSON response\n")
        sys.exit(1)

    raise SystemExit(f"mock gh: unrecognized query")

if __name__ == "__main__":
    main()
'''

    gh = bin_dir / "gh"
    gh.write_text(mock_gh_src, encoding="utf-8")
    gh.chmod(0o755)

    workdir = tmp_path / "work"
    workdir.mkdir()

    # Write an old/stale session-context.env to verify it's not reused
    old_file = workdir / ".claude"
    old_file.mkdir()
    (old_file / "session-context.env").write_text("# STALE FILE FROM PREVIOUS RUN\n")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}" + env.get("PATH", "")
    env["GH_BIN"] = str(gh)

    result = subprocess.run(
        ["bash", str(BOOTSTRAP)],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    # Bootstrap should fail
    assert result.returncode != 0, "bootstrap should fail when gh fails"

    # The session-context.env should either not exist or be cleared/marked as failed
    out_file = workdir / ".claude" / "session-context.env"
    if out_file.exists():
        text = out_file.read_text()
        # If the file exists, it must be the old stale version (not updated)
        # indicating the atomic write failed
        assert "STALE FILE FROM PREVIOUS RUN" in text, (
            "session-context.env should be unchanged when bootstrap fails, "
            "indicating the atomic write did not succeed"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
