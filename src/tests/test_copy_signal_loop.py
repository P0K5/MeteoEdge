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


TOKEN_ID = "0xtoken1"


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
        # issue #1167: the copied wallet's own trade record carries the CLOB
        # token id it traded -- normalize_trade() reads this as "asset",
        # which the live-execution path reuses directly as token_id.
        asset=TOKEN_ID,
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
    # Live execution (issue #1167) -- zero open live exposure by default so
    # the live exposure gates never trip unless a test deliberately
    # overrides one of these two to exercise it.
    db.get_open_copy_live_positions.return_value = []
    db.insert_copy_live_position.return_value = 101
    # Live circuit breaker (issue #1175) -- zero realized LIVE P&L by
    # default, mirroring the paper defaults above, so it never trips
    # unless a test deliberately overrides one of these two to exercise it.
    db.get_copy_live_realized_pnl_total_for_date.return_value = {
        "n_settled": 0, "total_pnl_usd": 0.0,
    }
    db.get_copy_live_realized_pnl_total.return_value = {
        "n_settled": 0, "total_pnl_usd": 0.0,
    }
    for key, value in overrides.items():
        setattr(getattr(db, key), "return_value", value)
    return db


def _live_enabled_config(**overrides) -> dict:
    """A _live_config() with live trading also switched on and its own
    exposure/capital gates wide open by default -- mirrors _live_config()'s
    own "generous defaults, tests override to exercise a specific gate"
    convention, one layer up."""
    cfg = _live_config(
        COPY_LIVE_TRADING_ENABLED=True,
        COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD=50.0,
        COPY_LIVE_MAX_TOTAL_EXPOSURE_USD=250.0,
    )
    cfg.update(overrides)
    return cfg


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


