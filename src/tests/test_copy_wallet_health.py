"""Unit tests for src/scripts/copy_wallet_health.py (epic #1138 story D2,
issue #1140; realized-P&L rework issue #1225). Uses a real in-memory
Database (seeded rows), mirroring test_copy_settle.py's `Database(":memory:")`
pattern -- no mocking of the DB itself, no network calls (this script makes
none).
"""
from src.data.db import Database
from src.scripts.copy_wallet_health import (
    MIN_DECISIONS_FOR_ROI_CHECK,
    dedupe_decisions,
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


def _settled_position(
    db: Database, address: str, pnl: float, stake_usd: float = 10.0,
    market: str = "0xmarket", outcome_index: int = 0, entry_price: float = 0.4,
) -> int:
    """Insert one settled ``copy_positions`` row (one fill). Fills sharing
    the same ``(market, outcome_index)`` collapse into a single deduped
    decision -- see ``_settled_decisions`` for the common "N distinct
    decisions" case this module's threshold actually counts.
    """
    signal_id = db.insert_copy_signal(
        address=address, market=market, source_price=entry_price,
        detected_at="2026-09-19T00:00:00+00:00",
    )
    position_id = db.insert_copy_position(
        signal_id=signal_id, address=address, market=market, outcome_index=outcome_index,
        entry_price=entry_price, stake_usd=stake_usd, entry_ts="2026-09-19T00:00:00+00:00",
    )
    db.settle_copy_position(position_id, pnl, "2026-09-20T00:00:00+00:00")
    return position_id


def _settled_decisions(
    db: Database, address: str, pnls: "list[float]", stake_usd: float = 10.0,
    entry_price: float = 0.4, market_prefix: str = "0xdecision",
) -> None:
    """Insert one settled position per entry in *pnls*, each on its own
    distinct ``(market, outcome_index)`` -- i.e. each pnl is exactly one
    deduped decision, matching what ``COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK``
    actually counts (issue #1225)."""
    for i, pnl in enumerate(pnls):
        _settled_position(
            db, address, pnl, stake_usd=stake_usd, entry_price=entry_price,
            market=f"{market_prefix}{i}",
        )


class _RaisingConfigDb:
    """Wraps a real ``Database`` but makes ``get_all_config`` raise, to
    exercise ``_realized_pnl_pause_reason``'s module-constant fallback path
    when the live-config lookup itself cannot be completed (issue #1225's
    "module fallback used when no DB" requirement)."""

    def __init__(self, db: Database):
        self._db = db

    def get_all_config(self):
        raise RuntimeError("simulated live-config failure")

    def __getattr__(self, name):
        return getattr(self._db, name)


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


class TestRealizedPnlPause:
    """The pause signal is total realized P&L over deduped decisions, gated
    on ``MIN_DECISIONS_FOR_ROI_CHECK`` (default 30) deduped decisions --
    issue #1225."""

    def test_negative_total_pnl_at_threshold_pauses(self):
        db = _db()
        _follow(db)
        # Stable screening history so the stability check never fires first.
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        # Exactly MIN_DECISIONS_FOR_ROI_CHECK deduped decisions, total P&L
        # negative.
        _settled_decisions(db, ADDRESS, [-1.0] * MIN_DECISIONS_FOR_ROI_CHECK)

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 1}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "paused"
        assert row["paused_reason"] == "realized_roi_negative"

    def test_positive_total_pnl_at_threshold_is_not_paused(self):
        db = _db()
        _follow(db)
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        _settled_decisions(db, ADDRESS, [1.0] * MIN_DECISIONS_FOR_ROI_CHECK)

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "active"


