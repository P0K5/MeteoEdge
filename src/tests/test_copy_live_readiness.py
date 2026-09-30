"""Unit + integration tests for src/scripts/copy_live_readiness.py (issue
#1255). Uses a real in-memory Database (seeded rows), mirroring
test_copy_wallet_health.py's ``Database(":memory:")`` pattern -- no
mocking of the DB itself, no network calls (this script makes none).
"""
import json
from datetime import datetime, timezone

from src.data.db import Database
from src.scripts.copy_live_readiness import (
    GATE_MAX_CONCENTRATION,
    GATE_MIN_DECISIONS,
    LAST_N_DAYS,
    _decisions_needed_for_positive_ci,
    _parse_ts,
    _stats,
    build_report,
    evaluate_wallet,
    main,
)

ADDRESS = "0xwallet1"
ADDED_AT = "2026-09-01T00:00:00+00:00"

# Fixed "now" so last-N-day windowing is deterministic across the suite.
NOW = datetime.fromisoformat("2026-09-30T12:00:00+00:00")
RECENT_TS = "2026-09-29T00:00:00+00:00"  # within LAST_N_DAYS of NOW
OLD_TS = "2026-08-01T00:00:00+00:00"     # well outside LAST_N_DAYS of NOW


def _db() -> Database:
    return Database(":memory:")


def _follow(db: Database, address: str = ADDRESS, status: str = "active", paused_reason=None) -> None:
    db.insert_followed_wallet(address=address, stake_per_trade=5.0, added_at=ADDED_AT, status=status)
    if status == "paused" or paused_reason is not None:
        db.update_followed_wallet_status(address, status, paused_reason)


def _settled_position(
    db: Database, address: str, pnl: float, *, market: str, outcome_index: int = 0,
    stake_usd: float = 10.0, entry_price: float = 0.4, settled_at: str = RECENT_TS,
) -> int:
    """Insert one settled ``copy_positions`` row (one fill)."""
    signal_id = db.insert_copy_signal(
        address=address, market=market, source_price=entry_price, detected_at=settled_at,
    )
    position_id = db.insert_copy_position(
        signal_id=signal_id, address=address, market=market, outcome_index=outcome_index,
        entry_price=entry_price, stake_usd=stake_usd, entry_ts=settled_at,
    )
    db.settle_copy_position(position_id, pnl, settled_at)
    return position_id


def _settled_decisions(
    db: Database, address: str, pnls: "list[float]", *, settled_at: str = RECENT_TS,
    market_prefix: str = "0xdecision", stake_usd: float = 10.0,
) -> None:
    """One settled position per entry in *pnls*, each on its own distinct
    ``(market, outcome_index)`` -- i.e. each pnl is exactly one deduped
    decision."""
    for i, pnl in enumerate(pnls):
        _settled_position(
            db, address, pnl, market=f"{market_prefix}{i}", stake_usd=stake_usd,
            settled_at=settled_at,
        )


class TestParseTsNaiveTimestamps:
    """Regression: a naive (offset-less) ``settled_at`` must not crash the
    report -- ``_parse_ts`` is compared against an aware ``cutoff``
    elsewhere, and comparing naive vs aware datetimes raises TypeError if
    the naive value isn't first made aware."""

    def test_naive_timestamp_is_treated_as_utc_not_returned_naive(self):
        dt = _parse_ts("2026-09-29T00:00:00")  # no offset
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt == datetime(2026, 9, 29, tzinfo=timezone.utc)

    def test_aware_timestamp_is_left_as_is(self):
        dt = _parse_ts("2026-09-29T00:00:00+00:00")
        assert dt == datetime(2026, 9, 29, tzinfo=timezone.utc)

    def test_unparseable_timestamp_returns_none(self):
        assert _parse_ts("not-a-timestamp") is None

    def test_none_and_empty_return_none(self):
        assert _parse_ts(None) is None
        assert _parse_ts("") is None

    def test_report_completes_rather_than_raising_on_a_naive_settled_at(self):
        """Was: TypeError: can't compare offset-naive and offset-aware
        datetimes, raised out of build_report -> evaluate_wallet's last-7d
        filter the moment one settled row had a naive timestamp."""
        db = _db()
        _follow(db)
        _settled_position(
            db, ADDRESS, 2.0, market="0xmarket", outcome_index=0,
            settled_at="2026-09-29T00:00:00",  # naive -- no UTC offset
        )

        report = build_report(db, now=NOW)  # must not raise

        assert len(report) == 1
        # The naive-but-recent decision is still correctly bucketed into
        # the last-7-day window, not silently dropped.
        assert report[0]["last_7d"]["n"] == 1