class TestLiveExecution:
    """Issue #1167 -- wiring live order placement into the gate ladder.

    Per the issue's own explicit requirement, ``test_disabled_never_invokes_clob_submission``
    below is the single most important test in this class: it patches
    ``LiveTrader.place_order`` itself (the actual CLOB submission entry
    point every live order -- weather or copy-trading -- goes through, not
    just this module's own ``execute_live_copy_order`` wrapper) and proves
    zero invocations across a full ``run_cycle()`` with
    ``COPY_LIVE_TRADING_ENABLED=False``, deliberately set up so every other
    gate (paper's ladder, live's own exposure caps, the sanity check) would
    otherwise pass.
    """

    def _run(
        self, db, raw_trades, live_config=None, resolution=None,
        clob_client_factory=None, live_capital_usd=250.0,
    ):
        with patch(
            "src.scripts.copy_signal_loop.get_live_config",
            return_value=live_config if live_config is not None else _live_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since",
            return_value=raw_trades,
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution",
            return_value=resolution,
        ), patch(
            "src.scripts.copy_signal_loop.COPY_LIVE_CAPITAL_USD", live_capital_usd,
        ):
            run_cycle(db, clob_client_factory=clob_client_factory)

    def test_disabled_never_invokes_clob_submission(self):
        """THE test (per the issue): COPY_LIVE_TRADING_ENABLED=False must
        produce zero CLOB submission calls, full stop -- regardless of
        every other gate passing."""
        db = _mock_db()
        fake_factory = MagicMock()
        # live_config has COPY_LIVE_TRADING_ENABLED=False (the default from
        # _live_config()) but everything ELSE wide open: no breaker, no
        # exposure caps, a live capital pool that would pass its own
        # sanity check too -- proving the kill switch alone is what stops
        # this, not an incidental gate failure elsewhere.
        live_config = _live_enabled_config(COPY_LIVE_TRADING_ENABLED=False)

        with patch(
            "src.execution.live_trader.LiveTrader.place_order",
        ) as mock_place_order:
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None,
                       clob_client_factory=fake_factory)

        mock_place_order.assert_not_called()
        fake_factory.assert_not_called()
        # Paper still executed normally -- the kill switch only ever
        # affects the live sub-decision (see TestPaperIndependentOfLive).
        db.insert_copy_position.assert_called_once()
        db.insert_copy_live_position.assert_not_called()

    def test_disabled_skips_before_any_live_db_read(self):
        """Belt-and-braces version of the same guarantee at the DB layer:
        get_open_copy_live_positions (the live exposure gate's own read)
        is never even called when the switch is off."""
        db = _mock_db()
        live_config = _live_enabled_config(COPY_LIVE_TRADING_ENABLED=False)
        self._run(db, [_buy_raw()], live_config=live_config, resolution=None)
        db.get_open_copy_live_positions.assert_not_called()

    def test_enabled_and_all_gates_pass_submits_and_records_fill(self):
        db = _mock_db()
        live_config = _live_enabled_config()
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
            return_value={"status": "filled", "order_id": "oid-1", "fill_price": 0.41},
        ) as mock_exec:
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        mock_exec.assert_called_once()
        call_kwargs = mock_exec.call_args.kwargs
        assert call_kwargs["token_id"] == TOKEN_ID
        assert call_kwargs["market"] == MARKET
        assert call_kwargs["stake_usd"] == 10.0
        assert call_kwargs["side_label"] == "YES"  # outcome_index 0

        db.insert_copy_live_position.assert_called_once()
        insert_kwargs = db.insert_copy_live_position.call_args.kwargs
        assert insert_kwargs["address"] == ADDRESS
        assert insert_kwargs["market"] == MARKET
        assert insert_kwargs["stake_usd"] == 10.0

        db.update_copy_live_position_status.assert_called_once_with(
            101, status="filled", order_id="oid-1", fill_price=0.41, rejected_reason=None,
            filled_stake_usd=None,
        )

    def test_enabled_but_no_token_id_on_trade_is_rejected_without_submitting(self):
        db = _mock_db()
        live_config = _live_enabled_config()
        raw = _buy_raw()
        raw.pop("asset", None)  # normalize_trade() falls back to token_id key too
        raw.pop("token_id", None)
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
        ) as mock_exec:
            self._run(db, [raw], live_config=live_config, resolution=None)

        mock_exec.assert_not_called()
        db.insert_copy_live_position.assert_called_once()
        db.update_copy_live_position_status.assert_called_once_with(
            101, status="rejected", rejected_reason="live_missing_token_id",
        )
        # Paper is completely unaffected by the live-side rejection.
        db.insert_copy_position.assert_called_once()

    def test_live_wallet_exposure_limit_rejects_without_submitting(self):
        db = _mock_db()
        db.get_open_copy_live_positions.side_effect = lambda address=None: (
            [{"stake_usd": 45.0}] if address is not None else [{"stake_usd": 45.0}]
        )
        live_config = _live_enabled_config(COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD=50.0)
        # wallet stake_per_trade=10 -> 45 + 10 = 55 > 50 -> live-reject
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
        ) as mock_exec:
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        mock_exec.assert_not_called()
        db.update_copy_live_position_status.assert_called_once_with(
            101, status="rejected", rejected_reason="live_wallet_exposure_limit",
        )
        db.insert_copy_position.assert_called_once()  # paper unaffected

    def test_live_total_exposure_limit_rejects_without_submitting(self):
        db = _mock_db()

        def _open_live_positions(address=None):
            if address is not None:
                return [{"stake_usd": 5.0}]  # well under per-wallet cap
            return [{"stake_usd": 245.0}]  # near total cap

        db.get_open_copy_live_positions.side_effect = _open_live_positions
        live_config = _live_enabled_config(
            COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD=50.0,
            COPY_LIVE_MAX_TOTAL_EXPOSURE_USD=250.0,
        )
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
        ) as mock_exec:
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        mock_exec.assert_not_called()
        db.update_copy_live_position_status.assert_called_once_with(
            101, status="rejected", rejected_reason="live_total_exposure_limit",
        )
        db.insert_copy_position.assert_called_once()

    def test_live_sanity_check_failure_rejects_without_submitting(self):
        """Epic G's COPY_LIVE_MAX_TOTAL_EXPOSURE_USD vs. COPY_LIVE_CAPITAL_USD
        invariant, re-checked every cycle for live (unlike paper's
        main()-only startup_sanity_check) -- see run_cycle's docstring."""
        db = _mock_db()
        live_config = _live_enabled_config(COPY_LIVE_MAX_TOTAL_EXPOSURE_USD=999999.0)
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
        ) as mock_exec:
            # live_capital_usd=250 (the _run default) << 999999 -> sanity check fails
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        mock_exec.assert_not_called()
        rejected_call = db.update_copy_live_position_status.call_args
        assert rejected_call.kwargs["status"] == "rejected"
        assert "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD" in rejected_call.kwargs["rejected_reason"]
        db.insert_copy_position.assert_called_once()

    def test_execute_raising_unexpectedly_is_caught_and_recorded_rejected(self):
        db = _mock_db()
        live_config = _live_enabled_config()
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
            side_effect=RuntimeError("network exploded"),
        ):
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        db.update_copy_live_position_status.assert_called_once()
        call_kwargs = db.update_copy_live_position_status.call_args.kwargs
        assert call_kwargs["status"] == "rejected"
        assert "network exploded" in call_kwargs["rejected_reason"]
        # A live-side crash must never propagate and must never touch the
        # already-committed paper rows or crash the wallet's cycle.
        db.insert_copy_position.assert_called_once()

    def test_paper_gate_skip_means_live_never_even_considered(self):
        """Live sits strictly downstream of paper's own execute branch --
        when paper itself skips (e.g. market_resolved), live must never be
        attempted at all, even with COPY_LIVE_TRADING_ENABLED=True and
        every live-specific gate wide open."""
        db = _mock_db()
        live_config = _live_enabled_config()
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
        ) as mock_exec:
            self._run(db, [_buy_raw()], live_config=live_config, resolution=True)  # resolved

        mock_exec.assert_not_called()
        db.insert_copy_live_position.assert_not_called()
        db.insert_copy_position.assert_not_called()

    def test_rejected_clob_submission_is_recorded_with_order_id_when_present(self):
        db = _mock_db()
        live_config = _live_enabled_config()
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
            return_value={"status": "rejected", "order_id": "oid-timeout", "rejected_reason": "timeout"},
        ):
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        db.update_copy_live_position_status.assert_called_once_with(
            101, status="rejected", order_id="oid-timeout", rejected_reason="timeout",
            fill_price=None, filled_stake_usd=None,
        )

    def test_ghost_order_rejection_preserves_fill_price_for_later_recovery(self):
        """A ghost order (rejected_reason='cancel_failed_ghost', #1174)
        carries its placed fill_price through to the DB write even though
        status='rejected' -- recover_ghost_orders() needs it later to value
        a fill discovered on recheck. This is the one 'rejected' case where
        fill_price is NOT None."""
        db = _mock_db()
        live_config = _live_enabled_config()
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
            return_value={
                "status": "rejected", "order_id": "oid-ghost",
                "rejected_reason": "cancel_failed_ghost", "fill_price": 0.40,
            },
        ):
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        db.update_copy_live_position_status.assert_called_once_with(
            101, status="rejected", order_id="oid-ghost",
            rejected_reason="cancel_failed_ghost", fill_price=0.40, filled_stake_usd=None,
        )

    def test_partial_fill_is_recorded_with_partial_status(self):
        db = _mock_db()
        live_config = _live_enabled_config()
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
            return_value={"status": "partial", "order_id": "oid-2", "fill_price": 0.40},
        ):
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        db.update_copy_live_position_status.assert_called_once_with(
            101, status="partial", order_id="oid-2", fill_price=0.40, rejected_reason=None,
            filled_stake_usd=None,
        )

    def test_partial_fill_with_filled_stake_usd_is_passed_through(self):
        """Issue #1171 item 3 / #1174: when execute_live_copy_order reports
        the actual USD spent on a confirmed partial fill, it must reach the
        DB write -- not be silently dropped -- so settlement can later
        compute P&L from what actually filled, not the full intended stake."""
        db = _mock_db()
        live_config = _live_enabled_config()
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
            return_value={
                "status": "partial", "order_id": "oid-2", "fill_price": 0.40,
                "filled_stake_usd": 3.5,
            },
        ):
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        db.update_copy_live_position_status.assert_called_once_with(
            101, status="partial", order_id="oid-2", fill_price=0.40, rejected_reason=None,
            filled_stake_usd=3.5,
        )

    def test_db_write_failure_after_real_fill_logs_critical_and_does_not_raise(self, caplog):
        """The order already filled on the exchange by the time this write
        is attempted -- a failure here must be LOUD (CRITICAL), never a
        quiet propagate-and-crash-the-cycle or a quiet swallow. Mirrors
        LiveTrader.place_order's own "CLOB order placed but DB write
        failed" precedent."""
        db = _mock_db()
        db.update_copy_live_position_status.side_effect = RuntimeError("db locked")
        live_config = _live_enabled_config()
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
            return_value={"status": "filled", "order_id": "oid-3", "fill_price": 0.41},
        ), caplog.at_level("CRITICAL"):
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        assert any(
            "CRITICAL" in rec.message and "oid-3" in rec.message
            for rec in caplog.records
        )
        # Paper is unaffected and the cycle did not crash.
        db.insert_copy_position.assert_called_once()


