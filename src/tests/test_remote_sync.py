"""Tests for the incremental remote data sync (scripts/Fetch-RemoteData.ps1).

Covers the two behaviours the sync exists to guarantee:

1. **Nothing already held is re-downloaded.** Planning decisions (skip / full /
   tail-resume, and the database fingerprint check) are pure functions of the
   remote inventory plus the local files, so they are tested directly.
2. **Databases never arrive torn.** Snapshots are taken through SQLite rather
   than copied, and the block-delta round trip must reconstruct the snapshot
   byte for byte -- including when it grows, shrinks, or is missing locally.

No network: everything here runs against tmp_path.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from scripts import remote_sync as rs
from scripts.remote_sync import apply_regular
from scripts import remote_sync_agent as agent


# --------------------------------------------------------------------------- #
# configuration parsing
# --------------------------------------------------------------------------- #
def test_parse_env_file_handles_quotes_export_crlf_and_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(
        b"# a comment\r\n"
        b"REMOTE_HOST=host.local\r\n"
        b'export REMOTE_USER="bot"\r\n'
        b"REMOTE_KEY_PATH='~/.ssh/k'\r\n"
        b"\r\n"
        b"NOT_AN_ASSIGNMENT\r\n"
    )
    values = rs.parse_env_file(str(env))
    assert values["REMOTE_HOST"] == "host.local"
    assert values["REMOTE_USER"] == "bot"
    assert values["REMOTE_KEY_PATH"] == "~/.ssh/k"
    assert "NOT_AN_ASSIGNMENT" not in values


def test_load_config_reports_every_missing_credential(tmp_path):
    (tmp_path / ".env").write_text("REMOTE_HOST=h\n", encoding="utf-8")
    with pytest.raises(rs.SyncError) as excinfo:
        rs.load_config(str(tmp_path), ".env")
    message = str(excinfo.value)
    assert "REMOTE_USER" in message and "REMOTE_PROJECT_ROOT" in message


def test_load_config_defaults_and_optional_knobs(tmp_path):
    key = tmp_path / "id_key"
    key.write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "REMOTE_HOST=h\nREMOTE_USER=u\nREMOTE_KEY_PATH=%s\n"
        "REMOTE_PROJECT_ROOT=/srv/app/\n"
        "REMOTE_SYNC_EXCLUDE=cryptoedge.db, *.bak*\n" % key,
        encoding="utf-8")
    cfg = rs.load_config(str(tmp_path), ".env")
    assert cfg["root"] == "/srv/app"            # trailing slash trimmed
    assert cfg["dirs"] == ["logs", "data"]
    assert cfg["exclude"] == ["cryptoedge.db", "*.bak*"]
    assert cfg["db_method"] == "backup"


@pytest.mark.parametrize("rel,expected", [
    ("data/cryptoedge.db", True),
    ("cryptoedge.db", True),
    ("data/meteoedge.db.bak-20260828", True),
    ("logs/bot.log", False),
    ("data/analytics.db", False),
])
def test_matches_any_on_path_or_bare_name(rel, expected):
    assert rs.matches_any(rel, ["cryptoedge.db", "*.bak*"]) is expected


@pytest.mark.parametrize("rel,expected", [
    ("data/meteoedge.db", True),
    ("data/meteoedge.db.bak-20260828-112302", True),
    ("data/meteoedge.db.bak.20260721-1903", True),
    ("data/x.sqlite3", True),
    ("data/meteoedge.db-wal", False),
    ("data/meteoedge.db-shm", False),
    ("logs/bot.log", False),
    ("data/notes.dbf", False),
])
def test_is_database_classification(rel, expected):
    assert agent.is_database(rel) is expected


@pytest.mark.parametrize("rel", ["../etc/passwd", "/abs/path", "C:/win", "a/../b"])
def test_safe_rel_rejects_escapes(rel):
    assert rs.safe_rel(rel) is False


def test_safe_rel_accepts_ordinary_relative_paths():
    assert rs.safe_rel("logs/sub/bot.log") is True


# --------------------------------------------------------------------------- #
# "don't re-download what we already have"
# --------------------------------------------------------------------------- #
def _write(path, payload, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if mtime is not None:
        os.utime(str(path), (mtime, mtime))
    return path


def test_plan_regular_asks_the_remote_to_confirm_an_undated_identical_copy(tmp_path):
    # Same size, mtime we cannot vouch for (what the old scp left behind): the
    # remote compares digests instead of the driver re-fetching blind.
    local = _write(tmp_path / "logs" / "old.jsonl.gz", b"abc", mtime=1_600_000_000)
    mode, extra = rs.plan_regular("logs/old.jsonl.gz", 3, 1_700_000_000, str(local))
    assert mode == "maybe_same"
    assert extra["local_digest"] == agent.file_digest(str(local))


def test_plan_regular_skips_a_file_we_already_hold(tmp_path):
    local = _write(tmp_path / "logs" / "bot.log", b"abc", mtime=1_700_000_000)
    mode, extra = rs.plan_regular("logs/bot.log", 3, 1_700_000_000, str(local))
    assert (mode, extra) == ("skip", {})


def test_plan_regular_tolerates_sub_second_mtime_skew(tmp_path):
    local = _write(tmp_path / "logs" / "bot.log", b"abc", mtime=1_700_000_000)
    mode, _ = rs.plan_regular("logs/bot.log", 3, 1_700_000_001.4, str(local))
    assert mode == "skip"


def test_plan_regular_fetches_a_file_we_do_not_have(tmp_path):
    mode, extra = rs.plan_regular("logs/new.log", 10, 1_700_000_000,
                                  str(tmp_path / "logs" / "new.log"))
    assert (mode, extra) == ("full", {})


def test_plan_regular_resumes_an_appended_log(tmp_path):
    local = _write(tmp_path / "logs" / "bot.log", b"line1\n", mtime=1_700_000_000)
    mode, extra = rs.plan_regular("logs/bot.log", 40, 1_700_000_500, str(local))
    assert mode == "maybe_tail"
    assert extra["local_size"] == 6
    assert extra["local_digest"] == agent.file_digest(str(local))


def test_plan_regular_refetches_a_rotated_archive_whole(tmp_path):
    # .gz archives are rewritten, not appended, so resuming makes no sense.
    local = _write(tmp_path / "logs" / "bot.log.gz", b"zzz", mtime=1_700_000_000)
    mode, _ = rs.plan_regular("logs/bot.log.gz", 99, 1_700_000_500, str(local))
    assert mode == "full"


def test_prepare_confirms_an_identical_file_without_staging_it(tmp_path, capsys):
    import json

    root = tmp_path / "remote"
    same = _write(root / "logs" / "same.log", b"identical bytes!")
    differs = _write(root / "logs" / "differs.log", b"0123456789")
    plan = {"regular": [
        {"p": "logs/same.log", "mode": "maybe_same", "local_size": 16,
         "local_digest": agent.file_digest(str(same))},
        {"p": "logs/differs.log", "mode": "maybe_same", "local_size": 10,
         "local_digest": agent.file_digest(str(differs)).replace("a", "b")},
    ]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    agent.main(["prepare", "--root", str(root), "--plan", str(plan_path),
                "--stage", str(tmp_path / "stage")])
    report = _captured_json(capsys)
    actions = dict((e["p"], e["action"]) for e in report["regular"])
    assert actions == {"logs/same.log": "same", "logs/differs.log": "full"}


def test_apply_regular_adopts_the_remote_mtime_for_confirmed_files(tmp_path):
    target = _write(tmp_path / "logs" / "same.log", b"identical", mtime=1_600_000_000)
    entries = [{"p": "logs/same.log", "action": "same", "size": 9, "mtime": 1_700_000_000}]
    applied, errors = apply_regular(None, str(tmp_path), entries, lambda *a: None)
    assert (applied["same"], errors) == (1, [])
    assert rs.mtime_close(os.stat(str(target)).st_mtime, 1_700_000_000)
    assert target.read_bytes() == b"identical"      # untouched


def test_plan_regular_refetches_a_shrunken_file_whole(tmp_path):
    local = _write(tmp_path / "logs" / "bot.log", b"aaaaaaaaaa", mtime=1_700_000_000)
    mode, _ = rs.plan_regular("logs/bot.log", 4, 1_700_000_500, str(local))
    assert mode == "full"


def test_plan_regular_force_ignores_an_identical_local_copy(tmp_path):
    local = _write(tmp_path / "logs" / "bot.log", b"abc", mtime=1_700_000_000)
    mode, _ = rs.plan_regular("logs/bot.log", 3, 1_700_000_000, str(local), force=True)
    assert mode == "full"


def test_db_is_current_only_when_remote_and_local_both_match(tmp_path):
    local = _write(tmp_path / "data" / "x.db", b"snapshot", mtime=1_700_000_000)
    fp = {"db": [100, 5.0], "wal": 0}
    entry = {"fp": fp, "local_size": 8, "local_mtime": 1_700_000_000}

    assert rs.db_is_current(fp, entry, str(local)) is True
    # the remote WAL grew -> the database was committed to since we fetched it
    assert rs.db_is_current({"db": [100, 5.0], "wal": 4096}, entry, str(local)) is False
    # the main file was written (a checkpoint, or a non-WAL database)
    assert rs.db_is_current({"db": [100, 9.0], "wal": 0}, entry, str(local)) is False
    # the local copy was replaced behind our back
    assert rs.db_is_current(fp, {"fp": fp, "local_size": 4, "local_mtime": 1_700_000_000},
                            str(local)) is False
    # never fetched before
    assert rs.db_is_current(fp, None, str(local)) is False
    # local copy deleted
    assert rs.db_is_current(fp, entry, str(tmp_path / "data" / "gone.db")) is False


def test_source_fingerprint_ignores_shm_and_wal_timestamps(tmp_path):
    """Taking a snapshot re-times the sidecars; the fingerprint must survive it."""
    db = _write(tmp_path / "live.db", b"x" * 100, mtime=1_700_000_000)
    _write(tmp_path / "live.db-wal", b"", mtime=1_700_000_000)
    _write(tmp_path / "live.db-shm", b"y" * 32768, mtime=1_700_000_000)
    before = agent.source_fingerprint(str(db))

    # what a read-only open does to the sidecars
    os.utime(str(tmp_path / "live.db-wal"), (1_700_009_999, 1_700_009_999))
    os.utime(str(tmp_path / "live.db-shm"), (1_700_009_999, 1_700_009_999))
    assert agent.source_fingerprint(str(db)) == before

    # but a commit appending to the WAL must show up
    _write(tmp_path / "live.db-wal", b"z" * 4096)
    assert agent.source_fingerprint(str(db)) != before


def test_source_fingerprint_reports_a_missing_wal_as_none(tmp_path):
    db = _write(tmp_path / "plain.db", b"x" * 10)
    assert agent.source_fingerprint(str(db))["wal"] is None


def test_load_state_discards_state_from_another_server(tmp_path):
    path = tmp_path / rs.STATE_FILE
    rs.save_state(str(path), {"version": rs.STATE_VERSION, "host": "old", "root": "/a",
                              "dbs": {"data/x.db": {"fp": {}}}})
    fresh = rs.load_state(str(path), "new", "/a")
    assert fresh["dbs"] == {}
    kept = rs.load_state(str(path), "old", "/a")
    assert "data/x.db" in kept["dbs"]


def test_load_state_survives_a_corrupt_state_file(tmp_path):
    path = tmp_path / rs.STATE_FILE
    path.write_text("{not json", encoding="utf-8")
    state = rs.load_state(str(path), "h", "/r")
    assert state["dbs"] == {}


# --------------------------------------------------------------------------- #
# block delta round trip
# --------------------------------------------------------------------------- #
BS = 1024


def _round_trip(tmp_path, old_payload, new_payload):
    """Reconstruct ``new_payload`` from a local copy of ``old_payload`` + a delta."""
    local = _write(tmp_path / "local.bin", old_payload)
    remote = _write(tmp_path / "remote.bin", new_payload)
    have = agent.block_digests(str(local), BS) if old_payload is not None else []
    indices = agent.changed_block_indices(have, agent.block_digests(str(remote), BS))
    blob = tmp_path / "delta.blob"
    sent = agent.extract_blocks(str(remote), indices, str(blob), BS)
    agent.apply_blocks(str(local), str(blob), indices, len(new_payload), BS)
    assert local.read_bytes() == new_payload
    assert agent.file_digest(str(local)) == agent.file_digest(str(remote))
    return sent, indices


def test_delta_sends_nothing_when_the_snapshot_is_unchanged(tmp_path):
    payload = os.urandom(BS * 5 + 17)
    sent, indices = _round_trip(tmp_path, payload, payload)
    assert (sent, indices) == (0, [])


def test_delta_sends_only_the_changed_block(tmp_path):
    old = bytearray(os.urandom(BS * 6))
    new = bytearray(old)
    new[BS * 3:BS * 3 + 10] = b"0123456789"
    sent, indices = _round_trip(tmp_path, bytes(old), bytes(new))
    assert indices == [3]
    assert sent == BS


def test_delta_sends_only_the_tail_when_the_snapshot_grew(tmp_path):
    old = os.urandom(BS * 4)
    new = old + os.urandom(BS * 2 + 5)
    _sent, indices = _round_trip(tmp_path, old, new)
    assert indices == [4, 5, 6]          # two whole blocks plus the short one


def test_delta_truncates_when_the_snapshot_shrank(tmp_path):
    old = os.urandom(BS * 8)
    new = old[:BS * 3]
    _sent, indices = _round_trip(tmp_path, old, new)
    assert indices == []                      # nothing to send, only to truncate


def test_delta_degrades_to_everything_when_there_is_no_local_copy(tmp_path):
    payload = os.urandom(BS * 3 + 1)
    remote = _write(tmp_path / "remote.bin", payload)
    indices = agent.changed_block_indices([], agent.block_digests(str(remote), BS))
    assert indices == [0, 1, 2, 3]


def test_apply_blocks_rejects_a_truncated_delta(tmp_path):
    local = _write(tmp_path / "local.bin", b"\x00" * (BS * 2))
    short = _write(tmp_path / "delta.blob", b"\x01" * 10)
    with pytest.raises(ValueError):
        agent.apply_blocks(str(local), str(short), [0], BS * 2, BS)


def test_file_digest_prefix_matches_a_truncated_copy(tmp_path):
    payload = os.urandom(5000)
    whole = _write(tmp_path / "whole.bin", payload)
    prefix = _write(tmp_path / "prefix.bin", payload[:1234])
    assert agent.file_digest(str(whole), limit=1234) == agent.file_digest(str(prefix))


# --------------------------------------------------------------------------- #
# snapshots: the reason this sync stopped producing torn databases
# --------------------------------------------------------------------------- #
def _make_live_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [("x" * 200,) for _ in range(2000)])
    con.commit()
    return con                                 # left open, WAL hot, like the bot's


@pytest.mark.parametrize("method", ["backup", "vacuum"])
def test_snapshot_of_a_live_wal_database_is_complete_and_standalone(tmp_path, method):
    src = tmp_path / "live.db"
    writer = _make_live_db(src)
    try:
        # A writer with uncommitted work in flight, exactly the situation a plain
        # scp used to capture half of.
        writer.execute("INSERT INTO t (v) VALUES ('uncommitted')")
        out = tmp_path / "snap.db"
        used = agent.snapshot_database(str(src), str(out), method=method)
        assert used == method or used == "vacuum"   # backup may fall back
        assert (tmp_path / "live.db-wal").exists()  # source still in WAL mode
        assert not (tmp_path / "snap.db-wal").exists()
        assert not (tmp_path / "snap.db-shm").exists()

        con = sqlite3.connect(str(out))
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 2000
        con.close()
    finally:
        writer.rollback()
        writer.close()


def test_snapshot_overwrites_a_previous_snapshot_and_its_sidecars(tmp_path):
    src = tmp_path / "live.db"
    writer = _make_live_db(src)
    writer.close()
    out = tmp_path / "snap.db"
    out.write_bytes(b"stale")
    (tmp_path / "snap.db-wal").write_bytes(b"stale wal")
    agent.snapshot_database(str(src), str(out))
    assert agent.looks_like_sqlite(str(out))
    assert not (tmp_path / "snap.db-wal").exists()


def test_non_sqlite_files_are_detected_rather_than_snapshotted(tmp_path):
    plain = _write(tmp_path / "not.db", b"just bytes")
    assert agent.looks_like_sqlite(str(plain)) is False


def test_clear_sidecars_removes_a_stale_wal_next_to_a_fresh_snapshot(tmp_path):
    db = _write(tmp_path / "x.db", b"snapshot")
    _write(tmp_path / "x.db-wal", b"stale")
    _write(tmp_path / "x.db-shm", b"stale")
    removed = rs.clear_sidecars(str(db))
    assert sorted(removed) == ["x.db-shm", "x.db-wal"]
    assert db.exists()


# --------------------------------------------------------------------------- #
# inventory / prepare wire protocol
# --------------------------------------------------------------------------- #
def test_inventory_lists_files_and_skips_sidecar_free_walk(tmp_path, capsys):
    _write(tmp_path / "logs" / "bot.log", b"a")
    _write(tmp_path / "logs" / "sub" / "old.log", b"bb")
    _write(tmp_path / "data" / "x.db", b"ccc")
    agent.main(["inventory", "--root", str(tmp_path), "--dir", "logs", "--dir", "data"])
    payload = _captured_json(capsys)
    paths = [f["p"] for f in payload["files"]]
    assert paths == ["data/x.db", "logs/bot.log", "logs/sub/old.log"]
    assert payload["files"][0]["s"] == 3


def test_prepare_stages_a_bundle_a_tail_and_a_db_snapshot(tmp_path, capsys):
    import json

    root = tmp_path / "remote"
    _write(root / "logs" / "fresh.log", b"brand new\n")
    _write(root / "logs" / "grown.log", b"old part\nnew part\n")
    writer = _make_live_db(root / "data" / "live.db")
    writer.close()

    plan = {
        "block_size": BS,
        "regular": [
            {"p": "logs/fresh.log", "mode": "full"},
            {"p": "logs/grown.log", "mode": "maybe_tail", "local_size": 9,
             "local_digest": agent.file_digest(str(root / "logs" / "grown.log"), limit=9)},
            {"p": "logs/vanished.log", "mode": "full"},
        ],
        "dbs": [{"p": "data/live.db", "mode": "full"}],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    agent.main(["prepare", "--root", str(root), "--plan", str(plan_path),
                "--stage", str(tmp_path / "stage")])
    report = _captured_json(capsys)

    actions = dict((e["p"], e["action"]) for e in report["regular"])
    assert actions == {"logs/fresh.log": "full", "logs/grown.log": "tail",
                       "logs/vanished.log": "missing"}
    tail = [e for e in report["regular"] if e["p"] == "logs/grown.log"][0]
    assert tail["offset"] == 9 and tail["tail_size"] == len(b"new part\n")
    assert os.path.exists(report["bundle"])

    db = report["dbs"][0]
    assert db["action"] == "full" and db["snapshot_size"] > 0
    assert db["snapshot_digest"] == agent.file_digest(db["payload"])
    assert report["errors"] == []


def test_prepare_falls_back_to_a_full_copy_when_the_delta_saves_nothing(tmp_path, capsys):
    import json

    root = tmp_path / "remote"
    writer = _make_live_db(root / "data" / "live.db")
    writer.close()
    plan = {"block_size": BS,
            "dbs": [{"p": "data/live.db", "mode": "delta", "blocks": []}]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    agent.main(["prepare", "--root", str(root), "--plan", str(plan_path),
                "--stage", str(tmp_path / "stage")])
    report = _captured_json(capsys)
    # Every block differs, so shipping a "delta" would be pointless overhead.
    assert report["dbs"][0]["action"] == "full"


def _captured_json(capsys):
    import json

    out = capsys.readouterr().out
    return json.loads(out.split(agent.JSON_MARKER, 1)[1])


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    (0, "0 B"), (512, "512 B"), (2048, "2.0 KB"),
    (5 * 1024 * 1024, "5.0 MB"), (3 * 1024 ** 3, "3.0 GB"),
])
def test_human_readable_sizes(value, expected):
    assert rs.human(value) == expected
