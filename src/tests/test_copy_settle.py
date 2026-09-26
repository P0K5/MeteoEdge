"""Unit tests for src/scripts/copy_settle.py (epic #1102 story C2, issue
#1132). Uses a real in-memory Database (seeded rows), mirroring
test_db.py's `Database(":memory:")` pattern, with `fetch_market_resolution`
patched -- no real network calls.
"""
from unittest.mock import patch

from src.data.db import Database
from src.scripts.copy_settle import run_once

ADDRESS = "0xwallet1"
NOW_ISO = "2026-09-20T00:00:00+00:00"


def _seed_position(
    db: Database,
    *,
    market: str,
    outcome_index: int = 0,
    entry_price: float = 0.40,
    stake_usd: float = 10.0,
    address: str = ADDRESS,
) -> int:
    """Insert one open copy_positions row (via its required copy_signals FK
    parent first) and return the new position id."""
    signal_id = db.insert_copy_signal(
        address=address, market=market, source_price=entry_price,
        detected_at=NOW_ISO, outcome_index=outcome_index,
    )
    position_id = db.insert_copy_position(
        signal_id=signal_id, address=address, market=market,
        outcome_index=outcome_index, entry_price=entry_price,
        stake_usd=stake_usd, entry_ts=NOW_ISO,
    )
    db.link_copy_signal_to_position(signal_id, position_id)
    return position_id


class TestRunOnceNoOpenPositions:
    def test_no_open_positions_returns_zero_summary(self):
        db = Database(":memory:")
        with patch("src.scripts.copy_settle.fetch_market_resolution") as mock_resolve:
            summary = run_once(db=db)
        assert summary == {"settled": 0, "pending": 0, "errors": 0}
        mock_resolve.assert_not_called()


class TestRunOnceFullPass:
    """Seeded DB with one YES-resolving, one NO-resolving, and one
    still-unresolved position -- asserts the right rows end up settled with
    correct settled_pnl_usd, and the unresolved one stays open."""

    def test_yes_no_and_unresolved_positions(self):
        db = Database(":memory:")

        yes_id = _seed_position(db, market="0xyesmarket", outcome_index=0, entry_price=0.40, stake_usd=10.0)
        no_id = _seed_position(db, market="0xnomarket", outcome_index=0, entry_price=0.40, stake_usd=10.0)
        pending_id = _seed_position(db, market="0xpendingmarket", outcome_index=0, entry_price=0.40, stake_usd=10.0)

        def fake_resolve(market):
            return {"0xyesmarket": True, "0xnomarket": False, "0xpendingmarket": None}[market]

        with patch("src.scripts.copy_settle.fetch_market_resolution", side_effect=fake_resolve):
            summary = run_once(db=db)

        assert summary == {"settled": 2, "pending": 1, "errors": 0}

        positions = {p["id"]: p for p in db._conn.execute("SELECT * FROM copy_positions").fetchall()}

        yes_row = positions[yes_id]
        assert yes_row["status"] == "settled"
        # entry_price=0.40, stake=10, win -> pnl = 10 * (1-0.4)/0.4 = 15.0
        assert yes_row["settled_pnl_usd"] == 15.0
        assert yes_row["settled_at"] is not None

        no_row = positions[no_id]
        assert no_row["status"] == "settled"
        # outcome_index=0 (YES) loses when yes_won=False -> pnl = -stake
        assert no_row["settled_pnl_usd"] == -10.0

        pending_row = positions[pending_id]
        assert pending_row["status"] == "open"
        assert pending_row["settled_pnl_usd"] is None
        assert pending_row["settled_at"] is None

    def test_no_side_position_wins_when_yes_loses(self):
        db = Database(":memory:")
        position_id = _seed_position(db, market="0xmarket", outcome_index=1, entry_price=0.30, stake_usd=20.0)

        with patch("src.scripts.copy_settle.fetch_market_resolution", return_value=False):
            summary = run_once(db=db)

        assert summary == {"settled": 1, "pending": 0, "errors": 0}
        row = db._conn.execute("SELECT * FROM copy_positions WHERE id=?", (position_id,)).fetchone()
        assert row["status"] == "settled"
        # outcome_index=1 (NO) wins when yes_won=False -> pnl = 20 * (1-0.3)/0.3
        assert round(row["settled_pnl_usd"], 4) == round(20.0 * (1 - 0.30) / 0.30, 4)