class TestProfitableLowWinRateWalletIsNotPaused:
    """Pins defect #3 from issue #1225: median per-trade ROI is a win-rate
    check wearing an ROI label. A wallet buying at 0.25 with a 40% hit rate
    (12 winners of 30) is net solidly profitable (+$90) even though its
    median trade -- and therefore the OLD median-ROI signal -- is a loss.
    Must NOT be paused."""

    def test_profitable_low_win_rate_wallet_not_paused_on_total_pnl(self):
        db = _db()
        _follow(db)
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        winners = [15.0] * 12
        losers = [-5.0] * 18
        pnls = winners + losers
        assert len(pnls) == 30 == MIN_DECISIONS_FOR_ROI_CHECK
        assert sum(pnls) == 90.0  # net profitable ...
        # ... yet more than half the decisions are losses, so the median
        # per-trade pnl (the OLD signal) is negative:
        sorted_pnls = sorted(pnls)
        median_pnl = sorted_pnls[len(sorted_pnls) // 2]
        assert median_pnl < 0

        _settled_decisions(db, ADDRESS, pnls, stake_usd=5.0, entry_price=0.25)

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "active"
        assert row["paused_reason"] is None


class TestMultiFillClusterCountsAsOneDecision:
    """Pins defect #2 from issue #1225 (``0x684baa57c3``'s real shape): a
    single signal split across several fills must count as ONE decision,
    not one per fill. 5 fills of the same (market, outcome_index) must
    collapse to 1 deduped decision -- nowhere near
    MIN_DECISIONS_FOR_ROI_CHECK (30) -- even though the pre-#1225 raw-row
    count (5) used to be enough to trip the old
    MIN_SETTLED_TRADES_FOR_ROI_CHECK=5 rule on arrival."""

    def test_five_fills_of_one_market_outcome_is_one_decision_not_paused(self):
        db = _db()
        _follow(db)
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        # 5 fills, same market and outcome_index -> 1 deduped decision.
        for _ in range(5):
            _settled_position(db, ADDRESS, -5.0, stake_usd=10.0, market="0xmarket", outcome_index=0)

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "active"


class TestDedupeDecisionsSettledAtOrdering:
    """``dedupe_decisions``'s ``settled_at`` pick must compare PARSED
    timestamps, not raw strings -- a raw-string compare orders a
    ``Z``-suffixed row incorrectly against a ``+00:00``-suffixed one for
    the same instant (``'Z' > '+'`` lexicographically), and treats a naive
    (offset-less) timestamp as just another string rather than UTC."""

    def _settle(self, db, pnl, settled_at):
        signal_id = db.insert_copy_signal(
            address=ADDRESS, market="0xmarket", source_price=0.4, detected_at=settled_at,
        )
        position_id = db.insert_copy_position(
            signal_id=signal_id, address=ADDRESS, market="0xmarket", outcome_index=0,
            entry_price=0.4, stake_usd=10.0, entry_ts=settled_at,
        )
        db.settle_copy_position(position_id, pnl, settled_at)

    def test_z_suffixed_row_is_not_lexicographically_misordered_against_offset_form(self):
        db = _db()
        # A raw-string compare would say "2026-09-29T00:00:00+00:00" > any
        # "...Z" string, even though the Z-suffixed row below is actually
        # LATER in real time.
        self._settle(db, 1.0, "2026-09-29T00:00:00+00:00")
        self._settle(db, 1.0, "2026-09-30T00:00:00Z")

        [decision] = dedupe_decisions(db.get_settled_copy_positions(ADDRESS))

        assert decision["pnl"] == 2.0
        assert decision["settled_at"] == "2026-09-30T00:00:00Z"

    def test_naive_settled_at_can_still_win_as_latest(self):
        db = _db()
        self._settle(db, 1.0, "2026-09-29T00:00:00+00:00")
        self._settle(db, 1.0, "2026-09-30T00:00:00")  # naive, but later

        [decision] = dedupe_decisions(db.get_settled_copy_positions(ADDRESS))

        assert decision["settled_at"] == "2026-09-30T00:00:00"


class TestMinimumSampleSizeGuard:
    def test_below_minimum_decisions_with_negative_total_pnl_is_not_paused(self):
        db = _db()
        _follow(db)
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        n_below_minimum = MIN_DECISIONS_FOR_ROI_CHECK - 1
        assert n_below_minimum > 0
        _settled_decisions(db, ADDRESS, [-5.0] * n_below_minimum)

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "active"


class TestThresholdHonouredFromLiveConfig:
    """Threshold is live-editable via bot_config
    (``COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK``), read through
    ``get_live_config`` -- not the module-constant fallback -- whenever the
    DB is available."""

    def test_lowered_threshold_from_config_pauses_below_default(self):
        db = _db()
        db.set_config("COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK", "3")
        _follow(db)
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        # Only 3 decisions -- far below the default (30) but at the
        # overridden threshold -- negative total pnl.
        _settled_decisions(db, ADDRESS, [-1.0, -1.0, -1.0])

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 1}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "paused"
        assert row["paused_reason"] == "realized_roi_negative"

    def test_below_overridden_threshold_is_not_paused(self):
        db = _db()
        db.set_config("COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK", "3")
        _follow(db)
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        _settled_decisions(db, ADDRESS, [-1.0, -1.0])

        summary = run_once(db=db)

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 0}
        row = db.get_followed_wallets()[0]
        assert row["status"] == "active"


