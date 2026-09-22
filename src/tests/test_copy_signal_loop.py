"""Unit tests for src/scripts/copy_signal_loop.py (epic #1101 story B3,
issue #1123). Fully mocked -- no real DB or network calls, mirroring
test_copy_wallet_screening.py's mocking style (MagicMock db, patched
module-level functions).
"""
from unittest.mock import MagicMock, patch

import pytest

from src.config import CONFIG_DEFAULTS
from src.scripts.copy_signal_loop import (
    live_startup_sanity_check,
    main,
    run_cycle,
    startup_sanity_check,
)

ADDRESS = "0xwallet1"
MARKET = "0xmarket1"
NOW_ISO = "2026-09-20T00:00:00+00:00"


def _wallet(**overrides) -> dict:
    # last_seen_trade_ts defaults to a real (non-None) prior value -- most
    # tests exercise the steady-state decision ladder (market_resolved /
    # exposure limits / execute), NOT the first-poll bootstrap guard.
    # Tests that specifically want the bootstrap path pass
    # last_seen_trade_ts=None explicitly.
    kwargs = dict(
        address=ADDRESS,
        stake_per_trade=10.0,
        status="active",
        paused_reason=None,
        added_at="2026-09-19T00:00:00+00:00",
        last_seen_trade_ts=500,
    )
    kwargs.update(overrides)
    return kwargs


def _live_config(**overrides) -> dict:
    cfg = dict(CONFIG_DEFAULTS)
    cfg.update(
        COPY_TRADING_ENABLED=True,
        COPY_MAX_EXPOSURE_PER_WALLET_USD=50.0,
        COPY_MAX_TOTAL_EXPOSURE_USD=250.0,
        COPY_TRADING_CAPITAL_USD=250.0,
    )
    cfg.update(overrides)
    return cfg


def _buy_raw(**overrides) -> dict:
    raw = dict(
        conditionId=MARKET,
        side="BUY",
        price="0.40",
        size="10",
        timestamp="1000",
        outcome="Yes",
        outcomeIndex=0,
        transactionHash="0xtx1",
    )
    raw.update(overrides)
    return raw


def _sell_raw(**overrides) -> dict:
    raw = dict(
        conditionId=MARKET,
        side="SELL",
        price="0.60",
        size="5",
        timestamp="1000",
        outcome="Yes",
        outcomeIndex=0,
        transactionHash="0xtx-sell",
    )
    raw.update(overrides)
    return raw


def _mock_db(**overrides) -> MagicMock:
    """A MagicMock Database double with sane defaults for the happy path."""
    db = MagicMock()
    db.get_followed_wallets.return_value = [_wallet()]
    db.copy_signal_exists_for_trade.return_value = False
    db.get_open_copy_positions.return_value = []
    db.insert_copy_signal.return_value = 1
    db.insert_copy_position.return_value = 1
    # Circuit breaker (issue #1139) reads these -- zero realized P&L by
    # default so the breaker never trips unless a test deliberately
    # overrides one of these two to exercise it.
    db.get_copy_realized_pnl_total_for_date.return_value = {
        "n_settled": 0, "total_pnl_usd": 0.0,
    }
    db.get_copy_realized_pnl_total.return_value = {
        "n_settled": 0, "total_pnl_usd": 0.0,
    }
    for key, value in overrides.items():
        setattr(getattr(db, key), "return_value", value)
    return db


class TestStartupSanityCheck:
    def test_refuses_when_total_exposure_exceeds_capital(self):
        with patch("src.scripts.copy_signal_loop.COPY_TRADING_CAPITAL_USD", 100.0):
            error = startup_sanity_check(
                _live_config(COPY_MAX_TOTAL_EXPOSURE_USD=250.0)
            )
        assert error is not None
        assert "COPY_MAX_TOTAL_EXPOSURE_USD" in error

    def test_allows_when_total_exposure_at_or_below_capital(self):
        with patch("src.scripts.copy_signal_loop.COPY_TRADING_CAPITAL_USD", 250.0):
            error = startup_sanity_check(
                _live_config(COPY_MAX_TOTAL_EXPOSURE_USD=250.0)
            )
        assert error is None

    def test_main_refuses_to_start_and_returns_1(self):
        db = _mock_db()
        with patch("src.scripts.copy_signal_loop.Database", return_value=db), \
             patch("src.scripts.copy_signal_loop.seed_config"), \
             patch(
                 "src.scripts.copy_signal_loop.get_live_config",
                 return_value=_live_config(COPY_MAX_TOTAL_EXPOSURE_USD=999999.0),
             ):
            rc = main(["--once"])
        assert rc == 1
        db.get_followed_wallets.assert_not_called()


