"""Tests for scripts/check_pr_body.py (issue #791). Offline."""
import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_pr_body.py"
_spec = importlib.util.spec_from_file_location("check_pr_body", SCRIPT)
cpb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpb)

DEPLOY_OK = "## How to deploy\n- [x] **No deploy needed** (docs only)\n"
TEMPLATE_DEPLOY = (
    "## How to deploy\n<!-- state what to do -->\n"
    "- [ ] **Code only** - restart\n- [ ] **No deploy needed**\n")


def run(body, **kw):
    return cpb.check(body, **kw)


def test_ok_ticked_box():
    assert run("Closes #5\n\n" + DEPLOY_OK, branch="dev/5-x") == []


def test_ok_plain_no_deploy_text():
    body = "Closes #5\n## How to deploy\nNo deploy needed (CI only)\n"
    assert run(body, branch="x/5") == []


def test_missing_section_fails():
    errs = run("Closes #5\n## What changed\nx\n", branch="x/5")
    assert any("missing" in e for e in errs)


def test_untouched_template_section_fails():
    errs = run("Closes #5\n" + TEMPLATE_DEPLOY, branch="x/5")
    assert any("empty" in e for e in errs)


def test_section_ends_at_next_heading():
    body = "Closes #5\n## How to deploy\n\n## Checklist\n- [x] done\n"
    assert any("empty" in e for e in run(body, branch="x/5"))


def test_empty_body_fails():
    assert run("", branch="x/5")
    assert run("   \n", branch="x/5")


def test_mismatch_names_issue():
    errs = run("Closes #944\n" + DEPLOY_OK, branch="dev/913-foo",
               title="fix(#913): foo")
    assert len(errs) == 1 and "#944" in errs[0]


def test_match_via_title_or_commit():
    b = "Fixes #7\n" + DEPLOY_OK
    assert run(b, branch="zzz", title="fix(#7): a") == []
    assert run(b, branch="zzz", title="a", commits="wip\n\nrefs #7") == []


def test_number_is_not_substring_matched():
    errs = run("Closes #94\n" + DEPLOY_OK, branch="dev/944-x")
    assert errs and "#94" in errs[0]


def test_non_closing_reference_exempt():
    assert run("Part of #944\nRefs #12\n" + DEPLOY_OK, branch="dev/913") == []


def test_multiple_closing_numbers():
    errs = run("Closes #1, #2\n" + DEPLOY_OK, branch="dev/1-x")
    assert len(errs) == 1 and "#2" in errs[0]


def test_template_placeholder_and_code_ignored():
    body = "Closes #\n`Closes #99`\n" + DEPLOY_OK
    assert run(body, branch="dev/1") == []


def test_exemptions():
    assert run("", author="dependabot[bot]") == []
    assert run("", title="chore(graph): update") == []
    assert run("", title="chore(ci): bump") == []
    assert run("", title="x [skip ci]") == []
    assert run("", author="human", title="docs: x")  # docs not exempt


def test_cli_exit_codes(tmp_path):
    good = tmp_path / "g.md"
    good.write_text("Closes #5\n" + DEPLOY_OK)
    bad = tmp_path / "b.md"
    bad.write_text("Closes #5\n")
    base = [sys.executable, str(SCRIPT), "--branch", "dev/5-x"]
    ok = subprocess.run(base + ["--body-file", str(good)],
                        capture_output=True, text=True)
    ko = subprocess.run(base + ["--body-file", str(bad)],
                        capture_output=True, text=True)
    missing = subprocess.run(base + ["--body-file", str(tmp_path / "no")],
                             capture_output=True, text=True)
    assert ok.returncode == 0
    assert ko.returncode == 1 and "How to deploy" in ko.stdout
    assert missing.returncode == 2