class TestModuleFallbackThresholdWhenLiveConfigUnavailable:
    """If ``get_live_config`` itself cannot be completed, fall back to the
    module constant (issue #1225's "module fallback for the no-DB path")."""

    def test_fallback_threshold_used_when_live_config_lookup_fails(self):
        real_db = _db()
        _follow(real_db)
        _screening_row(real_db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(real_db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _settled_decisions(real_db, ADDRESS, [-1.0] * MIN_DECISIONS_FOR_ROI_CHECK)

        summary = run_once(db=_RaisingConfigDb(real_db))

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 1}
        row = real_db.get_followed_wallets()[0]
        assert row["status"] == "paused"
        assert row["paused_reason"] == "realized_roi_negative"

    def test_fallback_logged_at_warning_not_silent(self, caplog):
        """AI review #1225 BLOCK item: a broken live-config read must not
        silently change the auto-pause threshold -- it must be logged at
        WARNING."""
        import logging

        real_db = _db()
        _follow(real_db)
        _screening_row(real_db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(real_db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _settled_decisions(real_db, ADDRESS, [1.0] * MIN_DECISIONS_FOR_ROI_CHECK)

        with caplog.at_level(logging.WARNING, logger="src.scripts.copy_wallet_health"):
            run_once(db=_RaisingConfigDb(real_db))

        assert any(
            "COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_non_numeric_config_value_falls_back_and_does_not_crash(self):
        """AI review #1225 BLOCK item: a non-numeric value that somehow
        reaches the threshold read (bypassing get_live_config's own
        coercion, e.g. via a hand-edited bot_config row) must fall back to
        the module constant, not raise a TypeError on the `<` comparison."""
        real_db = _db()
        _follow(real_db)
        _screening_row(real_db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(real_db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _settled_decisions(real_db, ADDRESS, [-1.0] * MIN_DECISIONS_FOR_ROI_CHECK)

        class _NonNumericThresholdDb:
            def __init__(self, db):
                self._db = db

            def get_all_config(self):
                return {"COPY_HEALTH_MIN_DECISIONS_FOR_ROI_CHECK": "not-a-number"}

            def __getattr__(self, name):
                return getattr(self._db, name)

        # get_live_config's own int() coercion of "not-a-number" already
        # falls back to CONFIG_DEFAULTS (30) internally; this asserts the
        # whole path is crash-proof end-to-end regardless of where the
        # coercion happens.
        summary = run_once(db=_NonNumericThresholdDb(real_db))

        assert summary == {"checked": 1, "paused_stability": 0, "paused_roi": 1}
        row = real_db.get_followed_wallets()[0]
        assert row["status"] == "paused"
        assert row["paused_reason"] == "realized_roi_negative"


class TestMalformedRowGuard:
    """AI review #1141, BLOCK item: a settled row with a NULL
    settled_pnl_usd must be excluded from the sample rather than crashing
    the run. The copy_positions schema's own writers (settle_copy_position)
    never produce such a row -- this simulates one via a direct UPDATE,
    bypassing settle_copy_position, to prove the defensive filter actually
    works rather than assuming it."""

    def test_settled_row_with_null_pnl_is_excluded_not_crashed_on(self):
        db = _db()
        _follow(db)
        _screening_row(db, ADDRESS, "2026-09-18T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)
        _screening_row(db, ADDRESS, "2026-09-19T00:00:00+00:00", median_roi=0.10, n_resolved=100, eligible_to_follow=1)

        # MIN_DECISIONS_FOR_ROI_CHECK usable decisions, all positive pnl.
        _settled_decisions(db, ADDRESS, [1.0] * MIN_DECISIONS_FOR_ROI_CHECK)

        # One extra row hand-crafted to carry status='settled' with a NULL
        # settled_pnl_usd -- settle_copy_position never produces this; it's
        # a stand-in for "a malformed row exists somehow." Its own distinct
        # market keeps it from silently merging into a good decision.
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
        # leaving exactly MIN_DECISIONS_FOR_ROI_CHECK usable positive-pnl
        # decisions, so the wallet stays active.
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
        _settled_decisions(db, ADDRESS, [1.0] * MIN_DECISIONS_FOR_ROI_CHECK)

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
        # Would otherwise trip the realized-P&L check if it were evaluated.
        _settled_decisions(db, ADDRESS, [-5.0] * MIN_DECISIONS_FOR_ROI_CHECK)

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
        _settled_decisions(
            db, "0xlosing", [-1.0] * MIN_DECISIONS_FOR_ROI_CHECK, market_prefix="0xlosingmarket",
        )

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