class TestLiveStartupSanityCheck:
    """Issue #1163: mirrors TestStartupSanityCheck exactly, one layer up, for
    live_startup_sanity_check / COPY_LIVE_MAX_TOTAL_EXPOSURE_USD vs.
    COPY_LIVE_CAPITAL_USD. Not wired into main() -- no live loop exists yet
    to call it (epic H's job, #1159), so there is no main()-refuses-to-start
    equivalent here."""

    def test_refuses_when_live_total_exposure_exceeds_live_capital(self):
        with patch("src.scripts.copy_signal_loop.COPY_LIVE_CAPITAL_USD", 100.0):
            error = live_startup_sanity_check(
                _live_config(COPY_LIVE_MAX_TOTAL_EXPOSURE_USD=250.0)
            )
        assert error is not None
        assert "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD" in error

    def test_allows_when_live_total_exposure_at_or_below_live_capital(self):
        with patch("src.scripts.copy_signal_loop.COPY_LIVE_CAPITAL_USD", 250.0):
            error = live_startup_sanity_check(
                _live_config(COPY_LIVE_MAX_TOTAL_EXPOSURE_USD=250.0)
            )
        assert error is None

    def test_live_check_is_independent_of_paper_sanity_check(self):
        """A live config that fails the live check but passes the paper
        check (and vice versa) confirms the two checks are fully isolated,
        mirroring the kill-switch/capital isolation established in #1115."""
        cfg = _live_config(
            COPY_MAX_TOTAL_EXPOSURE_USD=250.0,
            COPY_LIVE_MAX_TOTAL_EXPOSURE_USD=250.0,
        )
        with patch("src.scripts.copy_signal_loop.COPY_TRADING_CAPITAL_USD", 250.0), \
             patch("src.scripts.copy_signal_loop.COPY_LIVE_CAPITAL_USD", 100.0):
            assert startup_sanity_check(cfg) is None
            assert live_startup_sanity_check(cfg) is not None


class TestKillSwitch:
    def test_disabled_produces_no_polling_or_writes(self):
        db = _mock_db()
        with patch(
            "src.scripts.copy_signal_loop.get_live_config",
            return_value=_live_config(COPY_TRADING_ENABLED=False),
        ):
            run_cycle(db)
        db.get_followed_wallets.assert_not_called()
        db.insert_copy_signal.assert_not_called()
        db.insert_copy_position.assert_not_called()


