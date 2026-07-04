"""Regression tests for the issue #611 live entry gate in run.py.

The bug: candidate evaluation had no "already holding this token/bracket
today" check, so the bot re-entered the same bracket roughly once per poll
(7x KATL 98-99F on 2026-07-02, 3 filled + 5 timeout WMKK orders on
2026-07-03), multiplying the intended flat 5 EUR exposure 3-7x.

Covers:
- Two consecutive polls flagging the same bracket produce exactly ONE live
  order; the second is blocked with an [entry-guard] warning.
- Two duplicate candidates in the SAME poll: only the first passes.
- A DIFFERENT bracket/side/station is not over-blocked.
- LIVE_ALLOW_BRACKET_REENTRY=true allows re-entry after a sold exit but
  still blocks while an open position exists for the token.

No calendar dates are hardcoded: every market endDate derives from
datetime.now(timezone.utc) at test run time.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import src.scripts.run as run_module
from src.data.db import Database
from src.model.envelope import Bracket
from src.scripts.run import poll_once
from src.strategy.scanner import Candidate


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _make_candidate(
    station: str = "KATL",
    ticker: str = "0xkatl-98-99",
    side: str = "NO",
    no_token: str = "token-katl-no-1",
    yes_token: str = "token-katl-yes-1",
) -> Candidate:
    bracket = Bracket(
        ticker=ticker,
        low_f=98.0,
        high_f=99.0,
        yes_ask_cents=30,
        yes_ask_size=100,
        no_ask_cents=70,
        no_ask_size=100,
        yes_token_id=yes_token,
        no_token_id=no_token,
    )
    return Candidate(
        station=station,
        bracket=bracket,
        side=side,
        edge_cents=16.0,
        price_cents=70,
        confidence=0.86,
        p_yes=0.14,
        ev_yes=-10.0,
        ev_no=16.0,
        minutes_to_settlement=300.0,
        market={"question": f"{station} high temp", "endDate": f"{_today()}T23:59:00Z"},
        shadow=False,
    )


def _make_risk() -> MagicMock:
    risk = MagicMock()
    risk.allow_trade.return_value = (True, "ok")
    risk._daily_pnl = 0.0
    return risk


def _make_trader() -> MagicMock:
    trader = MagicMock()
    trader.get_usdc_balance.return_value = 100.0
    trader._client_factory = MagicMock()
    return trader


def _fake_execute_factory(db, calls: list):
    """Return an _execute_live stand-in that records to the DB exactly like the
    real seam: trades row at placement (synthetic ticker), open_positions row,
    then the outcome update that rewrites the real ticker (see
    live_trader.place_order + run._append_live_trade)."""

    def _fake_execute(cand, client_factory, risk_manager, ts, db_arg, bankroll):
        calls.append(cand)
        order_id = f"test-order-{len(calls)}"
        token_id = (
            cand.bracket.yes_token_id if cand.side == "YES"
            else cand.bracket.no_token_id
        )
        end_date = (cand.market.get("endDate") or "")[:10]
        trade_id = db.insert_trade(
            ts=ts,
            station=cand.station,
            ticker=f"{cand.station}-order-{order_id[:8]}",
            bracket_low=cand.bracket.low_f,
            bracket_high=cand.bracket.high_f,
            side=cand.side,
            predicted_price=86,
            actual_price=cand.price_cents,
            predicted_edge=cand.edge_cents,
            mode="live",
            order_id=order_id,
            capital_before=5.0,
            end_date=end_date or None,
        )
        db.open_position(
            trade_id=trade_id,
            station=cand.station,
            ticker=f"{cand.station}-order-{order_id[:8]}",
            token_id=token_id,
            side=cand.side,
            order_id=order_id,
            entry_price=cand.price_cents,
            shares=7.14,
            entry_ts=ts,
        )
        db.update_trade_by_order(order_id, ticker=cand.bracket.ticker, outcome="filled")

    return _fake_execute


def _poll_ctx(candidates, live_trader, fake_execute):
    """Context-manager patches to run poll_once in live mode without real I/O."""
    return (
        patch("src.scripts.run._build_weather", return_value={"KATL": MagicMock()}),
        patch("src.scripts.run.build_weather_low_for_scanning", return_value={}),
        patch("src.scripts.run.build_weather_for_pricing", return_value={}),
        patch("src.scripts.run.get_weather_markets", return_value=[]),
        patch("src.scripts.run.scan_markets", return_value=(candidates, [])),
        patch("src.scripts.run.fetch_orderbooks_batch", return_value={}),
        patch("src.scripts.run._load_open_no_positions", return_value=[]),
        patch("src.scripts.run._execute_live", side_effect=fake_execute),
        patch("src.scripts.run._maybe_run_emos_shadow"),
        patch.object(run_module.order_manager, "reconcile_timeout_fills"),
        patch.object(run_module.order_manager, "sync_open_orders"),
        patch.object(run_module.order_manager, "check_take_profit_exits"),
        patch("src.scripts.run._log_open_position_snapshots", return_value=[]),
        patch("src.scripts.run._check_forced_exits"),
        patch("src.scripts.run._check_stop_loss_exits"),
        patch("src.scripts.run.FreshnessMonitor"),
        patch("src.scripts.run.get_source_priority", return_value=[]),
        patch("src.scripts.run._append_candidate"),
        patch("src.scripts.run._append_snapshot"),
        patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
    )


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "entry-guard-test.db")


@pytest.fixture(autouse=True)
def _reset_run_state():
    run_module._balance_fail_count = 0
    run_module._wallet_cooldown_until = 0.0
    yield
    run_module._balance_fail_count = 0
    run_module._wallet_cooldown_until = 0.0


def _run_poll(db, candidates, calls, live_trader=None):
    live_trader = live_trader or _make_trader()
    fake_execute = _fake_execute_factory(db, calls)
    with contextlib.ExitStack() as stack:
        for p in _poll_ctx(candidates, live_trader, fake_execute):
            stack.enter_context(p)
        poll_once(_make_risk(), live_trader=live_trader, alert_manager=None, db=db)


class TestConsecutivePolls:
    def test_second_poll_same_bracket_is_blocked(self, db, caplog):
        """Two consecutive polls flagging the same bracket -> exactly one live order."""
        calls: list = []
        with caplog.at_level(logging.WARNING):
            _run_poll(db, [_make_candidate()], calls)
            assert len(calls) == 1, "first poll must place the order"
            _run_poll(db, [_make_candidate()], calls)

        assert len(calls) == 1, (
            "second poll flagged the same bracket and must be blocked by the "
            "entry guard -- this is the 7x KATL stacking regression"
        )
        assert any("[entry-guard]" in r.message for r in caplog.records), (
            "guard-fired events must be visible in logs"
        )

    def test_timeout_attempt_also_blocks_reentry(self, db, caplog):
        """A prior timeout attempt (not just a fill) blocks re-entry: repeated
        timeout retries were part of the observed stacking (5x WMKK)."""
        cand = _make_candidate()
        trade_id = db.insert_trade(
            ts=datetime.now(timezone.utc).isoformat(),
            station=cand.station,
            ticker=cand.bracket.ticker,
            bracket_low=98.0,
            bracket_high=99.0,
            side="NO",
            predicted_price=86,
            actual_price=70,
            predicted_edge=16.0,
            mode="live",
            order_id="timeout-order-1",
            outcome="timeout",
            capital_before=5.0,
            end_date=_today(),
        )
        assert trade_id > 0
        # No open_positions row: the GTC was cancelled after timeout.

        calls: list = []
        with caplog.at_level(logging.WARNING):
            _run_poll(db, [cand], calls)

        assert calls == [], "a timeout attempt today must block re-entry"
        assert any("[entry-guard]" in r.message for r in caplog.records)


class TestSamePollDedup:
    def test_duplicate_candidates_in_one_poll_place_one_order(self, db, caplog):
        calls: list = []
        with caplog.at_level(logging.WARNING):
            _run_poll(db, [_make_candidate(), _make_candidate()], calls)

        assert len(calls) == 1, "same-poll duplicate candidates must collapse to one order"
        assert any(
            "[entry-guard]" in r.message and "same poll" in r.message
            for r in caplog.records
        )


class TestNoOverBlocking:
    def test_different_brackets_and_stations_pass(self, db, caplog):
        """The guard must not block distinct (station, ticker, side) keys."""
        cand_a = _make_candidate()
        cand_b = _make_candidate(
            ticker="0xkatl-99-100", no_token="token-katl-no-2",
            yes_token="token-katl-yes-2",
        )
        cand_c = _make_candidate(
            station="WMKK", ticker="0xwmkk-91-93",
            no_token="token-wmkk-no-1", yes_token="token-wmkk-yes-1",
        )
        calls: list = []
        with caplog.at_level(logging.WARNING):
            _run_poll(db, [cand_a, cand_b, cand_c], calls)

        assert len(calls) == 3, "distinct brackets/stations must all pass the guard"
        assert not any("[entry-guard]" in r.message for r in caplog.records)


class TestReentryConfigFlag:
    def _seed_sold_trade(self, db, cand):
        """A live trade for the candidate's key that exited earlier today."""
        db.insert_trade(
            ts=datetime.now(timezone.utc).isoformat(),
            station=cand.station,
            ticker=cand.bracket.ticker,
            bracket_low=98.0,
            bracket_high=99.0,
            side=cand.side,
            predicted_price=86,
            actual_price=70,
            predicted_edge=16.0,
            mode="live",
            order_id="sold-order-1",
            outcome="sold",
            capital_before=5.0,
            end_date=_today(),
        )

    def test_default_blocks_reentry_after_exit(self, db, caplog):
        """LIVE_ALLOW_BRACKET_REENTRY=false (default): a sold exit still blocks."""
        cand = _make_candidate()
        self._seed_sold_trade(db, cand)

        calls: list = []
        with caplog.at_level(logging.WARNING):
            _run_poll(db, [cand], calls)

        assert calls == [], "default config must block re-entry even after an exit"
        assert any("[entry-guard]" in r.message for r in caplog.records)

    def test_reentry_flag_allows_after_sold_exit(self, db):
        """With the flag on, a bracket exited earlier today may be re-entered."""
        cand = _make_candidate()
        self._seed_sold_trade(db, cand)

        calls: list = []
        with patch("src.scripts.run.LIVE_ALLOW_BRACKET_REENTRY", True):
            _run_poll(db, [cand], calls)

        assert len(calls) == 1, (
            "LIVE_ALLOW_BRACKET_REENTRY=true must allow re-entry after a sold exit"
        )

    def test_reentry_flag_still_blocks_open_position(self, db, caplog):
        """Even with re-entry enabled, never stack on an OPEN position."""
        cand = _make_candidate()
        trade_id = db.insert_trade(
            ts=datetime.now(timezone.utc).isoformat(),
            station=cand.station,
            ticker=cand.bracket.ticker,
            bracket_low=98.0,
            bracket_high=99.0,
            side=cand.side,
            predicted_price=86,
            actual_price=70,
            predicted_edge=16.0,
            mode="live",
            order_id="open-order-1",
            outcome="filled",
            capital_before=5.0,
            end_date=_today(),
        )
        db.open_position(
            trade_id=trade_id,
            station=cand.station,
            ticker=cand.bracket.ticker,
            token_id=cand.bracket.no_token_id,
            side=cand.side,
            order_id="open-order-1",
            entry_price=70,
            shares=7.14,
            entry_ts=datetime.now(timezone.utc).isoformat(),
        )

        calls: list = []
        with patch("src.scripts.run.LIVE_ALLOW_BRACKET_REENTRY", True), \
                caplog.at_level(logging.WARNING):
            _run_poll(db, [cand], calls)

        assert calls == [], "an open position must always block, even with re-entry enabled"
        assert any(
            "[entry-guard]" in r.message and "open position" in r.message
            for r in caplog.records
        )


class TestGuardCounterSurface:
    def test_block_writes_guardrail_event(self, db):
        """A guard block is countable via the existing guardrail_events surface."""
        calls: list = []
        _run_poll(db, [_make_candidate()], calls)
        _run_poll(db, [_make_candidate()], calls)

        stats = db.get_guardrail_stats()
        assert stats["entry_guard_blocks"]["total"] == 1
        assert stats["entry_guard_blocks"]["last_7d"] == 1


class TestPaperModeUntouched:
    def test_paper_mode_candidates_bypass_guard(self, db):
        """Paper mode places no orders; the live gate must not interfere with
        paper accounting (risk open/close called once per candidate per poll)."""
        risk = _make_risk()
        fake_execute = MagicMock()
        for _ in range(2):
            with contextlib.ExitStack() as stack:
                for p in _poll_ctx([_make_candidate()], None, fake_execute):
                    stack.enter_context(p)
                poll_once(risk, live_trader=None, alert_manager=None, db=db)

        assert risk.open_position.call_count == 2, (
            "paper mode must keep acting on the candidate every poll -- the "
            "entry guard is live-mode only"
        )
        fake_execute.assert_not_called()
