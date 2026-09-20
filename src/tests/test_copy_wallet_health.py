"""Unit tests for src/scripts/copy_wallet_health.py (epic #1138 story D2,
issue #1140). Uses a real in-memory Database (seeded rows), mirroring
test_copy_settle.py's `Database(":memory:")` pattern -- no mocking of the DB
itself, no network calls (this script makes none).
"""
from src.data.db import Database
from src.scripts.copy_wallet_health import (
    MIN_SETTLED_TRADES_FOR_ROI_CHECK,
    run_once,
)

ADDRESS = "0xwallet1"
ADDED_AT = "2026-09-01T00:00:00+00:00"


def _db() -> Database:
    return Database(":memory:")


def _follow(db: Database, address: str = ADDRESS, status: str = "active", **overrides) -> None:
    db.insert_followed_wallet(
        address=address, stake_per_trade=5.0, added_at=ADDED_AT, status=status,
    )
    if overrides:
        # Only used to seed an already-paused wallet's paused_reason, since
        # insert_followed_wallet doesn't take one directly.
        db.update_followed_wallet_status(address, status, overrides.get("paused_reason"))


def _screening_row(
    db: Database, address: str, screened_at: str, median_roi: float, n_resolved: int,
    eligible_to_follow: int,
) -> None:
    db.insert_wallet_screening(
        address=address, window="month", screened_at=screened_at,
        n_buy_trades=n_resolved, n_resolved=n_resolved, win_rate=0.5,
        mean_roi=median_roi, median_roi=median_roi, slippage_bps=50.0,
        eligible_to_follow=eligible_to_follow,
    )


def _settled_position(db: Database, address: str, pnl: float, stake_usd: float = 10.0, market: str = "0xmarket") -> int:
    signal_id = db.insert_copy_signal(
        address=address, market=market, source_price=0.4,
        detected_at="2026-09-19T00:00:00+00:00",
    )
    position_id = db.insert_copy_position(
        signal_id=signal_id, address=address, market=market, outcome_index=0,
        entry_price=0.4, stake_usd=stake_usd, entry_ts="2026-09-19T00:00:00+00:00",
    )
    db.settle_copy_position(position_id, pnl, "2026-09-20T00:00:00+00:00")
    return position_id


class TestRunOnceNoWallets:
    def test_no_active_wallets_returns_zero_summary(self):
        db = _db()
        summary = run_once(db=db)
        assert summary == {"checked": 0, "paused_stability": 0, "paused_roi": 0}


class TestStabilityCheckPause:
    def test_two_rows_failing_check_stability_pause_the_wallet(self):
        db = _db()
        _follow(db)
        # median_roi sign reversal between the two most recent runs ->
        # check_stability returns False.
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.3, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=-0.1, n_resolved=100, eligible_to_follow=0)

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 1, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "paused"
        assert row["paused_reason"] == "stability_check_failed"

    def test_no_screening_history_is_left_alone(self):
        db = _db()
        _follow(db)

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "active"


class TestLatestRowIneligiblePause:
    def test_pairwise_stable_but_latest_eligible_to_follow_zero_still_pauses(self):
        """check_stability would pass pairwise (same sign, volume within
        tolerance) but the latest row's own eligible_to_follow=0 -- the
        acceptance-criteria case that must not be missed by only checking
        pairwise agreement."""
        db = _db()
        _follow(db)
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=105, eligible_to_follow=0)

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 1, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "paused"
        assert row["paused_reason"] == "stability_check_failed"


class TestRealizedRoiPause:
    def test_negative_median_roi_with_enough_settled_trades_pauses(self):
        db = _db()
        _follow(db)
        # Stable screening history so the stability check never fires first.
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        # 5 settled trades (>= MIN_SETTLED_TRADES_FOR_ROI_CHECK), median
        # per-trade ROI = -0.2 (negative).
        for pnl in (-2.0, -2.0, -2.0, 1.0, 1.0):
            _settled_position(db, ADDRESS, pnl, stake_usd=10.0)

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 1}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "paused"
        assert row["paused_reason"] == "realized_roi_negative"


class TestMinimumSampleSizeGuard:
    def test_negative_median_roi_below_minimum_sample_is_not_paused(self):
        db = _db()
        _follow(db)
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        n_below_minimum = MIN_SETTLED_TRADES_FOR_ROI_CHECK - 1
        assert n_below_minimum > 0
        for _ in range(n_below_minimum):
            _settled_position(db, ADDRESS, -5.0, stake_usd=10.0)

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "active"


