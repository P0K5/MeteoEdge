"""Regression tests for scripts/ensure_worktree.sh (issue #800).

These reproduce the failure mode that corrupted #781/#780: a dev agent working
in the shared checkout, branching off whatever HEAD the shared checkout happened
to be sitting on (another concurrent process's branch), and committing there.

The helper must instead land the agent in an isolated worktree, on its own
branch, branched off an *explicit* base ref -- independent of the shared
checkout's current HEAD -- and fail loudly (never silently fall back to the
shared checkout) when it can't.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENSURE_WT = REPO_ROOT / "scripts" / "ensure_worktree.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="bash and git are required",
)


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        capture_output=True, text=True,
    )


def _run_ensure(shared_checkout, *args, script=ENSURE_WT):
    """Run ensure_worktree.sh (or an instrumented copy of it) from inside the
    shared checkout."""
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=str(shared_checkout),
        capture_output=True, text=True,
    )


@pytest.fixture()
def shared_checkout(tmp_path):
    """A bare 'origin' + a shared working checkout cloned from it, on master."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(origin)],
                   check=True, capture_output=True)

    work = tmp_path / "MeteoEdge"
    subprocess.run(["git", "clone", str(origin), str(work)],
                   check=True, capture_output=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "seed.txt").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base commit")
    _git(work, "push", "origin", "master")
    _git(work, "fetch", "origin")
    return work


