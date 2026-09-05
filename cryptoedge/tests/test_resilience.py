"""Regression tests for the 2026-09-01 outage and the 2026-09-02 false alarm.

Two incidents, two lessons, both encoded here:

1. The host lost network for 28 HOURS. The collector stayed `active`, retried,
   logged warnings nobody read, and collected nothing. systemd's
   Restart=always never fired because nothing crashed. ~340 windows lost, found
   only because SSH dropped.

2. Diagnosing it required reproducing the HTTP request by hand, because every
   failure had been collapsed into one generic RuntimeError naming only the URL.
"""
from __future__ import annotations

import sqlite3
import urllib.error

import pytest

from cryptoedge import collector, db as cdb, gamma


# --- 1. failures must be diagnosable from the log alone -------------------

def test_describe_error_distinguishes_http_status():
    exc = urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, None)
    got = gamma.describe_error(exc)
    assert "429" in got and "Too Many Requests" in got


def test_describe_error_distinguishes_dns_from_other_urlerrors():
    import socket
    dns = gamma.describe_error(urllib.error.URLError(socket.gaierror(-2, "Name or service not known")))
    refused = gamma.describe_error(urllib.error.URLError(ConnectionRefusedError("refused")))
    assert "gaierror" in dns
    assert "ConnectionRefusedError" in refused
    assert dns != refused, "DNS failure must be distinguishable from a refused connection"


def test_describe_error_handles_timeout():
    assert "TimeoutError" in gamma.describe_error(TimeoutError("timed out"))


def test_get_failure_message_names_the_cause_not_just_the_url(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None)
    monkeypatch.setattr(gamma.urllib.request, "urlopen", boom)
    monkeypatch.setattr(gamma.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError) as ei:
        gamma._get("https://gamma-api.polymarket.com/events?slug=x")
    assert "503" in str(ei.value), "the cause must survive into the raised error"


# --- 2. a stalled collector must exit, not loop silently ------------------

def _args(tmp_path, **over):
    import argparse
    d = dict(db=str(tmp_path / "ce.db"), interval=0.0, assets="btc", windows="5",
             once=False)
    d.update(over)
    return argparse.Namespace(**d)


def test_exits_nonzero_after_sustained_failure(tmp_path, monkeypatch):
    """The 28-hour outage must become a restart, not a silent stall."""
    monkeypatch.setattr(gamma, "fetch_slugs",
                        lambda slugs: (_ for _ in ()).throw(RuntimeError("gamma down")))
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)
    rc = collector.main(["--db", str(tmp_path / "ce.db"), "--interval", "0",
                         "--assets", "btc", "--windows", "5"])
    assert rc == 1, "sustained failure must exit non-zero so systemd restarts us"


def test_failed_polls_are_recorded_but_write_no_quotes(tmp_path, monkeypatch):
    """An outage must produce ABSENT data, never wrong data."""
    monkeypatch.setattr(gamma, "fetch_slugs",
                        lambda slugs: (_ for _ in ()).throw(RuntimeError("gamma down")))
    con = cdb.connect(tmp_path / "ce.db")
    assert collector.poll_once(con, {"btc"}, {5}) == 0
    assert con.execute("SELECT count(*) FROM quotes").fetchone()[0] == 0
    row = con.execute("SELECT ok, note FROM poll_runs").fetchone()
    assert row["ok"] == 0
    assert "gamma down" in row["note"]
    con.close()


def test_counter_resets_after_a_good_poll(tmp_path, monkeypatch):
    """A blip must not accumulate toward the exit threshold."""
    calls = {"n": 0}

    def flaky(slugs):
        calls["n"] += 1
        if calls["n"] % 2:
            raise RuntimeError("transient")
        return []
    monkeypatch.setattr(gamma, "fetch_slugs", flaky)
    monkeypatch.setattr(collector.time, "sleep", lambda *_: None)
    con = cdb.connect(tmp_path / "ce.db")
    for _ in range(6):
        collector.poll_once(con, {"btc"}, {5})
    assert con.execute("SELECT count(*) FROM poll_runs WHERE ok=1").fetchone()[0] > 0
    con.close()


# --- 3. the timestamp comparison that caused a false alarm ----------------

def test_iso_timestamp_comparison_requires_datetime_wrapper():
    """'T' (0x54) sorts above ' ' (0x20), so a naive string compare against a
    datetime('now')-style cutoff matches every row from the same DATE.

    That bug reported 3,027 polls / 513 failures for an hour that really had 240
    polls and zero failures, and triggered an unnecessary restart.

    Both timestamps are FIXED literals on purpose. An earlier version of this
    test compared a hardcoded row against a live datetime('now'), which meant it
    only exercised the trap on the day it was written and silently stopped
    testing anything three days later (#844's flake class). The trap needs the
    two values to share a date, so both are pinned here.
    """
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t(ts TEXT)")
    con.execute("INSERT INTO t VALUES ('2026-09-02T07:01:59.885285+00:00')")
    cutoff = "2026-09-02 17:20:00"        # same date, SPACE separator, 10h later

    naive = con.execute("SELECT ts > ? FROM t", (cutoff,)).fetchone()[0]
    fixed = con.execute("SELECT datetime(ts) > ? FROM t", (cutoff,)).fetchone()[0]

    assert naive == 1, (
        "documents the trap: 07:01 wrongly compares as LATER than 17:20 "
        "because 'T' > ' '")
    assert fixed == 0, "datetime(ts) compares correctly"


def test_healthcheck_wraps_every_time_comparison():
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "healthcheck.sh"
    text = src.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue          # the header documents the BAD pattern deliberately
        if "datetime('now'" in line and "WHERE" in line.upper():
            assert "datetime(ts)" in line, (
                f"unwrapped ts comparison would silently match a whole day: {line.strip()}")
