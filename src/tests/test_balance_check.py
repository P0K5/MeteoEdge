"""Tests for balance-check fix (issue #286).

Covers:
- Exception path: balance check raises -> available_usdc=0.0, risk_manager.allow_trade NOT called
- Empty-wallet cooldown: first empty-wallet observation sets cooldown; subsequent calls skip
- Alert threshold: N consecutive failures -> AlertManager._fire called; success resets counter
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

import src.scripts.run as run_module
from src.scripts.run import poll_once


def _make_mock_db():
    mock_db = MagicMock()
    mock_db.get_all_config.return_value = {}
    mock_db.get_config.return_value = None
    return mock_db


def _make_mock_risk():
    mock_risk = MagicMock()
    mock_risk.allow_trade.return_value = (True, "ok")
    mock_risk._daily_pnl = 0.0
    return mock_risk


def _make_live_trader(balance_side_effect=None, balance_return=100.0):
    mock_trader = MagicMock()
    if balance_side_effect is not None:
        mock_trader.get_usdc_balance.side_effect = balance_side_effect
    else:
        mock_trader.get_usdc_balance.return_value = balance_return
    return mock_trader


def _common_ctx(scan_return=([], []), live_trader=None):
    """Return the context managers needed to run poll_once without real I/O."""
    patches = [
        patch("src.scripts.run._build_weather", return_value={"Tokyo": MagicMock()}),
        patch("src.scripts.run.get_weather_markets", return_value=[]),
        patch("src.scripts.run.scan_markets", return_value=scan_return),
        patch("src.scripts.run._append_candidate"),
        patch("src.scripts.run._append_snapshot"),
        patch("src.scripts.run.FreshnessMonitor"),
        patch("src.scripts.run.get_source_priority", return_value=[]),
        patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
    ]
    if live_trader is not None:
        patches += [
            patch.object(run_module.order_manager, "reconcile_timeout_fills"),
            patch.object(run_module.order_manager, "sync_open_orders"),
            patch.object(run_module.order_manager, "check_take_profit_exits"),
            patch("src.scripts.run._log_open_position_snapshots"),
        ]
    return patches


class TestBalanceCheckException:
    def setup_method(self):
        run_module._balance_fail_count = 0
        run_module._wallet_cooldown_until = 0.0

    def test_exception_prevents_allow_trade_call(self):
        """When balance check raises, no candidate should reach risk_manager.allow_trade."""
        mock_risk = _make_mock_risk()
        mock_trader = _make_live_trader(balance_side_effect=RuntimeError("API down"))
        mock_db = _make_mock_db()

        mock_cand = MagicMock()
        mock_cand.shadow = False
        mock_cand.bracket.yes_ask_size = 100.0
        mock_cand.bracket.no_ask_size = 100.0

        with (
            patch("src.scripts.run._build_weather", return_value={"Tokyo": MagicMock()}),
            patch("src.scripts.run.get_weather_markets", return_value=[]),
            patch("src.scripts.run.scan_markets", return_value=([mock_cand], [])),
            patch.object(run_module.order_manager, "reconcile_timeout_fills"),
            patch.object(run_module.order_manager, "sync_open_orders"),
            patch.object(run_module.order_manager, "check_take_profit_exits"),
            patch("src.scripts.run._log_open_position_snapshots"),
            patch("src.scripts.run.FreshnessMonitor"),
            patch("src.scripts.run.get_source_priority", return_value=[]),
            patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
            patch("src.scripts.run._append_candidate"),
            patch("src.scripts.run._append_snapshot"),
        ):
            poll_once(mock_risk, live_trader=mock_trader, alert_manager=None, db=mock_db)

        mock_risk.allow_trade.assert_not_called()

    def test_exception_increments_fail_count(self):
        mock_risk = _make_mock_risk()
        mock_trader = _make_live_trader(balance_side_effect=RuntimeError("API down"))
        mock_db = _make_mock_db()
        run_module._balance_fail_count = 0

        with (
            patch("src.scripts.run._build_weather", return_value={"Tokyo": MagicMock()}),
            patch("src.scripts.run.get_weather_markets", return_value=[]),
            patch("src.scripts.run.scan_markets", return_value=([], [])),
            patch.object(run_module.order_manager, "reconcile_timeout_fills"),
            patch.object(run_module.order_manager, "sync_open_orders"),
            patch.object(run_module.order_manager, "check_take_profit_exits"),
            patch("src.scripts.run._log_open_position_snapshots"),
            patch("src.scripts.run.FreshnessMonitor"),
            patch("src.scripts.run.get_source_priority", return_value=[]),
            patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
            patch("src.scripts.run._append_candidate"),
            patch("src.scripts.run._append_snapshot"),
        ):
            poll_once(mock_risk, live_trader=mock_trader, alert_manager=None, db=mock_db)

        assert run_module._balance_fail_count == 1

    def test_success_resets_fail_count(self):
        mock_risk = _make_mock_risk()
        mock_trader = _make_live_trader(balance_return=500.0)
        mock_db = _make_mock_db()
        run_module._balance_fail_count = 5

        with (
            patch("src.scripts.run._build_weather", return_value={"Tokyo": MagicMock()}),
            patch("src.scripts.run.get_weather_markets", return_value=[]),
            patch("src.scripts.run.scan_markets", return_value=([], [])),
            patch.object(run_module.order_manager, "reconcile_timeout_fills"),
            patch.object(run_module.order_manager, "sync_open_orders"),
            patch.object(run_module.order_manager, "check_take_profit_exits"),
            patch("src.scripts.run._log_open_position_snapshots"),
            patch("src.scripts.run.FreshnessMonitor"),
            patch("src.scripts.run.get_source_priority", return_value=[]),
            patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
            patch("src.scripts.run._append_candidate"),
            patch("src.scripts.run._append_snapshot"),
        ):
            poll_once(mock_risk, live_trader=mock_trader, alert_manager=None, db=mock_db)

        assert run_module._balance_fail_count == 0


class TestWalletEmptyCooldown:
    def setup_method(self):
        run_module._balance_fail_count = 0
        run_module._wallet_cooldown_until = 0.0

    def test_cooldown_set_when_wallet_empty(self):
        """When available_usdc < POSITION_SIZE_WITH_FEES, _wallet_cooldown_until must be set ~1800s ahead."""
        mock_risk = _make_mock_risk()
        mock_trader = _make_live_trader(balance_return=0.0)
        mock_db = _make_mock_db()

        mock_cand = MagicMock()
        mock_cand.shadow = False
        mock_cand.bracket.yes_ask_size = 100.0
        mock_cand.bracket.no_ask_size = 100.0

        before = time.time()

        with (
            patch("src.scripts.run._build_weather", return_value={"Tokyo": MagicMock()}),
            patch("src.scripts.run.get_weather_markets", return_value=[]),
            patch("src.scripts.run.scan_markets", return_value=([mock_cand], [])),
            patch.object(run_module.order_manager, "reconcile_timeout_fills"),
            patch.object(run_module.order_manager, "sync_open_orders"),
            patch.object(run_module.order_manager, "check_take_profit_exits"),
            patch("src.scripts.run._log_open_position_snapshots"),
            patch("src.scripts.run.FreshnessMonitor"),
            patch("src.scripts.run.get_source_priority", return_value=[]),
            patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
            patch("src.scripts.run._append_candidate"),
            patch("src.scripts.run._append_snapshot"),
        ):
            poll_once(mock_risk, live_trader=mock_trader, alert_manager=None, db=mock_db)

        assert run_module._wallet_cooldown_until > before + 1700

    def test_cooldown_active_skips_balance_check_and_candidates(self):
        """When cooldown is active, poll_once returns early without calling get_usdc_balance."""
        mock_risk = _make_mock_risk()
        mock_trader = _make_live_trader(balance_return=500.0)
        mock_db = _make_mock_db()

        run_module._wallet_cooldown_until = time.time() + 9999.0

        with (
            patch("src.scripts.run._build_weather", return_value={"Tokyo": MagicMock()}),
            patch("src.scripts.run.get_weather_markets", return_value=[]),
            patch.object(run_module.order_manager, "reconcile_timeout_fills"),
            patch.object(run_module.order_manager, "sync_open_orders"),
            patch.object(run_module.order_manager, "check_take_profit_exits"),
            patch("src.scripts.run._log_open_position_snapshots"),
            patch("src.scripts.run.FreshnessMonitor"),
            patch("src.scripts.run.get_source_priority", return_value=[]),
            patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
        ):
            poll_once(mock_risk, live_trader=mock_trader, alert_manager=None, db=mock_db)

        mock_trader.get_usdc_balance.assert_not_called()
        mock_risk.allow_trade.assert_not_called()

    def test_cooldown_expired_allows_normal_scan(self):
        """Once the cooldown window passes, normal scanning resumes."""
        mock_risk = _make_mock_risk()
        mock_trader = _make_live_trader(balance_return=500.0)
        mock_db = _make_mock_db()

        run_module._wallet_cooldown_until = time.time() - 1.0

        with (
            patch("src.scripts.run._build_weather", return_value={"Tokyo": MagicMock()}),
            patch("src.scripts.run.get_weather_markets", return_value=[]),
            patch("src.scripts.run.scan_markets", return_value=([], [])),
            patch.object(run_module.order_manager, "reconcile_timeout_fills"),
            patch.object(run_module.order_manager, "sync_open_orders"),
            patch.object(run_module.order_manager, "check_take_profit_exits"),
            patch("src.scripts.run._log_open_position_snapshots"),
            patch("src.scripts.run.FreshnessMonitor"),
            patch("src.scripts.run.get_source_priority", return_value=[]),
            patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
            patch("src.scripts.run._append_candidate"),
            patch("src.scripts.run._append_snapshot"),
        ):
            poll_once(mock_risk, live_trader=mock_trader, alert_manager=None, db=mock_db)

        mock_trader.get_usdc_balance.assert_called_once()


class TestBalanceFailAlertThreshold:
    """After BALANCE_CHECK_FAIL_ALERT_THRESHOLD consecutive failures, AlertManager._fire is called."""

    def setup_method(self):
        run_module._balance_fail_count = 0
        run_module._wallet_cooldown_until = 0.0

    def _run_once_fail(self, alert_manager):
        mock_risk = _make_mock_risk()
        mock_trader = _make_live_trader(balance_side_effect=RuntimeError("API down"))
        mock_db = _make_mock_db()

        with (
            patch("src.scripts.run._build_weather", return_value={"Tokyo": MagicMock()}),
            patch("src.scripts.run.get_weather_markets", return_value=[]),
            patch("src.scripts.run.scan_markets", return_value=([], [])),
            patch.object(run_module.order_manager, "reconcile_timeout_fills"),
            patch.object(run_module.order_manager, "sync_open_orders"),
            patch.object(run_module.order_manager, "check_take_profit_exits"),
            patch("src.scripts.run._log_open_position_snapshots"),
            patch("src.scripts.run.FreshnessMonitor"),
            patch("src.scripts.run.get_source_priority", return_value=[]),
            patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
            patch("src.scripts.run._append_candidate"),
            patch("src.scripts.run._append_snapshot"),
        ):
            poll_once(mock_risk, live_trader=mock_trader, alert_manager=alert_manager, db=mock_db)

    def test_alert_fired_after_threshold(self):
        """AlertManager._fire must be called once the failure counter reaches default threshold (3)."""
        mock_alert = MagicMock()
        for _ in range(3):
            self._run_once_fail(mock_alert)

        mock_alert._fire.assert_called()
        call_args = mock_alert._fire.call_args[0]
        assert call_args[0] == "balance_check_failure"
        assert "ERROR" in call_args[1]

    def test_alert_not_fired_below_threshold(self):
        """AlertManager._fire must NOT be called before the threshold is reached."""
        mock_alert = MagicMock()
        for _ in range(2):
            self._run_once_fail(mock_alert)

        mock_alert._fire.assert_not_called()

    def test_success_resets_counter_no_alert_refired(self):
        """After a successful balance check, failure counter resets and N new failures are needed."""
        mock_alert = MagicMock()

        for _ in range(2):
            self._run_once_fail(mock_alert)

        # Successful balance check resets counter
        mock_risk = _make_mock_risk()
        mock_trader = _make_live_trader(balance_return=100.0)
        mock_db = _make_mock_db()
        with (
            patch("src.scripts.run._build_weather", return_value={"Tokyo": MagicMock()}),
            patch("src.scripts.run.get_weather_markets", return_value=[]),
            patch("src.scripts.run.scan_markets", return_value=([], [])),
            patch.object(run_module.order_manager, "reconcile_timeout_fills"),
            patch.object(run_module.order_manager, "sync_open_orders"),
            patch.object(run_module.order_manager, "check_take_profit_exits"),
            patch("src.scripts.run._log_open_position_snapshots"),
            patch("src.scripts.run.FreshnessMonitor"),
            patch("src.scripts.run.get_source_priority", return_value=[]),
            patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
            patch("src.scripts.run._append_candidate"),
            patch("src.scripts.run._append_snapshot"),
        ):
            poll_once(mock_risk, live_trader=mock_trader, alert_manager=mock_alert, db=mock_db)

        assert run_module._balance_fail_count == 0
        mock_alert._fire.assert_not_called()

        # 2 more failures -- still below threshold
        for _ in range(2):
            self._run_once_fail(mock_alert)

        mock_alert._fire.assert_not_called()