class TestLiveCircuitBreaker:
    """Live-specific realized-P&L circuit breaker wiring (issue #1175, epic
    I #1160). Mirrors TestCircuitBreaker's structure exactly, one layer up:
    never mocks src.risk.copy_risk_manager.allow_live_copy_signal itself --
    exercises the real function against the mocked db's
    get_copy_live_realized_pnl_total[_for_date], same "mock only the DB and
    network boundary" style as everywhere else in this file.

    Per issue #1175's own explicit requirement, the tests below prove the
    breaker actually HALTS live order placement (asserting
    execute_live_copy_order / LiveTrader.place_order are never called and
    the copy_live_positions row is written with status='rejected') -- not
    merely that a log line is emitted.
    """

    def _run(
        self, db, raw_trades, live_config=None, resolution=None, live_capital_usd=250.0,
    ):
        with patch(
            "src.scripts.copy_signal_loop.get_live_config",
            return_value=live_config if live_config is not None else _live_enabled_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since",
            return_value=raw_trades,
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution",
            return_value=resolution,
        ), patch(
            "src.scripts.copy_signal_loop.COPY_LIVE_CAPITAL_USD", live_capital_usd,
        ):
            run_cycle(db)

    def test_tripped_live_daily_loss_halts_placement_and_records_rejection(self):
        db = _mock_db()
        db.get_copy_live_realized_pnl_total_for_date.return_value = {
            "n_settled": 3, "total_pnl_usd": -30.0,
        }
        live_config = _live_enabled_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
        ) as mock_exec, patch(
            "src.execution.live_trader.LiveTrader.place_order",
        ) as mock_place_order:
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        # The breaker halts placement -- it is never even attempted, not
        # just logged as a rejection after the fact.
        mock_exec.assert_not_called()
        mock_place_order.assert_not_called()

        db.insert_copy_live_position.assert_called_once()
        db.update_copy_live_position_status.assert_called_once_with(
            101, status="rejected", rejected_reason="live_circuit_breaker_daily_loss",
        )
        # Paper is completely unaffected by the live breaker tripping.
        db.insert_copy_position.assert_called_once()

    def test_tripped_live_drawdown_halts_placement_and_records_rejection(self):
        db = _mock_db()
        db.get_copy_live_realized_pnl_total.return_value = {
            "n_settled": 5, "total_pnl_usd": -50.0,
        }
        live_config = _live_enabled_config(
            COPY_LIVE_DAILY_LOSS_LIMIT_USD=999.0, COPY_LIVE_DRAWDOWN_STOP_PCT=0.20,
        )
        with patch("src.risk.copy_risk_manager.COPY_LIVE_CAPITAL_USD", 100.0), patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
        ) as mock_exec:
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        mock_exec.assert_not_called()
        db.update_copy_live_position_status.assert_called_once_with(
            101, status="rejected", rejected_reason="live_circuit_breaker_drawdown",
        )
        db.insert_copy_position.assert_called_once()

    def test_not_tripped_does_not_change_normal_live_execution(self):
        db = _mock_db()
        live_config = _live_enabled_config()
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
            return_value={"status": "filled", "order_id": "oid-1", "fill_price": 0.41},
        ) as mock_exec:
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        mock_exec.assert_called_once()
        db.update_copy_live_position_status.assert_called_once_with(
            101, status="filled", order_id="oid-1", fill_price=0.41, rejected_reason=None,
            filled_stake_usd=None,
        )

    def test_live_breaker_checked_once_per_cycle_not_per_wallet(self):
        wallet_a = _wallet(address="0xwalletA")
        wallet_b = _wallet(address="0xwalletB")
        db = _mock_db()
        db.get_followed_wallets.return_value = [wallet_a, wallet_b]
        db.get_copy_live_realized_pnl_total_for_date.return_value = {
            "n_settled": 1, "total_pnl_usd": -30.0,
        }
        live_config = _live_enabled_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
        ) as mock_exec:
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        mock_exec.assert_not_called()
        db.get_copy_live_realized_pnl_total_for_date.assert_called_once()
        assert db.update_copy_live_position_status.call_count == 2
        for call in db.update_copy_live_position_status.call_args_list:
            assert call.kwargs["rejected_reason"] == "live_circuit_breaker_daily_loss"

    def test_live_sanity_check_takes_precedence_over_live_breaker(self):
        """When BOTH the live sanity check and the live breaker would trip,
        the (pre-existing, #1163) sanity check runs first and wins -- the
        breaker is a strictly-additional gate layered on top of it, never a
        replacement, and both still funnel through the same
        live_gate_reason mechanism."""
        db = _mock_db()
        db.get_copy_live_realized_pnl_total_for_date.return_value = {
            "n_settled": 1, "total_pnl_usd": -30.0,
        }
        live_config = _live_enabled_config(
            COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0,
            COPY_LIVE_MAX_TOTAL_EXPOSURE_USD=999999.0,
        )
        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
        ) as mock_exec:
            # live_capital_usd=250 (the _run default) << 999999 -> sanity check fails
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)

        mock_exec.assert_not_called()
        rejected_call = db.update_copy_live_position_status.call_args
        assert rejected_call.kwargs["status"] == "rejected"
        assert "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD" in rejected_call.kwargs["rejected_reason"]

    def test_disabled_live_never_queries_live_breaker(self):
        """The kill switch is still checked first (issue #1167) -- when live
        is off, the live breaker's own DB reads never happen at all."""
        db = _mock_db()
        live_config = _live_enabled_config(COPY_LIVE_TRADING_ENABLED=False)
        self._run(db, [_buy_raw()], live_config=live_config, resolution=None)
        db.get_copy_live_realized_pnl_total_for_date.assert_not_called()
        db.get_copy_live_realized_pnl_total.assert_not_called()

    def test_never_touches_paper_breaker_queries(self):
        """The live breaker must never read paper's own realized-P&L
        methods -- same table isolation #1175 requires, exercised end to
        end through run_cycle rather than just the risk-manager unit test."""
        db = _mock_db()
        db.get_copy_live_realized_pnl_total_for_date.return_value = {
            "n_settled": 1, "total_pnl_usd": -30.0,
        }
        live_config = _live_enabled_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch("src.scripts.copy_signal_loop.execute_live_copy_order"):
            self._run(db, [_buy_raw()], live_config=live_config, resolution=None)
        # Paper's own breaker query is still called once (it's part of the
        # separate paper breaker check), but it must reflect paper's own
        # (zero) P&L, never be short-circuited or replaced by the live one.
        db.get_copy_realized_pnl_total_for_date.assert_called_once()
        assert db.insert_copy_position.call_count == 1