class TestCircuitBreaker:
    """Realized-P&L circuit breaker wiring (issue #1139). Never mocks
    src.risk.copy_risk_manager.allow_copy_signal itself -- exercises the
    real function against the mocked db's get_copy_realized_pnl_total /
    get_copy_realized_pnl_total_for_date, matching test_copy_signal_loop.py's
    existing style of mocking only the DB and network boundary, not
    in-process collaborators.
    """

    def _run(self, db, raw_trades, live_config=None, resolution=None):
        with patch(
            "src.scripts.copy_signal_loop.get_live_config",
            return_value=live_config or _live_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since",
            return_value=raw_trades,
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution",
            return_value=resolution,
        ):
            run_cycle(db)

    def test_tripped_daily_loss_still_logs_signal_but_never_executes(self):
        db = _mock_db()
        db.get_copy_realized_pnl_total_for_date.return_value = {
            "n_settled": 3, "total_pnl_usd": -30.0,
        }
        live_config = _live_config(COPY_DAILY_LOSS_LIMIT_USD=25.0)
        self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        db.insert_copy_signal.assert_called_once()
        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["order_placed"] == 0
        assert signal_kwargs["skip_reason"] == "circuit_breaker_daily_loss"
        db.insert_copy_position.assert_not_called()

    def test_tripped_drawdown_still_logs_signal_but_never_executes(self):
        db = _mock_db()
        db.get_copy_realized_pnl_total.return_value = {
            "n_settled": 5, "total_pnl_usd": -50.0,
        }
        live_config = _live_config(
            COPY_DAILY_LOSS_LIMIT_USD=999.0, COPY_DRAWDOWN_STOP_PCT=0.20,
        )
        with patch("src.risk.copy_risk_manager.COPY_TRADING_CAPITAL_USD", 100.0):
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        db.insert_copy_signal.assert_called_once()
        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["order_placed"] == 0
        assert signal_kwargs["skip_reason"] == "circuit_breaker_drawdown"
        db.insert_copy_position.assert_not_called()

    def test_tripped_breaker_never_calls_fetch_market_resolution(self):
        """Same optimization as the first-poll bootstrap guard: a trade
        that will be skipped unconditionally never spends a network call."""
        db = _mock_db()
        db.get_copy_realized_pnl_total_for_date.return_value = {
            "n_settled": 1, "total_pnl_usd": -30.0,
        }
        live_config = _live_config(COPY_DAILY_LOSS_LIMIT_USD=25.0)
        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=live_config,
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since",
            return_value=[_buy_raw()],
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution",
        ) as mock_resolve:
            run_cycle(db)
        mock_resolve.assert_not_called()

    def test_not_tripped_does_not_change_normal_execution(self):
        db = _mock_db()
        # Explicitly zero, well under both thresholds.
        db.get_copy_realized_pnl_total_for_date.return_value = {
            "n_settled": 0, "total_pnl_usd": 0.0,
        }
        db.get_copy_realized_pnl_total.return_value = {
            "n_settled": 0, "total_pnl_usd": 0.0,
        }
        self._run(db, [_buy_raw()], resolution=None)

        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["order_placed"] == 1
        assert signal_kwargs.get("skip_reason") is None
        db.insert_copy_position.assert_called_once()

    def test_breaker_checked_once_per_cycle_not_per_wallet(self):
        """Two active wallets, breaker tripped -- get_copy_realized_pnl_total_for_date
        is called exactly once (the breaker decision), not once per wallet."""
        wallet_a = _wallet(address="0xwalletA")
        wallet_b = _wallet(address="0xwalletB")
        db = _mock_db()
        db.get_followed_wallets.return_value = [wallet_a, wallet_b]
        db.get_copy_realized_pnl_total_for_date.return_value = {
            "n_settled": 1, "total_pnl_usd": -30.0,
        }
        live_config = _live_config(COPY_DAILY_LOSS_LIMIT_USD=25.0)
        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=live_config,
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since",
            return_value=[_buy_raw()],
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution", return_value=None,
        ):
            run_cycle(db)
        db.get_copy_realized_pnl_total_for_date.assert_called_once()
        assert db.insert_copy_signal.call_count == 2
        for call in db.insert_copy_signal.call_args_list:
            assert call.kwargs["skip_reason"] == "circuit_breaker_daily_loss"

    def test_never_touches_settlement_code_path(self):
        """Code-inspection-level assertion (per the issue's test
        requirements): neither the circuit breaker module nor
        copy_signal_loop.py's breaker wiring imports copy_settle.py or
        calls Database.settle_copy_position -- a tripped breaker can only
        ever affect the execute-or-skip decision for a brand new signal,
        never an already-open position's settlement.
        """
        import ast
        import inspect

        import src.risk.copy_risk_manager as crm
        import src.scripts.copy_signal_loop as loop_module

        for module in (crm, loop_module):
            source = inspect.getsource(module)
            assert "settle_copy_position" not in source
            tree = ast.parse(source)
            imported_names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_names.add(node.module)
                    imported_names.update(
                        f"{node.module}.{alias.name}" for alias in node.names
                    )
            assert not any("copy_settle" in name for name in imported_names), (
                f"{module.__name__} must never import copy_settle.py "
                "(a tripped circuit breaker must never touch settlement)"
            )