class TestStatsHelper:
    def test_empty_values_has_none_mean(self):
        s = _stats([])
        assert s == {"n": 0, "total": 0.0, "mean": None, "sd": None, "ci_lower": None, "ci_upper": None}

    def test_single_value_has_no_sd_or_ci(self):
        s = _stats([5.0])
        assert s["n"] == 1
        assert s["mean"] == 5.0
        assert s["sd"] is None
        assert s["ci_lower"] is None
        assert s["ci_upper"] is None

    def test_two_identical_values_have_zero_sd_and_tight_ci(self):
        s = _stats([2.0, 2.0])
        assert s["mean"] == 2.0
        assert s["sd"] == 0.0
        assert s["ci_lower"] == s["ci_upper"] == 2.0


class TestDecisionsNeededEstimate:
    def test_non_positive_mean_returns_none(self):
        assert _decisions_needed_for_positive_ci(0.0, 1.0) is None
        assert _decisions_needed_for_positive_ci(-1.0, 1.0) is None

    def test_unknown_sd_returns_none(self):
        assert _decisions_needed_for_positive_ci(1.0, None) is None

    def test_zero_sd_needs_just_one(self):
        assert _decisions_needed_for_positive_ci(1.0, 0.0) == 1

    def test_positive_mean_and_sd_returns_a_finite_count(self):
        n = _decisions_needed_for_positive_ci(1.0, 5.0)
        assert isinstance(n, int) and n > 1


class TestMultiFillClusterCountsAsOneDecision:
    """3 fills of one market collapse into ONE decision (same dedupe as
    copy_wallet_health.py, reused via dedupe_decisions -- not re-derived
    here)."""

    def test_three_fills_of_one_market_is_one_decision(self):
        db = _db()
        _follow(db)
        for pnl in (2.0, -1.0, 3.0):
            _settled_position(db, ADDRESS, pnl, market="0xmarket", outcome_index=0)

        report = build_report(db, now=NOW)

        assert len(report) == 1
        entry = report[0]
        assert entry["all_time"]["n"] == 1
        assert entry["all_time"]["total"] == 4.0  # 2 - 1 + 3, one decision


class TestCorrelatedDuplicatesDoNotNarrowTheCI:
    """5 correlated fills of the SAME decision must never be treated as 5
    independent samples -- that would spuriously narrow the CI. Deduping
    first collapses them to n=1 (sd/CI undefined), which is the honest
    answer, not a fabricated tight interval."""

    def test_five_fills_of_one_decision_yields_undefined_ci_not_a_narrow_one(self):
        db = _db()
        _follow(db)
        for _ in range(5):
            _settled_position(db, ADDRESS, 2.0, market="0xmarket", outcome_index=0)

        report = build_report(db, now=NOW)[0]

        assert report["all_time"]["n"] == 1  # not 5
        assert report["all_time"]["sd"] is None
        assert report["all_time"]["ci_lower"] is None


class TestMinimumDecisionsGate:
    def test_fewer_than_gate_minimum_decisions_fails_min_decisions(self):
        db = _db()
        _follow(db)
        _settled_decisions(db, ADDRESS, [5.0] * (GATE_MIN_DECISIONS - 1))

        report = build_report(db, now=NOW)[0]

        assert report["verdict"] == "FAIL"
        assert report["failing_condition"] == "min_decisions"


class TestConcentrationGate:
    def test_one_dominant_decision_fails_concentration_even_with_enough_volume(self):
        db = _db()
        _follow(db)
        # 149 small, steady winners + 1 big winner that dwarfs the rest --
        # enough decisions and a positive all-time/7d mean, but > 25% of
        # total PnL comes from a single decision.
        pnls = [1.0] * (GATE_MIN_DECISIONS - 1) + [100.0]
        assert len(pnls) == GATE_MIN_DECISIONS
        _settled_decisions(db, ADDRESS, pnls, settled_at=RECENT_TS)

        report = build_report(db, now=NOW)[0]

        assert report["all_time"]["n"] == GATE_MIN_DECISIONS
        assert report["concentration"] > GATE_MAX_CONCENTRATION
        assert report["verdict"] == "FAIL"
        assert report["failing_condition"] == "concentration"