class TestWalletAutoPauseHaltsLiveExecution:
    """Issue #1177 regression coverage. ``copy_wallet_health.py``'s
    auto-pause calls ``db.update_followed_wallet_status(address, "paused",
    reason)``, which removes the wallet from
    ``db.get_followed_wallets(status="active")`` entirely -- and
    ``run_cycle``'s ``for wallet in db.get_followed_wallets(status="active")``
    is the ONE loop that both the paper path (``_process_wallet`` /
    ``_handle_buy_trade``) and the live path (``_handle_live_order``, added
    by #1167) are nested inside. A wallet absent from that list is therefore
    already skipped for both paper and live -- there is no separate live-only
    active-status gate to build; the existing one already covers it.

    These tests prove that end to end via a full ``run_cycle()``, with
    ``COPY_LIVE_TRADING_ENABLED=True`` and every live-specific gate
    (exposure, sanity check) wide open -- mirroring ``TestLiveExecution``'s
    own style of patching the real CLOB entry point
    (``LiveTrader.place_order``) and/or ``execute_live_copy_order`` and
    asserting zero calls, so a hypothetical future bug that re-introduced a
    live-only wallet fetch (bypassing the shared active-status list) would
    be caught here.
    """

    def _run(
        self, db, raw_trades, live_config=None, resolution=None,
        clob_client_factory=None, live_capital_usd=250.0,
    ):
        with patch(
            "src.scripts.copy_signal_loop.get_live_config",
            return_value=live_config if live_config is not None else _live_enabled_config(),
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since",
            return_value=raw_trades,
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution",
            return_value=resolution,
        ), patch(
            "src.scripts.copy_signal_loop.COPY_LIVE_CAPITAL_USD", live_capital_usd,
        ):
            run_cycle(db, clob_client_factory=clob_client_factory)

    def test_paused_wallet_never_reaches_live_order_placement(self):
        """A wallet excluded from get_followed_wallets(status="active")
        (the auto-pause outcome) must never have its trades fetched at all,
        let alone reach _handle_live_order / execute_live_copy_order --
        even with live trading enabled and every live gate wide open."""
        db = _mock_db(get_followed_wallets=[])  # no active wallets: the only
        # followed wallet was just auto-paused by copy_wallet_health.py.
        fake_factory = MagicMock()

        with patch(
            "src.execution.live_trader.LiveTrader.place_order",
        ) as mock_place_order, patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
        ) as mock_exec, patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since",
        ) as mock_fetch_trades:
            self._run(
                db, raw_trades=[_buy_raw()], live_config=_live_enabled_config(),
                clob_client_factory=fake_factory,
            )

        # The wallet's trades are never even fetched -- it is filtered out
        # before the per-wallet loop body ever runs.
        mock_fetch_trades.assert_not_called()
        fake_factory.assert_not_called()
        mock_place_order.assert_not_called()
        mock_exec.assert_not_called()

        # Neither paper nor live ever wrote anything for this cycle.
        db.insert_copy_signal.assert_not_called()
        db.insert_copy_position.assert_not_called()
        db.insert_copy_live_position.assert_not_called()

        # Confirms the loop queried the SAME active-only filter that
        # copy_wallet_health.py's auto-pause relies on to exclude a paused
        # wallet -- not some other, live-specific query.
        db.get_followed_wallets.assert_called_once_with(status="active")

    def test_active_wallet_alongside_a_paused_one_still_executes_live(self):
        """Sanity check for the test above: with an ACTIVE wallet returned
        by get_followed_wallets(status="active") (i.e. the paused wallet
        already filtered out server-side, exactly like the real
        Database.get_followed_wallets SQL WHERE clause), live execution
        proceeds normally -- proving the zero-calls result above is because
        the wallet was excluded from the list, not because live execution
        is broken outright."""
        db = _mock_db()  # default: one active wallet (see _wallet()'s
        # status="active" default), simulating a paused sibling wallet
        # already excluded by the same query.

        with patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
            return_value={"status": "filled", "order_id": "oid-active", "fill_price": 0.41},
        ) as mock_exec:
            self._run(db, raw_trades=[_buy_raw()], live_config=_live_enabled_config())

        mock_exec.assert_called_once()
        db.insert_copy_live_position.assert_called_once()

    def test_paused_wallet_excluded_from_get_followed_wallets_result(self):
        """Direct regression test for the underlying DB contract this whole
        guarantee rests on: Database.get_followed_wallets(status="active")
        performs a hard SQL filter (WHERE status=?), so a wallet
        update_followed_wallet_status() just flipped to "paused" is not
        merely flagged -- it is entirely absent from the very list
        run_cycle() iterates over for both paper and live."""
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE copy_wallets_followed ("
            "address TEXT PRIMARY KEY, status TEXT, paused_reason TEXT, "
            "paused_at TEXT, stake_per_trade REAL, added_at TEXT, "
            "last_seen_trade_ts INTEGER)"
        )
        conn.execute(
            "INSERT INTO copy_wallets_followed "
            "(address, status, stake_per_trade, added_at) "
            "VALUES (?, 'active', 10.0, ?)",
            (ADDRESS, NOW_ISO),
        )
        conn.commit()

        from src.data.db import Database
        db = Database.__new__(Database)  # bypass __init__ (no real file/schema setup)
        db._conn = conn
        db._lock = __import__("threading").RLock()

        assert [w["address"] for w in db.get_followed_wallets(status="active")] == [ADDRESS]

        db.update_followed_wallet_status(ADDRESS, "paused", "stability_check_failed")

        assert db.get_followed_wallets(status="active") == []
        conn.close()