class TestRunOnceSharedMarketDedup:
    def test_two_positions_same_market_resolve_once(self):
        """Two open positions sharing the same market -> fetch_market_resolution
        is called exactly once for that market, not twice."""
        db = Database(":memory:")
        _seed_position(db, market="0xsharedmarket", outcome_index=0)
        _seed_position(db, market="0xsharedmarket", outcome_index=1, address="0xwallet2")

        with patch("src.scripts.copy_settle.fetch_market_resolution", return_value=True) as mock_resolve:
            summary = run_once(db=db)

        mock_resolve.assert_called_once_with("0xsharedmarket")
        assert summary["settled"] == 2


class TestRunOncePerRowIsolation:
    def test_one_bad_row_does_not_prevent_others_from_settling(self):
        """A row whose settlement raises (simulated DB error) must not
        prevent the remaining rows in the same run from being processed."""
        db = Database(":memory:")
        bad_id = _seed_position(db, market="0xbadmarket", outcome_index=0)
        good_id = _seed_position(db, market="0xgoodmarket", outcome_index=0)

        real_settle_copy_position = db.settle_copy_position

        def flaky_settle(position_id, settled_pnl_usd, settled_at):
            if position_id == bad_id:
                raise RuntimeError("simulated DB error")
            return real_settle_copy_position(position_id, settled_pnl_usd, settled_at)

        db.settle_copy_position = flaky_settle

        with patch("src.scripts.copy_settle.fetch_market_resolution", return_value=True):
            summary = run_once(db=db)

        assert summary == {"settled": 1, "pending": 0, "errors": 1}

        bad_row = db._conn.execute("SELECT * FROM copy_positions WHERE id=?", (bad_id,)).fetchone()
        assert bad_row["status"] == "open"

        good_row = db._conn.execute("SELECT * FROM copy_positions WHERE id=?", (good_id,)).fetchone()
        assert good_row["status"] == "settled"


class TestRunOnceNoDb:
    def test_db_unavailable_returns_zero_summary(self):
        with patch("src.scripts.copy_settle._open_db", return_value=None):
            summary = run_once()
        assert summary == {"settled": 0, "pending": 0, "errors": 0}