class TestLast7DayMeanGate:
    def test_negative_recent_mean_fails_last_7d_mean_despite_good_all_time_track_record(self):
        db = _db()
        _follow(db)
        # Plenty of old, steady winners give a solid all-time CI...
        _settled_decisions(
            db, ADDRESS, [2.0] * (GATE_MIN_DECISIONS - 5),
            settled_at=OLD_TS, market_prefix="0xold",
        )
        # ...but the last LAST_N_DAYS days have gone negative.
        _settled_decisions(
            db, ADDRESS, [-3.0] * 5, settled_at=RECENT_TS, market_prefix="0xrecent",
        )

        report = build_report(db, now=NOW)[0]

        assert report["all_time"]["n"] == GATE_MIN_DECISIONS
        assert report["all_time"]["ci_lower"] > 0  # all-time still looks solid
        assert report["last_7d"]["n"] == 5
        assert report["last_7d"]["mean"] < 0
        assert report["verdict"] == "FAIL"
        assert report["failing_condition"] == "last_7d_mean"


class TestPausedWalletAlwaysFails:
    def test_paused_wallet_fails_regardless_of_stats(self):
        db = _db()
        _follow(db, status="paused", paused_reason="stability_check_failed")
        # Even a wallet with a textbook-perfect record still fails while paused.
        _settled_decisions(db, ADDRESS, [2.0] * GATE_MIN_DECISIONS, settled_at=RECENT_TS)

        report = build_report(db, now=NOW)[0]

        assert report["status"] == "paused"
        assert report["verdict"] == "FAIL"
        assert report["failing_condition"] == "paused"


class TestPassingWallet:
    def test_wallet_meeting_all_four_conditions_passes(self):
        db = _db()
        _follow(db)
        # Old + recent decisions, steady small wins, none dominant.
        _settled_decisions(
            db, ADDRESS, [2.0] * (GATE_MIN_DECISIONS - 5), settled_at=OLD_TS, market_prefix="0xold",
        )
        _settled_decisions(
            db, ADDRESS, [2.0] * 5, settled_at=RECENT_TS, market_prefix="0xrecent",
        )

        report = build_report(db, now=NOW)[0]

        assert report["all_time"]["n"] == GATE_MIN_DECISIONS
        assert report["all_time"]["ci_lower"] > 0
        assert report["last_7d"]["mean"] > 0
        assert report["concentration"] <= GATE_MAX_CONCENTRATION
        assert report["verdict"] == "PASS"
        assert report["failing_condition"] is None


class TestBuildReportShape:
    def test_no_followed_wallets_returns_empty_list(self):
        db = _db()
        assert build_report(db, now=NOW) == []

    def test_evaluate_wallet_handles_zero_decisions(self):
        wallet = {"address": ADDRESS, "status": "active", "paused_reason": None}
        entry = evaluate_wallet(wallet, [], now=NOW)
        assert entry["all_time"]["n"] == 0
        assert entry["verdict"] == "FAIL"
        assert entry["failing_condition"] == "min_decisions"


class TestJsonOutputIntegration:
    def test_main_json_flag_emits_valid_json(self, capsys, monkeypatch):
        db = _db()
        _follow(db)
        _settled_decisions(db, ADDRESS, [2.0] * GATE_MIN_DECISIONS, settled_at=RECENT_TS)

        # main() opens its own Database() -- point it at our seeded fixture
        # DB rather than the real on-disk one.
        monkeypatch.setattr(
            "src.scripts.copy_live_readiness.Database", lambda *a, **k: db,
        )

        rc = main(["--json"])

        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["address"] == ADDRESS
        assert parsed[0]["verdict"] in ("PASS", "FAIL")

    def test_main_text_output_does_not_crash(self, capsys, monkeypatch):
        db = _db()
        _follow(db)
        _settled_decisions(db, ADDRESS, [2.0] * 3, settled_at=RECENT_TS)
        monkeypatch.setattr(
            "src.scripts.copy_live_readiness.Database", lambda *a, **k: db,
        )

        rc = main([])

        assert rc == 0
        out = capsys.readouterr().out
        assert ADDRESS in out
        assert "VERDICT" in out
        assert f"last {LAST_N_DAYS}d" in out