class TestMalformedRowGuard:
    """AI review #1141, BLOCK item: a settled row with a NULL
    settled_pnl_usd must be excluded from the ROI sample rather than
    crashing the run with a TypeError. The copy_positions schema's own
    writers (settle_copy_position) never produce such a row -- this
    simulates one via a direct UPDATE, bypassing settle_copy_position, to
    prove the defensive filter actually works rather than assuming it."""

    def test_settled_row_with_null_pnl_is_excluded_not_crashed_on(self):
        db = _db()
        _follow(db)
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        # MIN_SETTLED_TRADES_FOR_ROI_CHECK usable rows, all positive ROI.
        for _ in range(MIN_SETTLED_TRADES_FOR_ROI_CHECK):
            _settled_position(db, ADDRESS, 1.0, stake_usd=10.0)

        # One extra row hand-crafted to carry status='settled' with a NULL
        # settled_pnl_usd -- settle_copy_position never produces this; it's
        # a stand-in for "a malformed row exists somehow."
        signal_id = db.insert_copy_signal(
            address=ADDRESS, market="0xbadrow", source_price=0.4,
            detected_at="2026-09-19T00:00:00+00:00",
        )
        position_id = db.insert_copy_position(
            signal_id=signal_id, address=ADDRESS, market="0xbadrow",
            outcome_index=0, entry_price=0.4, stake_usd=10.0,
            entry_ts="2026-09-19T00:00:00+00:00",
        )
        db._conn.execute(
            "UPDATE copy_positions SET status='settled', settled_at=? WHERE id=?",
            ("2026-09-20T00:00:00+00:00", position_id),
        )
        db._conn.commit()

        # Must not raise; the malformed row is excluded from the sample,
        # leaving exactly MIN_SETTLED_TRADES_FOR_ROI_CHECK usable positive-ROI
        # rows, so the wallet stays active.
        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "active"


class TestWalletPassingBothChecks:
    def test_stays_active_untouched(self):
        db = _db()
        _follow(db)
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        for pnl in (1.0, 1.0, 1.0, 1.0, 1.0):
            _settled_position(db, ADDRESS, pnl, stake_usd=10.0)

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "active"
        assert row["paused_reason"] is None


class TestAlreadyPausedWalletIsSkipped:
    def test_already_paused_wallet_is_not_re_evaluated_or_overwritten(self):
        db = _db()
        _follow(db, status="paused", paused_reason="manual_review")
        # Would otherwise trip the stability check if it were evaluated.
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.3, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=-0.1, n_resolved=100, eligible_to_follow=0)
        # Would otherwise trip the ROI check if it were evaluated.
        for _ in range(MIN_SETTLED_TRADES_FOR_ROI_CHECK):
            _settled_position(db, ADDRESS, -5.0, stake_usd=10.0)

        summary = run_once(db=db)

        assert summary == {"checked": 0, "paused_stability": 0, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "paused"
        assert row["paused_reason"] == "manual_review"


class TestMultipleWalletsSummary:
    def test_mixed_outcomes_across_wallets_tally_correctly(self):
        db = _db()
        _follow(db, address="0xstable")
        _screening_row(db, "0xstable", "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, "0xstable", "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        _follow(db, address="0xunstable")
        _screening_row(db, "0xunstable", "2026-09-18T00:00:00+00:00", median_roi=0.30, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, "0xunstable", "2026-09-19T00:00:00+00:00", median_roi=-0.1, n_resolved=100, eligible_to_follow=0)

        _follow(db, address="0xlosing")
        _screening_row(db, "0xlosing", "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, "0xlosing", "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        for _ in range(MIN_SETTLED_TRADES_FOR_ROI_CHECK):
            _settled_position(db, "0xlosing", -1.0, stake_usd=10.0, market="0xlosingmarket")

        summary = run_once(db=db)

        assert summary == {"checked": 3, "paused_stability": 1, "paused_roi": 1}
        statuses = {w["address"]: w["status"] for w in db.get_followed_wallets()}
        assert statuses["0xstable"] == "active"
        assert statuses["0xunstable"] == "paused"
        assert statuses["0xlosing"] == "paused"


class TestRunOnceNoDb:
    def test_db_unavailable_returns_zero_summary(self):
        from unittest.mock import patch
        with patch("src.scripts.copy_wallet_health._open_db", return_value=None):
            summary = run_once()
        assert summary == {"checked": 0, "paused_stability": 0, "paused_roi": 0}