class TestSignalDetectionAndExecution:
    def _run(self, db, raw_trades, live_config=None, resolution=None):
        with patch(
            "src.scripts.copy_signal_loop.get_live_config",
            return_value=live_config or _live_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since",
            return_value=raw_trades,
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution",
            return_value=resolution,
        ):
            run_cycle(db)

    def test_new_buy_under_limits_executes(self):
        db = _mock_db()
        self._run(db, [_buy_raw()], resolution=None)

        db.insert_copy_signal.assert_called_once()
        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["order_placed"] == 1
        assert signal_kwargs.get("skip_reason") is None
        expected_fill = 0.40 * 1.015  # apply_slippage(0.40, "BUY", 150bps)
        assert signal_kwargs["fill_price"] == pytest.approx(expected_fill)
        assert signal_kwargs["size_usd"] == 10.0

        db.insert_copy_position.assert_called_once()
        position_kwargs = db.insert_copy_position.call_args.kwargs
        assert position_kwargs["entry_price"] == pytest.approx(expected_fill)
        assert position_kwargs["stake_usd"] == 10.0
        assert position_kwargs["address"] == ADDRESS
        assert position_kwargs["market"] == MARKET

        db.link_copy_signal_to_position.assert_called_once()

    def test_market_already_resolved_skips(self):
        db = _mock_db()
        self._run(db, [_buy_raw()], resolution=True)  # resolved (YES won)

        db.insert_copy_signal.assert_called_once()
        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["order_placed"] == 0
        assert signal_kwargs["skip_reason"] == "market_resolved"
        db.insert_copy_position.assert_not_called()

    def test_wallet_exposure_limit_skips(self):
        db = _mock_db()
        db.get_open_copy_positions.side_effect = lambda address=None: (
            [{"stake_usd": 45.0}] if address is not None else [{"stake_usd": 45.0}]
        )
        live_config = _live_config(COPY_MAX_EXPOSURE_PER_WALLET_USD=50.0)
        # wallet stake_per_trade=10 -> 45 + 10 = 55 > 50 -> skip
        self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["skip_reason"] == "wallet_exposure_limit"
        db.insert_copy_position.assert_not_called()

    def test_total_exposure_limit_skips(self):
        db = _mock_db()

        def _open_positions(address=None):
            if address is not None:
                return [{"stake_usd": 5.0}]  # well under per-wallet cap
            return [{"stake_usd": 245.0}]  # near total cap

        db.get_open_copy_positions.side_effect = _open_positions
        live_config = _live_config(
            COPY_MAX_EXPOSURE_PER_WALLET_USD=50.0, COPY_MAX_TOTAL_EXPOSURE_USD=250.0,
        )
        # total: 245 + 10 = 255 > 250 -> skip
        self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["skip_reason"] == "total_exposure_limit"
        db.insert_copy_position.assert_not_called()

    def test_missing_outcome_index_skips_without_executing(self):
        db = _mock_db()
        raw = _buy_raw()
        del raw["outcomeIndex"]
        self._run(db, [raw], resolution=None)

        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["skip_reason"] == "missing_outcome_index"
        db.insert_copy_position.assert_not_called()

    def test_sell_trade_never_becomes_a_signal(self):
        db = _mock_db()
        self._run(db, [_sell_raw()], resolution=None)

        db.insert_copy_signal.assert_not_called()
        db.insert_copy_position.assert_not_called()
        # last_seen_trade_ts still advances past the SELL's timestamp.
        db.update_followed_wallet_last_seen.assert_called_once_with(ADDRESS, 1000)

    def test_already_signaled_trade_is_not_reprocessed(self):
        db = _mock_db()
        db.copy_signal_exists_for_trade.return_value = True
        self._run(db, [_buy_raw()], resolution=None)

        db.insert_copy_signal.assert_not_called()
        db.insert_copy_position.assert_not_called()


