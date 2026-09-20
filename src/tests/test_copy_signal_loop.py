"""Unit tests for src/scripts/copy_signal_loop.py (epic #1101 story B3,
issue #1123). Fully mocked -- no real DB or network calls, mirroring
test_copy_wallet_screening.py's mocking style (MagicMock db, patched
module-level functions).
"""
from unittest.mock import MagicMock, patch

import pytest

from src.config import CONFIG_DEFAULTS
from src.scripts.copy_signal_loop import (
    main,
    run_cycle,
    startup_sanity_check,
)

ADDRESS = "0xwallet1"
MARKET = "0xmarket1"
NOW_ISO = "2026-09-20T00:00:00+00:00"


def _wallet(**overrides) -> dict:
    kwargs = dict(
        address=ADDRESS,
        stake_per_trade=10.0,
        status="active",
        paused_reason=None,
        added_at="2026-09-19T00:00:00+00:00",
        last_seen_trade_ts=None,
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