class TestPaperIndependentOfLive:
    """Issue #1167 acceptance criterion, the OPPOSITE direction from the
    kill-switch test above: paper execution must be completely
    unconditional and independent of live mode's state. Never mocks
    execute_live_copy_order away as a no-op-implying shortcut -- it patches
    the real CLOB entry point (LiveTrader.place_order) exactly like
    TestLiveExecution's own primary test, so a hypothetical future bug that
    made paper depend on a live call succeeding would show up here too.
    """

    def _run(self, db, raw_trades, live_config, resolution=None, live_capital_usd=250.0):
        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=live_config,
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since", return_value=raw_trades,
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution", return_value=resolution,
        ), patch(
            "src.scripts.copy_signal_loop.COPY_LIVE_CAPITAL_USD", live_capital_usd,
        ), patch(
            "src.execution.live_trader.LiveTrader.place_order",
            return_value="oid-independent",
        ), patch(
            "src.execution.live_trader.LiveTrader.check_fill", return_value="filled",
        ), patch(
            "src.execution.copy_live_executor.time.sleep",
        ):
            run_cycle(db, clob_client_factory=MagicMock())

    def test_paper_executes_identically_whether_live_is_on_or_off(self):
        db_live_off = _mock_db()
        db_live_on = _mock_db()

        self._run(db_live_off, [_buy_raw()], _live_enabled_config(COPY_LIVE_TRADING_ENABLED=False))
        self._run(db_live_on, [_buy_raw()], _live_enabled_config(COPY_LIVE_TRADING_ENABLED=True))

        for db in (db_live_off, db_live_on):
            db.insert_copy_position.assert_called_once()
            position_kwargs = db.insert_copy_position.call_args.kwargs
            assert position_kwargs["stake_usd"] == 10.0
            assert position_kwargs["address"] == ADDRESS
            assert position_kwargs["market"] == MARKET

        # Live-on additionally attempted a live position; live-off did not.
        db_live_off.insert_copy_live_position.assert_not_called()
        db_live_on.insert_copy_live_position.assert_called_once()

    def test_live_side_failure_never_rolls_back_or_blocks_paper(self):
        """A live order that outright fails to place must never affect the
        already-committed paper copy_signals/copy_positions rows."""
        db = _mock_db()
        live_config = _live_enabled_config()
        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=live_config,
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since", return_value=[_buy_raw()],
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution", return_value=None,
        ), patch(
            "src.scripts.copy_signal_loop.COPY_LIVE_CAPITAL_USD", 250.0,
        ), patch(
            "src.execution.live_trader.LiveTrader.place_order",
            side_effect=RuntimeError("insufficient balance"),
        ):
            run_cycle(db, clob_client_factory=MagicMock())

        db.insert_copy_signal.assert_called_once()
        assert db.insert_copy_signal.call_args.kwargs["order_placed"] == 1
        db.insert_copy_position.assert_called_once()
        db.link_copy_signal_to_position.assert_called_once()

    def test_paper_breaker_trips_without_tripping_live_breaker(self):
        """Issue #1175 acceptance criterion, direction 1: paper's realized
        P&L (copy_positions) breaching its own daily-loss limit must NOT
        halt live order placement -- the two breakers read different
        tables and never share config keys."""
        db = _mock_db()
        db.get_copy_realized_pnl_total_for_date.return_value = {
            "n_settled": 1, "total_pnl_usd": -30.0,
        }
        live_config = _live_enabled_config(COPY_DAILY_LOSS_LIMIT_USD=25.0)
        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=live_config,
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since", return_value=[_buy_raw()],
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution", return_value=None,
        ), patch(
            "src.scripts.copy_signal_loop.COPY_LIVE_CAPITAL_USD", 250.0,
        ), patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
            return_value={"status": "filled", "order_id": "oid-live", "fill_price": 0.41},
        ) as mock_exec:
            run_cycle(db)

        # Paper itself was skipped by its own tripped breaker...
        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["skip_reason"] == "circuit_breaker_daily_loss"
        db.insert_copy_position.assert_not_called()
        # ...but live is a pure downstream-of-paper addition (see
        # _handle_buy_trade), so when paper itself never executes, live is
        # never even attempted either -- this is the pre-existing #1167
        # "never live-only" guarantee, not a new #1175 behavior. What #1175
        # adds is proven by the sibling test below: a genuinely independent
        # live-side trip that paper's own breaker knows nothing about.
        mock_exec.assert_not_called()
        db.insert_copy_live_position.assert_not_called()

    def test_live_breaker_trips_without_tripping_paper_breaker(self):
        """Issue #1175 acceptance criterion, direction 2: live's realized
        P&L (copy_live_positions) breaching its own daily-loss limit must
        NOT halt paper execution -- paper keeps running completely
        independently, exactly like TestPaperIndependentOfLive's other
        tests already establish for the kill switch and live-side
        failures."""
        db = _mock_db()
        db.get_copy_live_realized_pnl_total_for_date.return_value = {
            "n_settled": 1, "total_pnl_usd": -30.0,
        }
        live_config = _live_enabled_config(COPY_LIVE_DAILY_LOSS_LIMIT_USD=25.0)
        with patch(
            "src.scripts.copy_signal_loop.get_live_config", return_value=live_config,
        ), patch(
            "src.scripts.copy_signal_loop.get_wallet_trades_since", return_value=[_buy_raw()],
        ), patch(
            "src.scripts.copy_signal_loop.fetch_market_resolution", return_value=None,
        ), patch(
            "src.scripts.copy_signal_loop.COPY_LIVE_CAPITAL_USD", 250.0,
        ), patch(
            "src.scripts.copy_signal_loop.execute_live_copy_order",
        ) as mock_exec:
            run_cycle(db)

        # Live was halted by its own tripped breaker...
        mock_exec.assert_not_called()
        db.update_copy_live_position_status.assert_called_once_with(
            101, status="rejected", rejected_reason="live_circuit_breaker_daily_loss",
        )
        # ...but paper is completely unaffected: it executed normally, with
        # no skip_reason at all.
        signal_kwargs = db.insert_copy_signal.call_args.kwargs
        assert signal_kwargs["order_placed"] == 1
        assert signal_kwargs.get("skip_reason") is None
        db.insert_copy_position.assert_called_once()
