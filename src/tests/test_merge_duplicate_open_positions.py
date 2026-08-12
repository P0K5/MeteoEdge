"""Tests for src/scripts/merge_duplicate_open_positions.py (issue #611).

The pre-#611 stacking bug left multiple open_positions rows for the same
token_id (3 rows for one WMKK token on 2026-07-03). The one-off script must
merge them into a single row -- summed shares, share-weighted entry price,
earliest entry_ts, most protective stop/take-profit -- and be idempotent.

No calendar dates are hardcoded: entry timestamps derive from
datetime.now(timezone.utc) at test run time.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.data.db import Database
from src.scripts.merge_duplicate_open_positions import merge_duplicates


def _ts(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


TOKEN = "token-wmkk-no-611"


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "merge-test.db"
    db = Database(path)

    def _add(order_id, shares, price, entry_ts, stop=None, tp=None,
             token_id=TOKEN, station="WMKK", side="NO"):
        trade_id = db.insert_trade(
            ts=entry_ts,
            station=station,
            ticker=f"{station}-order-{order_id}",
            bracket_low=91.4,
            bracket_high=93.2,
            side=side,
            predicted_price=85,
            actual_price=price,
            predicted_edge=15.0,
            mode="live",
            order_id=order_id,
            outcome="filled",
            capital_before=5.0,
        )
        db.open_position(
            trade_id=trade_id,
            station=station,
            ticker=f"{station}-order-{order_id}",
            token_id=token_id,
            side=side,
            order_id=order_id,
            entry_price=price,
            shares=shares,
            entry_ts=entry_ts,
            stop_loss_cents=stop,
            take_profit_cents=tp,
        )

    # Three stacked fills on the same token (mirrors the WMKK incident):
    # earliest first. Weighted price: (5*80 + 5*90 + 10*85) / 20 = 85.
    _add("order-earliest", 5.0, 80, _ts(60), stop=60, tp=95)
    _add("order-middle", 5.0, 90, _ts(40), stop=70, tp=90)
    _add("order-latest", 10.0, 85, _ts(20), stop=None, tp=None)

    # A healthy single-row position on another token must never be touched.
    _add("order-other", 6.0, 75, _ts(50), token_id="token-katl-no-1", station="KATL")

    return path


def _rows(db_path, token=TOKEN):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM open_positions WHERE token_id=? ORDER BY id ASC", (token,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class TestMerge:
    def test_three_rows_merge_into_one(self, db_path):
        assert merge_duplicates(db_path) == 0

        rows = _rows(db_path)
        assert len(rows) == 1, "3 duplicate rows must collapse into exactly 1"
        merged = rows[0]
        assert merged["shares"] == pytest.approx(20.0)
        assert merged["entry_price"] == 85, "share-weighted average, rounded to int cents"
        assert merged["order_id"] == "order-earliest", "keeper is the earliest-entry row"

    def test_earliest_entry_ts_kept(self, db_path):
        conn = sqlite3.connect(str(db_path))
        expected_earliest = conn.execute(
            "SELECT MIN(entry_ts) FROM open_positions WHERE token_id=?", (TOKEN,)
        ).fetchone()[0]
        conn.close()

        assert merge_duplicates(db_path) == 0
        assert _rows(db_path)[0]["entry_ts"] == expected_earliest

    def test_most_protective_stop_and_take_profit_kept(self, db_path):
        assert merge_duplicates(db_path) == 0
        merged = _rows(db_path)[0]
        assert merged["stop_loss_cents"] == 70, "highest stop exits earliest"
        assert merged["take_profit_cents"] == 90, "lowest take-profit locks in earliest"

    def test_other_tokens_untouched(self, db_path):
        before = _rows(db_path, token="token-katl-no-1")
        assert merge_duplicates(db_path) == 0
        after = _rows(db_path, token="token-katl-no-1")
        assert after == before, "single-row positions on other tokens must not change"

    def test_trades_table_untouched(self, db_path):
        """#609 settles the extra trades rows independently -- the merge script
        must only touch open_positions."""
        conn = sqlite3.connect(str(db_path))
        before = conn.execute(
            "SELECT id, ticker, outcome, pnl FROM trades ORDER BY id"
        ).fetchall()
        conn.close()

        assert merge_duplicates(db_path) == 0

        conn = sqlite3.connect(str(db_path))
        after = conn.execute(
            "SELECT id, ticker, outcome, pnl FROM trades ORDER BY id"
        ).fetchall()
        conn.close()
        assert after == before


class TestIdempotency:
    def test_second_run_is_a_noop(self, db_path):
        assert merge_duplicates(db_path) == 0
        rows_after_first = _rows(db_path)

        assert merge_duplicates(db_path) == 0
        assert _rows(db_path) == rows_after_first, "second run must change nothing"


class TestDryRun:
    def test_dry_run_touches_nothing(self, db_path):
        before = _rows(db_path)
        assert len(before) == 3

        assert merge_duplicates(db_path, dry_run=True) == 0

        assert _rows(db_path) == before, "--dry-run must not modify any row"


class TestSafety:
    def test_missing_db_exits_nonzero(self, tmp_path):
        assert merge_duplicates(tmp_path / "does-not-exist.db") == 1

    def test_mismatched_station_rows_refused(self, db_path):
        """Rows for one token spanning two stations indicate data corruption --
        the script must refuse to merge them and exit non-zero."""
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE open_positions SET station='KATL' WHERE order_id='order-middle'"
        )
        conn.commit()
        conn.close()

        assert merge_duplicates(db_path) == 1
        assert len(_rows(db_path)) == 3, "mismatched token must be left untouched"


# ===========================================================================
# Zero-share phantom rows (issue #977)
# ===========================================================================
# Reproduces the exact live KORD incident: open_positions id 435 (trade_id
# 2161, real fill, synthetic ticker "KORD-order-0x418b18") and id 436
# (trade_id 2160, phantom, shares=0.0, real ticker from reconciliation) on
# the same token_id.

PHANTOM_TOKEN = "token-kord-phantom-977"


def _seed_phantom_scenario(db_path, *, funded_ticker=None, phantom_ticker="0xrealconditionhash"):
    db = Database(db_path)
    funded_ticker = funded_ticker or "KORD-order-0x418b18"  # pre-#977 synthetic placeholder

    funded_trade_id = db.insert_trade(
        ts="2026-08-11T11:47:31.021528Z", station="KORD", ticker=funded_ticker,
        bracket_low=32.0, bracket_high=36.0, side="NO", predicted_price=70,
        actual_price=70, predicted_edge=16.0, mode="live", capital_before=5.0,
        order_id="0x418b18",
    )
    db.open_position(
        trade_id=funded_trade_id, station="KORD", ticker=funded_ticker,
        token_id=PHANTOM_TOKEN, side="NO", order_id="0x418b18",
        entry_price=75, shares=6.67, entry_ts="2026-08-11T11:47:31.021528Z",
    )

    phantom_trade_id = db.insert_trade(
        ts="2026-08-11T11:41:52+00:00", station="KORD", ticker=phantom_ticker,
        bracket_low=32.0, bracket_high=36.0, side="NO", predicted_price=70,
        actual_price=70, predicted_edge=16.0, mode="live", capital_before=0.0,
        order_id="0x6f780a", outcome="filled",
    )
    db.open_position(
        trade_id=phantom_trade_id, station="KORD", ticker=phantom_ticker,
        token_id=PHANTOM_TOKEN, side="NO", order_id="0x6f780a",
        entry_price=0, shares=0.0, entry_ts="2026-08-11T11:41:52.086584+00:00",
    )
    return db, funded_trade_id, phantom_trade_id


class TestZeroSharePhantomRows:
    def test_phantom_dropped_real_row_kept(self, tmp_path):
        db_path = tmp_path / "phantom-test.db"
        _seed_phantom_scenario(db_path)

        assert merge_duplicates(db_path) == 0

        rows = _rows(db_path, token=PHANTOM_TOKEN)
        assert len(rows) == 1, "the zero-share phantom row must be dropped, not merged"
        assert rows[0]["order_id"] == "0x418b18"
        assert rows[0]["shares"] == pytest.approx(6.67)

    def test_synthetic_ticker_repaired_from_phantom(self, tmp_path):
        db_path = tmp_path / "phantom-repair-test.db"
        _seed_phantom_scenario(db_path)

        assert merge_duplicates(db_path) == 0

        rows = _rows(db_path, token=PHANTOM_TOKEN)
        assert rows[0]["ticker"] == "0xrealconditionhash", (
            "the funded row's synthetic ticker must be repaired using the "
            "dropped phantom's real condition-id ticker"
        )
        conn = sqlite3.connect(str(db_path))
        trade_ticker = conn.execute(
            "SELECT ticker FROM trades WHERE order_id='0x418b18'"
        ).fetchone()[0]
        conn.close()
        assert trade_ticker == "0xrealconditionhash", "trades.ticker must be repaired too"

    def test_phantom_trades_row_left_untouched(self, tmp_path):
        """Settlement history for the phantom's trade_id is out of scope --
        only its open_positions row (the thing blocking settle.py) is dropped."""
        db_path = tmp_path / "phantom-trade-test.db"
        _seed_phantom_scenario(db_path)

        assert merge_duplicates(db_path) == 0

        conn = sqlite3.connect(str(db_path))
        phantom_trade = conn.execute(
            "SELECT id FROM trades WHERE order_id='0x6f780a'"
        ).fetchone()
        conn.close()
        assert phantom_trade is not None, "the phantom's trades row must survive"

    def test_dry_run_previews_without_modifying(self, tmp_path):
        db_path = tmp_path / "phantom-dryrun-test.db"
        _seed_phantom_scenario(db_path)
        before = _rows(db_path, token=PHANTOM_TOKEN)

        assert merge_duplicates(db_path, dry_run=True) == 0

        assert _rows(db_path, token=PHANTOM_TOKEN) == before

    def test_idempotent_second_run_is_noop(self, tmp_path):
        db_path = tmp_path / "phantom-idem-test.db"
        _seed_phantom_scenario(db_path)

        assert merge_duplicates(db_path) == 0
        after_first = _rows(db_path, token=PHANTOM_TOKEN)

        assert merge_duplicates(db_path) == 0
        assert _rows(db_path, token=PHANTOM_TOKEN) == after_first

    def test_only_phantom_rows_left_untouched_and_flagged(self, tmp_path):
        """A token with ONLY zero-share rows (no real row to keep) must not
        be silently dropped -- that needs a human to confirm the order truly
        never filled before removing the tracking protecting it."""
        db_path = tmp_path / "phantom-only-test.db"
        db = Database(db_path)
        trade_id_a = db.insert_trade(
            ts="2026-08-11T11:41:52Z", station="KORD", ticker="0xa",
            bracket_low=32.0, bracket_high=36.0, side="NO", predicted_price=70,
            actual_price=70, predicted_edge=16.0, mode="live", capital_before=0.0,
            order_id="ord-only-phantom-a",
        )
        db.open_position(
            trade_id=trade_id_a, station="KORD", ticker="0xa", token_id="tok-only-phantom",
            side="NO", order_id="ord-only-phantom-a", entry_price=0, shares=0.0,
            entry_ts="2026-08-11T11:41:52Z",
        )
        trade_id_b = db.insert_trade(
            ts="2026-08-11T11:42:52Z", station="KORD", ticker="0xb",
            bracket_low=32.0, bracket_high=36.0, side="NO", predicted_price=70,
            actual_price=70, predicted_edge=16.0, mode="live", capital_before=0.0,
            order_id="ord-only-phantom-b",
        )
        db.open_position(
            trade_id=trade_id_b, station="KORD", ticker="0xb", token_id="tok-only-phantom",
            side="NO", order_id="ord-only-phantom-b", entry_price=0, shares=0.0,
            entry_ts="2026-08-11T11:42:52Z",
        )

        assert merge_duplicates(db_path) == 1, "all-phantom duplicates must be flagged, not dropped"
        assert len(_rows(db_path, token="tok-only-phantom")) == 2