class TestFirstPollBootstrapGuard:
    """PM decision on PR #1128 review (2026-09-20): a newly-followed
    wallet's first-ever poll (last_seen_trade_ts NULL entering the cycle)
    must never execute its backlog -- every BUY still gets a
    copy_signals row, but always skip_reason="wallet_newly_followed",
    regardless of what market-resolution/exposure checks would have
    said. The watermark still advances normally so the second poll
    onward sees only genuinely new trades."""

    def _run(self, db, raw_trades, live_config=None, resolution=None):
        with patch(
            "src.scripts.copy_signal_loop.get_live_config",
            return_value=live_config or _live_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since",
            return_value=raw_trades,
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution",
            return_value=resolution,
        ) as mock_resolution:
            run_cycle(db)
        return mock_resolution

    def test_backlog_buy_is_skipped_never_executed(self):
        db = _mock_db()
        db.get_followed_wallets.return_value = [_wallet(last_seen_trade_ts=None)]
        # Even a trade that would otherwise clearly clear every check
        # (market open, both exposure caps empty) must still be skipped.
        db.get_open_copy_positions.return_value = []
        self._run(db, [_buy_raw()], resolution=None)

        db.insert_copy_signal.assert_called_once()
        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["skip_reason"] == "wallet_newly_followed"
        assert signal_kwargs["order_placed"] == 0
        db.insert_copy_position.assert_not_called()

    def test_backlog_buy_skipped_even_if_market_would_be_resolved_or_over_limits(self):
        """The bootstrap skip pre-empts the normal ladder entirely -- it
        doesn't matter whether the market is resolved or exposure is
        maxed out, the reason is always wallet_newly_followed."""
        db = _mock_db()
        db.get_followed_wallets.return_value = [_wallet(last_seen_trade_ts=None)]
        db.get_open_copy_positions.return_value = [{"stake_usd": 999.0}]  # way over any cap
        self._run(db, [_buy_raw()], resolution=True)  # market resolved YES

        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["skip_reason"] == "wallet_newly_followed"

    def test_fetch_market_resolution_never_called_for_backlog(self):
        """No point spending the network call on a trade that's skipped
        unconditionally -- also avoids hammering the resolution endpoint
        across a large first-poll backlog."""
        db = _mock_db()
        db.get_followed_wallets.return_value = [_wallet(last_seen_trade_ts=None)]
        mock_resolution = self._run(db, [_buy_raw()], resolution=None)

        mock_resolution.assert_not_called()

    def test_exposure_checks_never_called_for_backlog(self):
        db = _mock_db()
        db.get_followed_wallets.return_value = [_wallet(last_seen_trade_ts=None)]
        self._run(db, [_buy_raw()], resolution=None)

        db.get_open_copy_positions.assert_not_called()

    def test_watermark_still_advances_normally_after_bootstrap_cycle(self):
        db = _mock_db()
        db.get_followed_wallets.return_value = [_wallet(last_seen_trade_ts=None)]
        self._run(db, [_buy_raw(timestamp="1500")], resolution=None)

        db.update_followed_wallet_last_seen.assert_called_once_with(ADDRESS, 1500)

    def test_sell_trade_in_backlog_still_just_advances_watermark(self):
        """SELLs were already never signaled -- the bootstrap guard only
        changes BUY handling, so this should be unaffected."""
        db = _mock_db()
        db.get_followed_wallets.return_value = [_wallet(last_seen_trade_ts=None)]
        self._run(db, [_sell_raw()], resolution=None)

        db.insert_copy_signal.assert_not_called()
        db.update_followed_wallet_last_seen.assert_called_once_with(ADDRESS, 1000)

    def test_second_poll_no_longer_bootstraps(self):
        """Once last_seen_trade_ts is set (from the first poll), a
        subsequent poll goes through the normal decision ladder again."""
        db = _mock_db()
        db.get_followed_wallets.return_value = [_wallet(last_seen_trade_ts=1500)]
        db.get_open_copy_positions.return_value = []
        self._run(db, [_buy_raw(timestamp="2000")], resolution=None)

        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs.get("skip_reason") is None
        assert signal_kwargs["order_placed"] == 1
        db.insert_copy_position.assert_called_once()