def _last_line(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def test_creates_isolated_worktree(shared_checkout):
    res = _run_ensure(shared_checkout, "feat/isolated-1")
    assert res.returncode == 0, res.stderr
    wt = Path(_last_line(res.stdout))
    assert wt.exists()

    toplevel = _git(wt, "rev-parse", "--show-toplevel").stdout.strip()
    main_root = _git(shared_checkout, "rev-parse", "--show-toplevel").stdout.strip()
    # The crux: the worktree is NOT the shared checkout.
    assert toplevel == str(wt.resolve())
    assert toplevel != main_root
    # ...and it lives under .claude/worktrees/agent-*
    assert ".claude/worktrees/agent-" in str(wt).replace("\\", "/")

    branch = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "feat/isolated-1"


def test_branches_off_explicit_base_not_shared_head(shared_checkout):
    """The #781 corruption: shared checkout moves to an unrelated branch with
    uncommitted work; the new worktree must still branch off origin/master, not
    inherit the stray branch, and must leave the shared checkout untouched."""
    master_tip = _git(shared_checkout, "rev-parse", "origin/master").stdout.strip()

    # Simulate a concurrent process moving the shared checkout's HEAD and adding
    # an extra commit + uncommitted work.
    _git(shared_checkout, "checkout", "-b", "reflect/2026-07-22")
    (shared_checkout / "stray.txt").write_text("concurrent commit\n")
    _git(shared_checkout, "add", "-A")
    _git(shared_checkout, "commit", "-m", "unrelated concurrent commit")
    stray_tip = _git(shared_checkout, "rev-parse", "HEAD").stdout.strip()
    (shared_checkout / "dirty.txt").write_text("uncommitted in-progress work\n")

    res = _run_ensure(shared_checkout, "junior/781-emos-projection", "origin/master")
    assert res.returncode == 0, res.stderr
    wt = Path(_last_line(res.stdout))

    wt_tip = _git(wt, "rev-parse", "HEAD").stdout.strip()
    # Branched off the explicit base, NOT the shared checkout's moved HEAD.
    assert wt_tip == master_tip
    assert wt_tip != stray_tip
    # The stray concurrent commit is absent from the isolated branch.
    assert not (wt / "stray.txt").exists()
    # The shared checkout's uncommitted work is untouched.
    assert (shared_checkout / "dirty.txt").read_text() == "uncommitted in-progress work\n"


def test_idempotent_reuse(shared_checkout):
    """Issue #1235: the reuse check compares the worktree's own reported
    toplevel (from `git -C <dir> rev-parse --show-toplevel`, native
    drive-letter form on Windows, e.g. `C:/...`) against the directory path
    the script itself built (MSYS form on Windows, e.g. `/c/...`). Before the
    fix those two spellings of the identical directory never compared equal,
    so re-running against a genuinely intact worktree always reported
    BLOCKED. This is the exact failure that blocked issue #1236."""
    r1 = _run_ensure(shared_checkout, "feat/reuse")
    r2 = _run_ensure(shared_checkout, "feat/reuse")
    assert r1.returncode == 0 and r2.returncode == 0, (r1.stderr, r2.stderr)
    assert _last_line(r1.stdout) == _last_line(r2.stdout)
    assert "reusing existing isolated worktree" in r2.stderr


def test_isolation_check_detects_worktree_root_equal_to_shared_checkout(shared_checkout, tmp_path):
    """Issue #1235: the reuse check (`=`) and the final isolation check
    (`!=`, "the whole point of #800") suffer the identical Windows path-
    spelling mismatch with opposite operators, and opposite consequences.
    The reuse check fails LOUDLY on a valid worktree (covered by
    test_idempotent_reuse above). The isolation check fails SILENTLY: on
    Windows it never fires at all, because the two spellings it compares
    also never compare equal -- so it would rubber-stamp a worktree whose
    root is genuinely the shared checkout itself as "isolated".

    The script's own directory construction (WT_DIR is always
    "$MAIN_ROOT/.claude/worktrees/agent-<sanitized>", and sanitization
    strips '/' so no path-traversal payload in <branch> can collapse that
    back to MAIN_ROOT) makes WT_DIR == MAIN_ROOT structurally unreachable
    through the CLI -- which is correct, desired behaviour, not a gap. To
    exercise the guard clause that exists specifically to catch that
    condition, this test runs the REAL, unmodified script with exactly one
    line of test scaffolding overridden (the WT_DIR assignment, forced to
    MAIN_ROOT) -- fault injection of the precondition, not a rewrite of the
    logic under test. norm_path() and the isolation check's `!=` comparison
    that consumes it are exercised byte-for-byte as shipped.
    """
    script_text = ENSURE_WT.read_text()
    marker = 'WT_DIR="$MAIN_ROOT/.claude/worktrees/agent-$SANITIZED"'
    assert marker in script_text, "ensure_worktree.sh's WT_DIR assignment changed shape; update this test"
    faulty_script = tmp_path / "ensure_worktree_force_wt_dir_eq_main_root.sh"
    faulty_script.write_text(script_text.replace(marker, 'WT_DIR="$MAIN_ROOT"'))

    # Branch name matches the shared checkout's current branch ("master") so
    # the reuse check's branch comparison doesn't short-circuit us before we
    # reach the isolation check -- we need to land on the isolation BLOCK
    # specifically, not any other one.
    res = _run_ensure(shared_checkout, "master", script=faulty_script)
    assert res.returncode != 0, (
        "isolation check failed to fire for a worktree root genuinely equal "
        f"to the shared checkout root: stdout={res.stdout!r} stderr={res.stderr!r}"
    )
    assert "BLOCKED" in res.stderr
    assert "isolation failed" in res.stderr, (
        "expected the isolation-check BLOCK message specifically, got: " + res.stderr
    )


def test_two_close_spawns_get_distinct_worktrees(shared_checkout):
    """Two agents spawned close together must not collide on dir or branch."""
    a = _run_ensure(shared_checkout, "mid/900-a")
    b = _run_ensure(shared_checkout, "junior/901-b")
    assert a.returncode == 0 and b.returncode == 0
    pa, pb = Path(_last_line(a.stdout)), Path(_last_line(b.stdout))
    assert pa != pb
    assert _git(pa, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "mid/900-a"
    assert _git(pb, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "junior/901-b"


def test_fails_loud_on_bad_base_ref(shared_checkout):
    res = _run_ensure(shared_checkout, "feat/bad-base", "origin/does-not-exist")
    assert res.returncode != 0
    assert "BLOCKED" in res.stderr
    # No worktree left behind.
    assert not (shared_checkout / ".claude" / "worktrees" / "agent-feat--bad-base").exists()


def test_fails_loud_on_missing_branch_arg(shared_checkout):
    res = _run_ensure(shared_checkout)
    assert res.returncode != 0
    assert "BLOCKED" in res.stderr


def test_fails_loud_outside_git_repo(tmp_path):
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    res = subprocess.run(
        ["bash", str(ENSURE_WT), "feat/x"],
        cwd=str(non_repo), capture_output=True, text=True,
    )
    assert res.returncode != 0
    assert "BLOCKED" in res.stderr


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