class TestRunOnceStaleTracking:
    """Test the fallback stale-position warning (issue #1218).

    When positions remain unresolved for >48 hours, emit ONE aggregate warning
    per run reporting the count. This prevents per-market log floods while
    surfacing genuine settlement delays.
    """

    def test_two_stale_positions_emit_exactly_one_warning(self, caplog):
        """Two positions >48h old and unresolved -> EXACTLY ONE warning line
        reporting the count of 2. The 'exactly one' assertion is critical:
        per-market logging (the bug being fixed) would emit two lines."""
        import logging
        from datetime import datetime, timedelta, timezone

        db = Database(":memory:")
        old_now = datetime.now(timezone.utc)
        stale_time = (old_now - timedelta(hours=56)).isoformat()

        # Two positions, both >48h old, both unresolved
        _seed_position(db, market="0xmarket1", outcome_index=0, entry_price=0.40, stake_usd=10.0)
        db._conn.execute(
            "UPDATE copy_positions SET entry_ts=? WHERE market=?",
            (stale_time, "0xmarket1")
        )
        db._conn.commit()

        _seed_position(db, market="0xmarket2", outcome_index=0, entry_price=0.40, stake_usd=10.0)
        db._conn.execute(
            "UPDATE copy_positions SET entry_ts=? WHERE market=?",
            (stale_time, "0xmarket2")
        )
        db._conn.commit()

        with patch("src.scripts.copy_settle.fetch_market_resolution", return_value=None):
            with caplog.at_level(logging.WARNING, logger="src.scripts.copy_settle"):
                # Mock datetime.now to match the stale_time scenario
                with patch("src.scripts.copy_settle.datetime") as mock_dt:
                    mock_dt.now.return_value = old_now
                    mock_dt.fromisoformat = datetime.fromisoformat
                    mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
                    summary = run_once(db=db)

        # Find all WARNING logs about stale positions
        stale_warnings = [r for r in caplog.records
                            if r.levelno == logging.WARNING and "unresolved for > 48 hours" in r.message]

        # EXACTLY one aggregate warning, not two
        assert len(stale_warnings) == 1, f"Expected exactly 1 stale warning, got {len(stale_warnings)}"
        assert "2 open position(s)" in stale_warnings[0].message

        # Settlement counts unaffected
        assert summary["pending"] == 2
        assert summary["settled"] == 0

    def test_no_warning_when_all_positions_under_48h(self, caplog):
        """Positions unresolved but <48h old -> no stale warning."""
        import logging
        from datetime import datetime, timedelta, timezone

        db = Database(":memory:")
        old_now = datetime.now(timezone.utc)
        fresh_time = (old_now - timedelta(hours=24)).isoformat()

        _seed_position(db, market="0xfreshmarket", outcome_index=0)
        db._conn.execute(
            "UPDATE copy_positions SET entry_ts=? WHERE market=?",
            (fresh_time, "0xfreshmarket")
        )
        db._conn.commit()

        with patch("src.scripts.copy_settle.fetch_market_resolution", return_value=None):
            with caplog.at_level(logging.WARNING, logger="src.scripts.copy_settle"):
                with patch("src.scripts.copy_settle.datetime") as mock_dt:
                    mock_dt.now.return_value = old_now
                    mock_dt.fromisoformat = datetime.fromisoformat
                    mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
                    summary = run_once(db=db)

        stale_warnings = [r for r in caplog.records
                            if r.levelno == logging.WARNING and "unresolved for > 48 hours" in r.message]
        assert len(stale_warnings) == 0

    def test_no_warning_when_no_open_positions(self, caplog):
        """No open positions -> no stale warning, no crash."""
        import logging

        db = Database(":memory:")

        with caplog.at_level(logging.WARNING, logger="src.scripts.copy_settle"):
            summary = run_once(db=db)

        assert summary["pending"] == 0
        stale_warnings = [r for r in caplog.records
                            if r.levelno == logging.WARNING and "unresolved for > 48 hours" in r.message]
        assert len(stale_warnings) == 0

    def test_malformed_entry_ts_skipped_with_debug_log(self, caplog):
        """Row with malformed entry_ts -> skip stale tracking, log DEBUG,
        run completes, other rows still counted."""
        import logging

        db = Database(":memory:")

        # Good position
        good_id = _seed_position(db, market="0xgoodmarket", outcome_index=0)

        # Bad position with malformed entry_ts
        bad_signal_id = db.insert_copy_signal(
            address=ADDRESS, market="0xbadmarket", source_price=0.40,
            detected_at=NOW_ISO, outcome_index=0,
        )
        bad_position_id = db.insert_copy_position(
            signal_id=bad_signal_id, address=ADDRESS, market="0xbadmarket",
            outcome_index=0, entry_price=0.40, stake_usd=10.0,
            entry_ts="not-a-valid-iso-timestamp",  # Malformed
        )
        db.link_copy_signal_to_position(bad_signal_id, bad_position_id)

        with patch("src.scripts.copy_settle.fetch_market_resolution", return_value=None):
            with caplog.at_level(logging.DEBUG, logger="src.scripts.copy_settle"):
                summary = run_once(db=db)

        # Both positions stay pending
        assert summary["pending"] == 2
        assert summary["errors"] == 0

        # Debug log recorded for malformed entry_ts
        debug_logs = [r for r in caplog.records
                        if r.levelno == logging.DEBUG and "malformed entry_ts" in r.message]
        assert len(debug_logs) == 1
        assert str(bad_position_id) in debug_logs[0].message

    def test_settlement_counts_unchanged_with_stale_rows(self):
        """Settled/pending counts identical with and without stale rows present."""
        from datetime import datetime, timedelta, timezone

        db = Database(":memory:")
        old_now = datetime.now(timezone.utc)
        stale_time = (old_now - timedelta(hours=56)).isoformat()

        # Mix of stale unresolved and fresh resolved
        stale_unresolved_id = _seed_position(db, market="0xstale", outcome_index=0)
        db._conn.execute(
            "UPDATE copy_positions SET entry_ts=? WHERE id=?",
            (stale_time, stale_unresolved_id)
        )
        db._conn.commit()

        fresh_resolved_id = _seed_position(db, market="0xfresh", outcome_index=0)

        def fake_resolve(market):
            return {
                "0xstale": None,   # Unresolved (stale)
                "0xfresh": True,   # Resolved
            }.get(market)

        with patch("src.scripts.copy_settle.fetch_market_resolution", side_effect=fake_resolve):
            summary = run_once(db=db)

        # Counts should match the actual state (1 settled, 1 pending)
        # stale warning does NOT affect settled/pending counts
        assert summary["settled"] == 1
        assert summary["pending"] == 1
        assert summary["errors"] == 0