class TestLastSeenTradeTsAdvancement:
    def test_advances_to_max_timestamp_of_processed_batch(self):
        db = _mock_db()
        trades = [_buy_raw(timestamp="1000"), _buy_raw(timestamp="2000", transactionHash="0xtx2")]
        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=_live_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since", return_value=trades,
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution", return_value=None,
        ):
            run_cycle(db)

        # Watermark advances incrementally, trade by trade (fail-safe
        # design, AI review #1128 BLOCK item 2) -- the LAST call reflects
        # the batch's max timestamp.
        db.update_followed_wallet_last_seen.assert_called_with(ADDRESS, 2000)
        assert db.update_followed_wallet_last_seen.call_count == 2

    def test_mid_batch_failure_persists_watermark_up_to_last_success(self):
        """If a trade raises partway through a batch, the watermark must
        already reflect everything processed before it -- never silently
        stuck at the pre-cycle value, and never advanced past the
        failing trade."""
        db = _mock_db()
        db.insert_copy_signal.side_effect = [1, RuntimeError("db write failed")]
        trades = [_buy_raw(timestamp="1000"), _buy_raw(timestamp="2000", transactionHash="0xtx2")]
        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=_live_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since", return_value=trades,
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution", return_value=None,
        ):
            run_cycle(db)  # per-wallet isolation swallows the RuntimeError

        # First trade (ts=1000) succeeded and its watermark was persisted;
        # the second (ts=2000) raised, so the watermark was never advanced
        # past it.
        db.update_followed_wallet_last_seen.assert_called_once_with(ADDRESS, 1000)

    def test_no_new_trades_does_not_update_last_seen(self):
        db = _mock_db()
        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=_live_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since", return_value=[],
        ):
            run_cycle(db)

        db.update_followed_wallet_last_seen.assert_not_called()

    def test_unnormalizable_trade_still_advances_watermark_and_warns(self, caplog):
        """AI review #1128 round 2, BLOCK item 1: unnormalizable trades are
        watermark-advanced past permanently by design (on-chain records
        don't change on re-fetch, so retrying is pointless) -- but a
        warning must be logged so a genuine normalize_trade() regression
        is observable."""
        db = _mock_db()
        malformed = _buy_raw(timestamp="1500")
        del malformed["conditionId"]  # no market -> normalize_trade() returns None
        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=_live_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since", return_value=[malformed],
        ), caplog.at_level("WARNING"):
            run_cycle(db)

        db.insert_copy_signal.assert_not_called()
        db.update_followed_wallet_last_seen.assert_called_once_with(ADDRESS, 1500)
        assert any("unparseable trade record" in rec.message for rec in caplog.records)


class TestNetworkNotHeldUnderLock:
    def test_fetch_market_resolution_called_before_lock_acquired(self):
        """AI review #1128 round 2, BLOCK item 2: fetch_market_resolution()
        is a blocking HTTP call and must never run inside db._lock -- a
        slow/hanging call would otherwise stall every other DB operation
        sharing this Database instance (e.g. the dashboard's own
        request-handling threads) for the duration of the timeout."""
        db = _mock_db()
        call_order = []

        class _RecordingLock:
            def __enter__(self):
                call_order.append("lock_enter")
                return self

            def __exit__(self, *exc_info):
                call_order.append("lock_exit")
                return False

        db._lock = _RecordingLock()

        def _resolution_side_effect(market):
            call_order.append("fetch_market_resolution")
            return None

        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=_live_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since", return_value=[_buy_raw()],
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution",
            side_effect=_resolution_side_effect,
        ):
            run_cycle(db)

        assert call_order.index("fetch_market_resolution") < call_order.index("lock_enter")


class TestPerWalletFailureIsolation:
    def test_one_wallet_fetch_failure_does_not_block_others(self):
        db = _mock_db()
        wallet_a = _wallet(address="0xaaa")
        wallet_b = _wallet(address="0xbbb")
        db.get_followed_wallets.return_value = [wallet_a, wallet_b]

        def _fetch_side_effect(address, since_ts):
            if address == "0xaaa":
                raise ConnectionError("boom")
            return [_buy_raw()]

        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=_live_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since",
            side_effect=_fetch_side_effect,
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution", return_value=None,
        ):
            run_cycle(db)

        # 0xbbb still got processed despite 0xaaa's fetch failure.
        db.insert_copy_signal.assert_called_once()
        assert db.insert_copy_signal.call_args.kwargs["address"] == "0xbbb"
        db.update_followed_wallet_last_seen.assert_called_once_with("0xbbb", 1000)
