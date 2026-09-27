"""Unit tests for src/scripts/copy_exit_analysis.py (issue #1222). All
Polymarket I/O is mocked -- no network calls. DB-touching tests use a real
in-memory Database (seeded rows), mirroring test_copy_settle.py's
`Database(":memory:")` pattern.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.data.db import Database
from src.scripts.copy_exit_analysis import (
    analyze_position,
    analyze_wallet,
    build_report,
    exit_following_pnl_usd,
    find_first_exit,
    index_wallet_sells,
    run,
)

ENTRY_DT = datetime(2026, 9, 19, 0, 0, 0, tzinfo=timezone.utc)
ENTRY_TS = ENTRY_DT.isoformat()
ENTRY_TS_UNIX = int(ENTRY_DT.timestamp())


def _sell_trade(market, outcome_index, price, ts_unix, size=25.0):
    return {"market": market, "side": "SELL", "price": price, "size": size,
            "timestamp": ts_unix, "outcome": None, "outcome_index": outcome_index,
            "asset": None, "source_trade_id": None}


def _raw(market, side, price, size, ts_unix, outcome_index=None):
    raw = {
        "conditionId": market, "side": side, "price": str(price),
        "size": str(size), "timestamp": str(ts_unix),
    }
    if outcome_index is not None:
        raw["outcomeIndex"] = outcome_index
    return raw


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TestIndexWalletSells:
    def test_only_sell_trades_indexed_buys_ignored(self, monkeypatch):
        trades = [
            _raw("0xabc", "BUY", 0.40, 25, ENTRY_TS_UNIX),
            _raw("0xabc", "SELL", 0.70, 25, ENTRY_TS_UNIX + 3600, outcome_index=0),
        ]
        monkeypatch.setattr("src.scripts.copy_exit_analysis.get_wallet_trades", lambda addr: trades)
        index = index_wallet_sells("0xwallet")
        assert list(index.keys()) == [("0xabc", 0)]
        assert len(index[("0xabc", 0)]) == 1

    def test_sell_missing_outcome_index_excluded_not_guessed(self, monkeypatch):
        trades = [_raw("0xabc", "SELL", 0.70, 25, ENTRY_TS_UNIX + 3600)]  # no outcomeIndex
        monkeypatch.setattr("src.scripts.copy_exit_analysis.get_wallet_trades", lambda addr: trades)
        index = index_wallet_sells("0xwallet")
        assert index == {}

    def test_bucket_sorted_ascending_by_timestamp(self, monkeypatch):
        trades = [
            _raw("0xabc", "SELL", 0.80, 25, ENTRY_TS_UNIX + 7200, outcome_index=0),
            _raw("0xabc", "SELL", 0.70, 25, ENTRY_TS_UNIX + 3600, outcome_index=0),
        ]
        monkeypatch.setattr("src.scripts.copy_exit_analysis.get_wallet_trades", lambda addr: trades)
        index = index_wallet_sells("0xwallet")
        timestamps = [t["timestamp"] for t in index[("0xabc", 0)]]
        assert timestamps == sorted(timestamps)

    def test_different_outcome_index_kept_in_separate_buckets(self, monkeypatch):
        trades = [
            _raw("0xabc", "SELL", 0.70, 25, ENTRY_TS_UNIX + 3600, outcome_index=0),
            _raw("0xabc", "SELL", 0.30, 25, ENTRY_TS_UNIX + 3600, outcome_index=1),
        ]
        monkeypatch.setattr("src.scripts.copy_exit_analysis.get_wallet_trades", lambda addr: trades)
        index = index_wallet_sells("0xwallet")
        assert set(index.keys()) == {("0xabc", 0), ("0xabc", 1)}


class TestFindFirstExit:
    def test_sell_before_entry_ts_is_ignored(self):
        index = {("0xabc", 0): [_sell_trade("0xabc", 0, 0.70, ENTRY_TS_UNIX - 3600)]}
        assert find_first_exit(index, "0xabc", 0, ENTRY_TS_UNIX) is None

    def test_sell_after_entry_ts_is_found(self):
        sell = _sell_trade("0xabc", 0, 0.70, ENTRY_TS_UNIX + 3600)
        index = {("0xabc", 0): [sell]}
        assert find_first_exit(index, "0xabc", 0, ENTRY_TS_UNIX) == sell

    def test_multiple_sells_after_entry_earliest_used(self):
        earliest = _sell_trade("0xabc", 0, 0.60, ENTRY_TS_UNIX + 1800)
        later = _sell_trade("0xabc", 0, 0.90, ENTRY_TS_UNIX + 7200)
        index = {("0xabc", 0): [earliest, later]}  # pre-sorted ascending
        assert find_first_exit(index, "0xabc", 0, ENTRY_TS_UNIX) == earliest

    def test_no_matching_market_or_outcome_index_returns_none(self):
        index = {("0xabc", 0): [_sell_trade("0xabc", 0, 0.70, ENTRY_TS_UNIX + 3600)]}
        assert find_first_exit(index, "0xabc", 1, ENTRY_TS_UNIX) is None
        assert find_first_exit(index, "0xdef", 0, ENTRY_TS_UNIX) is None


class TestExitFollowingPnlUsd:
    def test_computes_counterfactual_with_sell_slippage(self):
        # entry_price=0.40, stake_usd=10 -> 25 shares. Sell at 0.70, 150bps
        # SELL slippage -> fill 0.70*0.985=0.6895. pnl = 25*0.6895-10.
        pnl = exit_following_pnl_usd(0.40, 10.0, 0.70, 150.0)
        assert pnl == pytest.approx(25 * 0.6895 - 10, abs=1e-6)

    def test_zero_slippage_is_pure_price_delta(self):
        pnl = exit_following_pnl_usd(0.40, 10.0, 0.70, 0.0)
        assert pnl == pytest.approx(25 * 0.70 - 10, abs=1e-6)

    def test_non_positive_entry_price_returns_none(self):
        assert exit_following_pnl_usd(0.0, 10.0, 0.70, 150.0) is None
        assert exit_following_pnl_usd(-0.1, 10.0, 0.70, 150.0) is None


class TestAnalyzePosition:
    def _position(self, **overrides):
        base = {
            "market": "0xabc", "outcome_index": 0, "entry_price": 0.40,
            "stake_usd": 10.0, "entry_ts": ENTRY_TS,
            "settled_pnl_usd": 15.0,  # winning hold-to-resolution outcome
            "settled_at": _iso(ENTRY_DT + timedelta(hours=6)),
        }
        base.update(overrides)
        return base

    def test_wallet_sold_before_resolution_counterfactual_uses_sell_slippage(self):
        sell = _sell_trade("0xabc", 0, 0.70, ENTRY_TS_UNIX + 3600)
        sell_index = {("0xabc", 0): [sell]}
        row = analyze_position(self._position(), sell_index, slippage_bps=150.0)

        assert row["exited"] is True
        expected = exit_following_pnl_usd(0.40, 10.0, 0.70, 150.0)
        assert row["exit_pnl_usd"] == pytest.approx(expected)
        assert row["hours_to_exit"] == pytest.approx(1.0)
        assert row["hours_to_resolution"] == pytest.approx(6.0)
        assert row["hold_pnl_usd"] == 15.0
        assert row["is_winner"] is True

    def test_wallet_never_sold_lands_in_never_exited_bucket_not_dropped(self):
        row = analyze_position(self._position(), sell_index={}, slippage_bps=150.0)

        assert row["exited"] is False
        assert row["exit_pnl_usd"] is None
        assert row["hours_to_exit"] is None
        # Hold pnl / hours_to_resolution are still populated -- never dropped.
        assert row["hold_pnl_usd"] == 15.0
        assert row["hours_to_resolution"] == pytest.approx(6.0)

    def test_sell_before_entry_does_not_count_as_exit(self):
        sell = _sell_trade("0xabc", 0, 0.70, ENTRY_TS_UNIX - 3600)
        sell_index = {("0xabc", 0): [sell]}
        row = analyze_position(self._position(), sell_index, slippage_bps=150.0)
        assert row["exited"] is False

    def test_losing_position_marked_not_winner(self):
        row = analyze_position(
            self._position(settled_pnl_usd=-10.0), sell_index={}, slippage_bps=150.0,
        )
        assert row["is_winner"] is False
        assert row["hold_pnl_usd"] == -10.0

    def test_unparseable_entry_ts_lands_in_never_exited_bucket_no_crash(self):
        row = analyze_position(
            self._position(entry_ts="not-a-timestamp"), sell_index={}, slippage_bps=150.0,
        )
        assert row["exited"] is False
        assert row["hours_to_resolution"] is None


class TestAnalyzeWallet:
    def test_zero_settled_positions_reports_no_data_no_crash(self, monkeypatch):
        mock_fetch = pytest.importorskip("unittest.mock").MagicMock()
        monkeypatch.setattr("src.scripts.copy_exit_analysis.get_wallet_trades", mock_fetch)

        result = analyze_wallet("0xnodata", positions=[], slippage_bps=150.0)

        assert result == {"address": "0xnodata", "no_data": True}
        # No trade tape should be fetched for a wallet with nothing to analyze
        # -- avoids wasting the shared 1 req/sec Polymarket budget.
        mock_fetch.assert_not_called()

    def test_wallet_with_settled_positions_fetches_trade_tape_exactly_once(self, monkeypatch):
        calls = []

        def fake_get_wallet_trades(address):
            calls.append(address)
            return [_raw("0xabc", "SELL", 0.70, 25, ENTRY_TS_UNIX + 3600, outcome_index=0)]

        monkeypatch.setattr(
            "src.scripts.copy_exit_analysis.get_wallet_trades", fake_get_wallet_trades,
        )
        positions = [
            {
                "market": "0xabc", "outcome_index": 0, "entry_price": 0.40,
                "stake_usd": 10.0, "entry_ts": ENTRY_TS, "settled_pnl_usd": 15.0,
                "settled_at": _iso(ENTRY_DT + timedelta(hours=6)),
            },
            {
                "market": "0xdef", "outcome_index": 0, "entry_price": 0.40,
                "stake_usd": 10.0, "entry_ts": ENTRY_TS, "settled_pnl_usd": -10.0,
                "settled_at": _iso(ENTRY_DT + timedelta(hours=6)),
            },
        ]

        result = analyze_wallet("0xwallet", positions, slippage_bps=150.0)

        assert calls == ["0xwallet"]  # exactly one fetch for two positions
        assert result["no_data"] is False
        assert result["overall"]["n"] == 2


class TestWinnerLoserSplitArithmetic:
    """Hand-computed fixture: two winners (one exited, one never-exited) and
    one loser (exited), verifying both the aggregate arithmetic and the
    winner/loser split."""

    def test_hand_computed_split(self, monkeypatch):
        winner_exited_sell = _raw("0xwin1", "SELL", 0.70, 25, ENTRY_TS_UNIX + 3600, outcome_index=0)
        loser_exited_sell = _raw("0xlose1", "SELL", 0.10, 25, ENTRY_TS_UNIX + 3600, outcome_index=0)
        monkeypatch.setattr(
            "src.scripts.copy_exit_analysis.get_wallet_trades",
            lambda addr: [winner_exited_sell, loser_exited_sell],
        )

        positions = [
            # Winner, exited: hold=15.0, exit=25*0.6895-10=7.2375.
            {"market": "0xwin1", "outcome_index": 0, "entry_price": 0.40, "stake_usd": 10.0,
             "entry_ts": ENTRY_TS, "settled_pnl_usd": 15.0,
             "settled_at": _iso(ENTRY_DT + timedelta(hours=6))},
            # Winner, never exited: hold == exit == 15.0.
            {"market": "0xwin2", "outcome_index": 0, "entry_price": 0.40, "stake_usd": 10.0,
             "entry_ts": ENTRY_TS, "settled_pnl_usd": 15.0,
             "settled_at": _iso(ENTRY_DT + timedelta(hours=6))},
            # Loser, exited (cuts the loss): hold=-10.0, exit=25*0.0985-10=-7.5375.
            {"market": "0xlose1", "outcome_index": 0, "entry_price": 0.40, "stake_usd": 10.0,
             "entry_ts": ENTRY_TS, "settled_pnl_usd": -10.0,
             "settled_at": _iso(ENTRY_DT + timedelta(hours=6))},
        ]

        result = analyze_wallet("0xwallet", positions, slippage_bps=150.0)

        assert result["winners"]["n"] == 2
        assert result["winners"]["hold_total_pnl"] == pytest.approx(30.0)
        assert result["winners"]["exit_total_pnl"] == pytest.approx(7.2375 + 15.0, abs=0.01)

        assert result["losers"]["n"] == 1
        assert result["losers"]["hold_total_pnl"] == pytest.approx(-10.0)
        assert result["losers"]["exit_total_pnl"] == pytest.approx(-7.5375, abs=0.01)

        assert result["overall"]["n"] == 3
        assert result["overall"]["n_exited"] == 2
        assert result["overall"]["n_never_exited"] == 1
        assert result["overall"]["hold_total_pnl"] == pytest.approx(15.0 + 15.0 - 10.0)
        assert result["overall"]["exit_total_pnl"] == pytest.approx(7.2375 + 15.0 - 7.5375, abs=0.01)


class TestBuildReport:
    def test_report_contains_key_sections(self):
        wallet_results = [{
            "address": "0x1234567890", "no_data": False,
            "overall": {"n": 3, "n_exited": 2, "n_never_exited": 1, "pct_exited": 2 / 3,
                        "hold_total_pnl": 20.0, "exit_total_pnl": 14.7, "hold_avg_pnl": 6.67,
                        "exit_avg_pnl": 4.9, "median_hours_to_exit": 1.0,
                        "median_hours_to_resolution": 6.0},
            "winners": {"n": 2, "n_exited": 1, "n_never_exited": 1, "pct_exited": 0.5,
                        "hold_total_pnl": 30.0, "exit_total_pnl": 22.24, "hold_avg_pnl": 15.0,
                        "exit_avg_pnl": 11.12, "median_hours_to_exit": 1.0,
                        "median_hours_to_resolution": 6.0},
            "losers": {"n": 1, "n_exited": 1, "n_never_exited": 0, "pct_exited": 1.0,
                       "hold_total_pnl": -10.0, "exit_total_pnl": -7.54, "hold_avg_pnl": -10.0,
                       "exit_avg_pnl": -7.54, "median_hours_to_exit": 1.0,
                       "median_hours_to_resolution": None},
        }]
        report = build_report("2026-09-27", 150.0, wallet_results)

        assert "Copy-Trading Exit-Following Analysis" in report
        assert "Read-only report" in report
        assert "0x12345678" in report
        assert "150 bps" in report
        assert "## Limitations" in report
        assert "first-sell-only" in report.lower()
        assert "outcome_index disambiguation" in report.lower()

    def test_never_exited_bucket_shown_as_distinct_not_dropped(self):
        wallet_results = [{
            "address": "0xnodata", "no_data": True,
        }]
        report = build_report("2026-09-27", 150.0, wallet_results)
        assert "No settled positions" in report
        assert "0xnodata"[:10] in report

    def test_no_data_wallet_omitted_from_per_wallet_table_but_counted(self):
        wallet_results = [{"address": "0xnodata", "no_data": True}]
        report = build_report("2026-09-27", 150.0, wallet_results)
        assert "Wallets with no settled positions (reported, not dropped): 1" in report


class _ReadOnlyDbProxy:
    """Wraps a real Database, exposing only the read methods
    copy_exit_analysis.run() is documented to call. Any other attribute
    access (in particular any write/insert/settle method) raises
    AttributeError -- a stronger guarantee than asserting DB state
    afterwards, since it fails the moment a write is even attempted."""

    _ALLOWED = {"get_copy_realized_pnl_by_wallet", "get_followed_wallets", "get_settled_copy_positions"}

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        if name in self._ALLOWED:
            return getattr(self._db, name)
        raise AttributeError(f"read-only proxy: {name!r} is not an allowed read method")


class TestRunIsReadOnly:
    def test_run_never_calls_a_write_method(self, tmp_path, monkeypatch):
        db = Database(":memory:")
        signal_id = db.insert_copy_signal(
            address="0xwallet", market="0xabc", source_price=0.40,
            detected_at=ENTRY_TS, outcome_index=0,
        )
        position_id = db.insert_copy_position(
            signal_id=signal_id, address="0xwallet", market="0xabc",
            outcome_index=0, entry_price=0.40, stake_usd=10.0, entry_ts=ENTRY_TS,
        )
        db.settle_copy_position(position_id, 15.0, _iso(ENTRY_DT + timedelta(hours=6)))

        monkeypatch.setattr(
            "src.scripts.copy_exit_analysis.get_wallet_trades",
            lambda addr: [_raw("0xabc", "SELL", 0.70, 25, ENTRY_TS_UNIX + 3600, outcome_index=0)],
        )

        proxy = _ReadOnlyDbProxy(db)
        rc = run(proxy, slippage_bps=150.0, out_dir=tmp_path, run_date="2026-09-27")

        assert rc == 0
        out_file = tmp_path / "copy_exit_analysis_2026-09-27.md"
        assert out_file.exists()
        assert "0xwallet"[:10] in out_file.read_text()


class TestRunWithSeededDatabase:
    def test_wallet_with_no_settled_positions_reported_as_no_data(self, tmp_path):
        db = Database(":memory:")
        db.insert_followed_wallet(address="0xfollowed_no_history", stake_per_trade=5.0, added_at=ENTRY_TS)

        rc = run(db, slippage_bps=150.0, out_dir=tmp_path, run_date="2026-09-27")

        assert rc == 0
        report = (tmp_path / "copy_exit_analysis_2026-09-27.md").read_text()
        assert "0xfollowed" in report
        assert "No settled positions" in report

    def test_no_wallets_at_all_writes_nothing(self, tmp_path):
        db = Database(":memory:")
        rc = run(db, slippage_bps=150.0, out_dir=tmp_path, run_date="2026-09-27")
        assert rc == 0
        assert not list(tmp_path.glob("*.md"))
