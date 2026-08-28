"""Unit + integration tests for issue #756 -- per-bracket gate verdict capture
and the scan_decisions table.

Covers:
- Scanner: each of the 11 gate_verdict branches produces the expected value
  (src.strategy.scanner.scan_markets), with no change to which candidates are
  produced (candidates output is asserted unchanged alongside the verdict).
- DB: scan_decisions upsert is idempotent per (station, ticker, date) --
  a second poll's write replaces the prior row rather than appending.
- run.py: the verdict seam -- the scanner's "traded_live" placeholder is
  upgraded to entry_guard / timeout_today / (confirmed) traded_live once the
  entry-guard check and any live execution attempt have resolved -- and N
  evaluated brackets produce N scan_decisions rows without touching the
  existing candidates/snapshots output.
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import src.scripts.run as run_module
from src.data.db import Database
from src.model.envelope import Bracket, WeatherState
from src.model.residual_correction import ResidualStats
from src.scripts.run import poll_once
from src.strategy.scanner import GATE_VERDICTS, scan_markets

# ---------------------------------------------------------------------------
# Scanner-level helpers
# ---------------------------------------------------------------------------


def _market(group_title: str, condition_id: str, end_dt, prices: str,
            question: str = "Will the highest temperature in Miami be X?") -> dict:
    return {
        "conditionId": condition_id,
        "question": question,
        "groupItemTitle": group_title,
        "outcomes": '["Yes","No"]',
        "outcomePrices": prices,
        "clobTokenIds": '["tok_yes","tok_no"]',
        "endDate": end_dt.isoformat(),
    }


def _kmia_state(sunset_offset=True) -> WeatherState:
    """forecast=80, current=75 -- optionally pinned 30min past sunset so the
    envelope's remaining-climb term is zero regardless of wall-clock time
    (same trick as TestEntryGates in test_scanner.py -- keeps these tests
    deterministic instead of time-of-day flaky)."""
    now = datetime.now(timezone.utc)
    base = WeatherState(
        station="KMIA", now_local=now, sunset_local=now.replace(hour=20),
        current_high_f=75.0, current_high_time=now, latest_temp_f=72.0,
        latest_temp_time=now, forecast_high_f=80.0,
    )
    if sunset_offset:
        return dataclasses.replace(base, now_local=base.sunset_local + timedelta(minutes=30))
    return base


def _today_end() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=23, minute=59, second=59, microsecond=0)


# ---------------------------------------------------------------------------
# Wall-clock freezing (issue #786) -- a handful of tests below build a market
# settlement time relative to "now" (either the tail end of today, or "now +
# N hours"). Near the UTC day boundary that stops being safe: `scan_markets`
# reads its own `datetime.now(timezone.utc)` twice -- once for the same-day
# filter, once for minutes-to-settlement -- so a test built against the real
# wall clock at collection/run time can drift onto the wrong side of
# midnight, or shrink the remaining-time term enough to flip which gate
# fires first. `_FrozenDatetime` pins scanner's `datetime.now()` to a fixed
# mid-day UTC instant so those two reads -- and the market end time the test
# derives from the same instant -- always agree, independent of the hour CI
# actually runs in. Same discipline as `_kmia_state()`'s sunset pinning above.
# ---------------------------------------------------------------------------


class _FrozenDatetime(datetime):
    _fixed: "datetime | None" = None

    @classmethod
    def now(cls, tz=None):
        assert cls._fixed is not None, "set _FrozenDatetime._fixed before use"
        return cls._fixed.astimezone(tz) if tz else cls._fixed


@contextlib.contextmanager
def _frozen_scanner_now(fixed: datetime):
    """Freeze `src.strategy.scanner`'s `datetime.now(timezone.utc)` reads to
    `fixed` for the duration of the context. `fixed` should be a mid-day UTC
    instant, well clear of the day boundary."""
    _FrozenDatetime._fixed = fixed
    try:
        with patch("src.strategy.scanner.datetime", _FrozenDatetime):
            yield
    finally:
        _FrozenDatetime._fixed = None


_FROZEN_NOW = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)


class TestGateVerdicts:
    """One test per canonical gate_verdict -- each also asserts the existing
    candidates output (side/shadow) is unaffected, per the issue's AC that
    this is surfacing only."""

    def test_below_min_edge_both_sides_thin(self):
        """Bracket far below the forecast with an expensive (85c) NO ask --
        p_yes clamps to 0.05 so ev_no lands just under MIN_EDGE_CENTS (14c <
        15c), and ev_yes is deeply negative -- both edges genuinely too thin,
        independent of the price gate (no_ask=85 already clears MIN_PRICE_CENTS)."""
        weather = {"KMIA": _kmia_state()}
        market = _market("40-45°F", "0xthin", _today_end(), '["0.15","0.85"]')
        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            candidates, snapshots = scan_markets(weather, [market])
        assert candidates == []
        assert snapshots[0]["gate_verdict"] == "below_min_edge"
        assert snapshots[0]["side"] is None
        assert snapshots[0]["gate_unit"] == "cents"
        assert snapshots[0]["gate_actual"] < snapshots[0]["gate_threshold"]

    def test_above_max_edge_yes(self):
        weather = {"KMIA": _kmia_state()}
        market = _market("65-95°F", "0xmaxyes", _today_end(), '["0.75","0.25"]')
        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            candidates, snapshots = scan_markets(weather, [market])
        assert candidates == []  # rejected before a Candidate is built
        snap = snapshots[0]
        assert snap["gate_verdict"] == "above_max_edge"
        assert snap["side"] is None
        assert snap["gate_threshold"] == pytest.approx(20.0)
        assert snap["gate_actual"] > snap["gate_threshold"]

    def test_above_max_edge_no(self):
        weather = {"KMIA": _kmia_state()}
        market = _market("40-45°F", "0xmaxno", _today_end(), '["0.30","0.70"]')
        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            candidates, snapshots = scan_markets(weather, [market])
        assert candidates == []
        snap = snapshots[0]
        assert snap["gate_verdict"] == "above_max_edge"
        assert snap["gate_actual"] > snap["gate_threshold"] == pytest.approx(20.0)

    def test_below_min_price_no_side(self):
        weather = {"KMIA": _kmia_state()}
        market = _market("40-45°F", "0xprice", _today_end(), '["0.50","0.50"]')
        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            candidates, snapshots = scan_markets(weather, [market])
        assert candidates == []
        snap = snapshots[0]
        assert snap["gate_verdict"] == "below_min_price"
        assert snap["gate_actual"] == 50
        assert snap["gate_threshold"] == 70

    def test_below_min_confidence_no_side(self):
        """Settlement is pinned ~12h out from a frozen mid-day `now` (issue
        #786) -- at the real day's tail end this same market (built via
        `_today_end()`) can shrink to minutes-to-settlement, sharpening the
        distribution until `above_max_edge` fires before `below_min_confidence`
        is ever reached."""
        weather = {"KMIA": _kmia_state()}
        end = _FROZEN_NOW.replace(hour=23, minute=59, second=59, microsecond=0)
        # "75-76°F". #917 widened dash-range brackets to their true inclusive
        # upper bound, which moved this fixture twice: "78-79°F" now covers
        # [78, 80) and sits close enough to forecast_high_f=80 to trip
        # below_min_edge, and the "74-75°F" chosen to replace it went too far
        # the other way -- p fell to 0.0466, so the NO edge reached ~23.5c and
        # above_max_edge (MAX_EDGE_CENTS=20) fired before the confidence gate
        # was ever consulted. That left master red from #917's merge on
        # 2026-07-31 through six subsequent merges.
        #
        # The confidence gate is only reachable in the band where the NO edge
        # lands between MIN_EDGE (15c) and MAX_EDGE (20c) while p is still
        # under MAX_CONFIDENCE_YES_FOR_NO (0.05 -- note the gate reports the
        # unclamped 0.0923 against it). [75, 77) sits in that band; the two
        # neighbouring brackets do not.
        market = _market("75-76°F", "0xconf", end, '["0.30","0.70"]')
        with _frozen_scanner_now(_FROZEN_NOW):
            candidates, snapshots = scan_markets(weather, [market])
        assert candidates == []
        snap = snapshots[0]
        assert snap["gate_verdict"] == "below_min_confidence"
        assert snap["gate_unit"] == "probability"
        assert snap["gate_actual"] > snap["gate_threshold"] == pytest.approx(0.05)

    def test_margin_gate_blocks_thin_no_entry(self):
        weather = {"KMIA": _kmia_state()}
        market = _market("90-95°F", "0xmargin", _today_end(), '["0.20","0.80"]')
        with patch("src.strategy.scanner.MIN_FORECAST_BRACKET_MARGIN_F", 15.0), \
                patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            candidates, snapshots = scan_markets(weather, [market])
        assert candidates == []
        snap = snapshots[0]
        assert snap["gate_verdict"] == "margin_gate"
        assert snap["gate_unit"] == "degrees_f"
        assert snap["gate_threshold"] == pytest.approx(15.0)

    def test_mae_gate_forces_no_to_shadow(self):
        """A NO candidate that would otherwise trade live is forced to shadow
        by the MAE gate -- verdict reflects that specific reason, not the
        generic 'shadow_only'.

        `now`/`end_time` are pinned to a frozen mid-day `now` (issue #786)
        rather than the real wall clock -- `now + timedelta(hours=3)` run
        late in the UTC day rolls onto the next calendar date, which
        `scan_markets` then classifies as a next-day (not same-day) market
        and filters out of the candidate set entirely."""
        now = _FROZEN_NOW
        end_time = now + timedelta(hours=3)
        state = WeatherState(
            station="RKPK", now_local=now, sunset_local=now,
            current_high_f=70.0, current_high_time=now, latest_temp_f=68.0,
            latest_temp_time=now, forecast_high_f=72.0, deb_mu_f=72.0,
        )
        market = _market(
            "85-90°F", "0xmae", end_time, '["0.22","0.78"]',
            question="Will the highest temperature in Busan be 85-90°F?",
        )
        mock_db = MagicMock()
        mock_db.get_station_override.return_value = None
        mock_db.get_all_config.return_value = {}
        suppressed = ResidualStats(
            city="Busan", mean_signed_error=4.6, rolling_mae=10.1,
            sample_count=90, correction_applied=True, live_suppressed=True,
        )
        with patch("src.strategy.scanner.compute_residual_stats", return_value=suppressed), \
                _frozen_scanner_now(_FROZEN_NOW):
            candidates, snapshots = scan_markets({"RKPK": state}, [market], db=mock_db)

        assert len(candidates) == 1
        assert candidates[0].side == "NO"
        assert candidates[0].shadow is True
        snap = snapshots[0]
        assert snap["gate_verdict"] == "mae_gate"
        assert snap["side"] == "NO"
        assert snap["gate_actual"] == pytest.approx(10.1)
        assert snap["gate_threshold"] == pytest.approx(8.0)  # MAX_RESIDUAL_MAE_F_FOR_LIVE default
        assert snap["gate_unit"] == "degrees_f"

    def test_mae_gate_does_not_fire_below_threshold(self):
        """Same setup, MAE within threshold: verdict is the live placeholder,
        not mae_gate -- confirms the gate is genuinely conditional.

        `now`/`end_time` frozen for the same reason as
        `test_mae_gate_forces_no_to_shadow` above (issue #786)."""
        now = _FROZEN_NOW
        end_time = now + timedelta(hours=3)
        state = WeatherState(
            station="RKPK", now_local=now, sunset_local=now,
            current_high_f=70.0, current_high_time=now, latest_temp_f=68.0,
            latest_temp_time=now, forecast_high_f=72.0, deb_mu_f=72.0,
        )
        market = _market(
            "85-90°F", "0xmae2", end_time, '["0.22","0.78"]',
            question="Will the highest temperature in Busan be 85-90°F?",
        )
        mock_db = MagicMock()
        mock_db.get_station_override.return_value = None
        mock_db.get_all_config.return_value = {}
        safe = ResidualStats(
            city="Busan", mean_signed_error=1.0, rolling_mae=3.0,
            sample_count=90, correction_applied=True, live_suppressed=False,
        )
        with patch("src.strategy.scanner.compute_residual_stats", return_value=safe), \
                _frozen_scanner_now(_FROZEN_NOW):
            candidates, snapshots = scan_markets({"RKPK": state}, [market], db=mock_db)

        assert len(candidates) == 1
        assert candidates[0].shadow is False
        assert snapshots[0]["gate_verdict"] == "traded_live"

    def test_shadow_only(self):
        weather = {"KMIA": _kmia_state()}
        market = _market("90-95°F", "0xshadow", _today_end(), '["0.20","0.80"]')
        with patch("src.strategy.scanner.DISABLED_STATIONS", {"KMIA"}), \
                patch("src.strategy.scanner.SHADOW_STATIONS", {"KMIA"}), \
                patch("src.strategy.scanner.SHADOW_STATIONS_YES", set()), \
                patch("src.strategy.scanner.SHADOW_STATIONS_NO", set()), \
                patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            candidates, snapshots = scan_markets(weather, [market])
        assert len(candidates) == 1
        assert candidates[0].shadow is True
        assert snapshots[0]["gate_verdict"] == "shadow_only"
        assert snapshots[0]["side"] == candidates[0].side

    def test_next_day_shadow(self, monkeypatch):
        monkeypatch.setenv("NEXT_DAY_EVALUATION", "true")
        weather = {"KMIA": _kmia_state(sunset_offset=False)}
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=6)
        market = _market("76-88°F", "0xnextday", tomorrow, '["0.75","0.25"]')
        with patch("src.strategy.scanner._fetch_next_day_forecast", return_value=(82.0, 3.0)):
            candidates, snapshots = scan_markets(weather, [market])
        assert len(candidates) == 1
        assert candidates[0].is_next_day is True
        assert candidates[0].shadow is True
        snap = snapshots[0]
        assert snap["gate_verdict"] == "next_day_shadow"
        assert snap["is_next_day"] == 1
        assert snap["side"] == candidates[0].side

    def test_traded_live_placeholder(self):
        """A live (non-shadow) candidate that clears every gate gets the
        scanner's pre-execution placeholder -- run.py resolves the final
        verdict (see TestRunVerdictSeam below)."""
        weather = {"KMIA": _kmia_state()}
        market = _market("65-95°F", "0xlive", _today_end(), '["0.80","0.20"]')
        with patch("src.strategy.scanner.MIN_MINUTES_TO_SETTLEMENT", 0), \
                patch("src.strategy.scanner.MODEL_PROB_CAP", 1.0):
            candidates, snapshots = scan_markets(weather, [market])
        assert len(candidates) == 1
        assert candidates[0].side == "YES"
        assert candidates[0].shadow is False
        snap = snapshots[0]
        assert snap["gate_verdict"] == "traded_live"
        assert snap["side"] == "YES"

    def test_every_verdict_is_in_the_locked_enum(self):
        """Sanity guard: GATE_VERDICTS is exactly the 12 names locked with the
        design spec -- catches an accidental typo/rename in either place."""
        assert GATE_VERDICTS == {
            "traded_live", "shadow_only", "next_day_shadow", "entry_guard",
            "timeout_today", "below_min_edge", "above_max_edge",
            "below_min_price", "below_min_confidence", "margin_gate", "mae_gate",
            "day_mismatch_shadow",
        }


# ---------------------------------------------------------------------------
# DB-level: upsert idempotency
# ---------------------------------------------------------------------------


class TestScanDecisionsUpsert:
    @pytest.fixture()
    def db(self, tmp_path):
        return Database(tmp_path / "scan-decisions-test.db")

    def test_second_poll_replaces_prior_row(self, db):
        db.upsert_scan_decision(
            ts="2026-07-21T10:00:00Z", station="KMIA", ticker="0xabc",
            date="2026-07-21", bracket_low=80.0, bracket_high=85.0,
            gate_verdict="below_min_edge", gate_actual=4.0, gate_threshold=15.0,
            gate_unit="cents",
        )
        db.upsert_scan_decision(
            ts="2026-07-21T10:05:00Z", station="KMIA", ticker="0xabc",
            date="2026-07-21", bracket_low=80.0, bracket_high=85.0,
            gate_verdict="traded_live", side="YES",
        )
        rows = db.get_scan_decisions("KMIA", "2026-07-21")
        assert len(rows) == 1
        assert rows[0]["gate_verdict"] == "traded_live"
        assert rows[0]["ts"] == "2026-07-21T10:05:00Z"

    def test_different_ticker_same_day_is_a_separate_row(self, db):
        db.upsert_scan_decision(
            ts="2026-07-21T10:00:00Z", station="KMIA", ticker="0xabc",
            date="2026-07-21", bracket_low=80.0, bracket_high=85.0,
            gate_verdict="below_min_edge",
        )
        db.upsert_scan_decision(
            ts="2026-07-21T10:00:00Z", station="KMIA", ticker="0xdef",
            date="2026-07-21", bracket_low=85.0, bracket_high=90.0,
            gate_verdict="above_max_edge",
        )
        rows = db.get_scan_decisions("KMIA", "2026-07-21")
        assert len(rows) == 2

    def test_invalid_gate_verdict_rejected(self, db):
        with pytest.raises(ValueError):
            db.upsert_scan_decision(
                ts="x", station="KMIA", ticker="0xabc", date="2026-07-21",
                bracket_low=80.0, bracket_high=85.0, gate_verdict="not_a_real_verdict",
            )

    def test_execution_mode_defaults_to_paper(self, db):
        """Issue #780: a caller that never passes execution_mode gets the
        conservative default -- never claim an unspecified row as a
        confirmed live fill."""
        db.upsert_scan_decision(
            ts="2026-07-21T10:00:00Z", station="KMIA", ticker="0xabc",
            date="2026-07-21", bracket_low=80.0, bracket_high=85.0,
            gate_verdict="traded_live", side="YES",
        )
        rows = db.get_scan_decisions("KMIA", "2026-07-21")
        assert rows[0]["execution_mode"] == "paper"

    def test_execution_mode_live_round_trips(self, db):
        db.upsert_scan_decision(
            ts="2026-07-21T10:00:00Z", station="KMIA", ticker="0xabc",
            date="2026-07-21", bracket_low=80.0, bracket_high=85.0,
            gate_verdict="traded_live", side="YES", execution_mode="live",
        )
        rows = db.get_scan_decisions("KMIA", "2026-07-21")
        assert rows[0]["execution_mode"] == "live"

    def test_invalid_execution_mode_rejected(self, db):
        with pytest.raises(ValueError):
            db.upsert_scan_decision(
                ts="x", station="KMIA", ticker="0xabc", date="2026-07-21",
                bracket_low=80.0, bracket_high=85.0, gate_verdict="traded_live",
                execution_mode="not_a_real_mode",
            )

    def test_direction_defaults_to_high(self, db):
        """Issue #887: a caller that never passes direction gets the
        legacy default -- every scan_decisions row predates the low-side
        scanner and was genuinely high-side."""
        db.upsert_scan_decision(
            ts="2026-07-21T10:00:00Z", station="KMIA", ticker="0xabc",
            date="2026-07-21", bracket_low=80.0, bracket_high=85.0,
            gate_verdict="below_min_edge",
        )
        rows = db.get_scan_decisions("KMIA", "2026-07-21")
        assert rows[0]["direction"] == "high"

    def test_direction_low_round_trips(self, db):
        db.upsert_scan_decision(
            ts="2026-07-21T10:00:00Z", station="KMIA", ticker="0xabc",
            date="2026-07-21", bracket_low=80.0, bracket_high=85.0,
            gate_verdict="shadow_only", direction="low",
        )
        rows = db.get_scan_decisions("KMIA", "2026-07-21")
        assert rows[0]["direction"] == "low"

    def test_price_raw_defaults_to_none(self, db):
        """Issue #1076: a caller that never passes yes_price_raw/no_price_raw
        (e.g. every pre-#1076 row) round-trips NULL -- there is nothing to
        backfill since the clamp already discarded that information."""
        db.upsert_scan_decision(
            ts="2026-07-21T10:00:00Z", station="KMIA", ticker="0xabc",
            date="2026-07-21", bracket_low=80.0, bracket_high=85.0,
            gate_verdict="below_min_edge",
        )
        rows = db.get_scan_decisions("KMIA", "2026-07-21")
        assert rows[0]["yes_price_raw"] is None
        assert rows[0]["no_price_raw"] is None

    def test_price_raw_round_trips_with_full_float_precision(self, db):
        """Issue #1076: sub-penny prices, which yes_ask/no_ask's clamp would
        destroy, survive a full DB round-trip in yes_price_raw/no_price_raw."""
        db.upsert_scan_decision(
            ts="2026-07-21T10:00:00Z", station="KMIA", ticker="0xabc",
            date="2026-07-21", bracket_low=80.0, bracket_high=85.0,
            gate_verdict="below_min_price", yes_ask=1, no_ask=99,
            yes_price_raw=0.003, no_price_raw=0.997,
        )
        rows = db.get_scan_decisions("KMIA", "2026-07-21")
        assert rows[0]["yes_ask"] == 1
        assert rows[0]["no_ask"] == 99
        assert rows[0]["yes_price_raw"] == pytest.approx(0.003)
        assert rows[0]["no_price_raw"] == pytest.approx(0.997)


# ---------------------------------------------------------------------------
# run.py: the verdict seam + N-brackets-N-rows / candidates unaffected
# ---------------------------------------------------------------------------


def _snap(ticker: str, station: str = "KATL", gate_verdict: str = "traded_live",
          side: "str | None" = None, bracket_low=98.0, bracket_high=99.0,
          date: "str | None" = None, direction: str = "high") -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    return {
        "ts": ts, "station": station, "ticker": ticker,
        "bracket_low": bracket_low, "bracket_high": bracket_high,
        "yes_ask": 30, "no_ask": 70, "current_high": 70.0, "latest_temp": 68.0,
        "forecast_high": 72.0, "p_yes": 0.14, "raw_p_yes": 0.14, "capped_p_yes": 0.14,
        "ev_yes": -10.0, "ev_no": 16.0, "ev_yes_raw": -10.0, "ev_no_raw": 16.0,
        "minutes_to_settlement": 300.0, "emos_mode": "legacy", "is_next_day": 0,
        "date": date or datetime.now(timezone.utc).date().isoformat(), "poll_ts": ts,
        "side": side, "gate_verdict": gate_verdict, "gate_actual": None,
        "gate_threshold": None, "gate_unit": None, "gate_detail": None,
        "direction": direction,
        "ensemble_mean": None, "ensemble_members": None,
        "ensemble_range_low": None, "ensemble_range_high": None,
    }


def _make_candidate(ticker="0xkatl-98-99", side="NO", station="KATL") -> "object":
    from src.strategy.scanner import Candidate

    bracket = Bracket(
        ticker=ticker, low_f=98.0, high_f=99.0,
        yes_ask_cents=30, yes_ask_size=100, no_ask_cents=70, no_ask_size=100,
        yes_token_id=f"tok-yes-{ticker}", no_token_id=f"tok-no-{ticker}",
    )
    today = datetime.now(timezone.utc).date().isoformat()
    return Candidate(
        station=station, bracket=bracket, side=side, edge_cents=16.0,
        price_cents=70, confidence=0.86, p_yes=0.14, ev_yes=-10.0, ev_no=16.0,
        minutes_to_settlement=300.0,
        market={"question": f"{station} high temp", "endDate": f"{today}T23:59:00Z"},
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


def _poll_ctx(candidates, snapshots, live_trader, execute_side_effect):
    return (
        patch("src.scripts.run._build_weather", return_value={"KATL": MagicMock()}),
        patch("src.scripts.run.build_weather_low_for_scanning", return_value={}),
        patch("src.scripts.run.build_weather_for_pricing", return_value={}),
        patch("src.scripts.run.get_weather_markets", return_value=[]),
        patch("src.scripts.run.scan_markets", return_value=(candidates, snapshots)),
        patch("src.scripts.run.fetch_orderbooks_batch", return_value={}),
        patch("src.scripts.run._load_open_no_positions", return_value=[]),
        patch("src.scripts.run._execute_live", side_effect=execute_side_effect),
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
    return Database(tmp_path / "run-wiring-scan-decisions.db")


@pytest.fixture(autouse=True)
def _reset_run_state():
    run_module._balance_fail_count = 0
    run_module._wallet_cooldown_until = 0.0
    yield
    run_module._balance_fail_count = 0
    run_module._wallet_cooldown_until = 0.0


_UNSET = object()


def _run_poll(db, candidates, snapshots, execute_side_effect=None, live_trader=_UNSET):
    """live_trader defaults to a mock live trader; pass live_trader=None
    explicitly to exercise the paper-mode path (no entry-guard/execution seam)."""
    if live_trader is _UNSET:
        live_trader = _make_trader()
    with contextlib.ExitStack() as stack:
        for p in _poll_ctx(candidates, snapshots, live_trader, execute_side_effect):
            stack.enter_context(p)
        poll_once(_make_risk(), live_trader=live_trader, alert_manager=None, db=db)
    return live_trader


class TestRunVerdictSeam:
    """The scanner's 'traded_live' placeholder is upgraded to the real
    per-bracket outcome once run.py's entry-guard/execution seam resolves it
    (issue #756's 'verdict seam')."""

    def test_confirmed_fill_stays_traded_live(self, db, caplog):
        cand = _make_candidate()
        snap = _snap(cand.bracket.ticker, side="NO", gate_verdict="traded_live")

        with caplog.at_level(logging.WARNING):
            _run_poll(db, [cand], [snap], execute_side_effect=lambda *a, **kw: "filled")

        rows = db.get_scan_decisions("KATL", snap["date"])
        assert len(rows) == 1
        assert rows[0]["gate_verdict"] == "traded_live"
        assert rows[0]["gate_detail"] is None
        assert rows[0]["execution_mode"] == "live"  # issue #780: confirmed fill

    def test_execution_timeout_downgrades_to_timeout_today(self, db):
        cand = _make_candidate()
        snap = _snap(cand.bracket.ticker, side="NO", gate_verdict="traded_live")

        _run_poll(db, [cand], [snap], execute_side_effect=lambda *a, **kw: "timeout")

        rows = db.get_scan_decisions("KATL", snap["date"])
        assert len(rows) == 1
        assert rows[0]["gate_verdict"] == "timeout_today"
        assert "timeout" in rows[0]["gate_detail"]

    def test_entry_guard_block_downgrades_to_entry_guard(self, db):
        """A duplicate open position for the token blocks the candidate
        before it ever reaches execution -- verdict reflects that, with the
        real guard reason carried through verbatim (design spec's tooltip
        contract: gate_detail is the backend's reason string, not
        reconstructed by the frontend)."""
        cand = _make_candidate()
        snap = _snap(cand.bracket.ticker, side="NO", gate_verdict="traded_live")

        # Seed an existing open position for the same token so the entry
        # guard fires (mirrors test_entry_guard.py's "open position" case).
        trade_id = db.insert_trade(
            ts=datetime.now(timezone.utc).isoformat(), station=cand.station,
            ticker=cand.bracket.ticker, bracket_low=98.0, bracket_high=99.0,
            side="NO", predicted_price=86, actual_price=70, predicted_edge=16.0,
            mode="live", order_id="existing-order", capital_before=5.0,
        )
        db.open_position(
            trade_id=trade_id, station=cand.station, ticker=cand.bracket.ticker,
            token_id=cand.bracket.no_token_id, side="NO", order_id="existing-order",
            entry_price=70, shares=7.14, entry_ts=datetime.now(timezone.utc).isoformat(),
        )

        _run_poll(db, [cand], [snap], execute_side_effect=lambda *a, **kw: "filled")

        rows = db.get_scan_decisions("KATL", snap["date"])
        assert len(rows) == 1
        assert rows[0]["gate_verdict"] == "entry_guard"
        assert "open position" in rows[0]["gate_detail"]
        assert rows[0]["execution_mode"] == "live"  # issue #780: poll had a live trader

    def test_placeholder_never_resolved_downgrades_to_entry_guard(self, db):
        """Wallet-empty cooldown short-circuits the poll before the candidate
        loop ever runs -- the scanner's optimistic placeholder must not be
        left claiming a live trade that never happened."""
        run_module._wallet_cooldown_until = __import__("time").time() + 999
        cand = _make_candidate()
        snap = _snap(cand.bracket.ticker, side="NO", gate_verdict="traded_live")

        _run_poll(db, [cand], [snap], execute_side_effect=lambda *a, **kw: "filled")

        rows = db.get_scan_decisions("KATL", snap["date"])
        assert len(rows) == 1
        assert rows[0]["gate_verdict"] == "entry_guard"
        assert rows[0]["execution_mode"] == "live"  # issue #780: poll had a live trader

    def test_paper_mode_leaves_placeholder_as_traded_live(self, db):
        """No live_trader -- there is no entry-guard/execution seam at all,
        so the scanner's best-effort placeholder is persisted unchanged."""
        cand = _make_candidate()
        cand = dataclasses.replace(cand, shadow=False)
        snap = _snap(cand.bracket.ticker, side="NO", gate_verdict="traded_live")

        _run_poll(db, [cand], [snap], execute_side_effect=None, live_trader=None)

        rows = db.get_scan_decisions("KATL", snap["date"])
        assert len(rows) == 1
        assert rows[0]["gate_verdict"] == "traded_live"
        assert rows[0]["execution_mode"] == "paper"  # issue #780: unconfirmed placeholder


class TestScanDecisionsRowCount:
    def test_n_brackets_yields_n_rows_and_candidates_unaffected(self, db):
        """N evaluated brackets -> N scan_decisions rows; the candidates list
        acted on this poll is unchanged (issue #756 AC: surfacing only)."""
        snaps = [
            _snap("0xa", gate_verdict="below_min_edge"),
            _snap("0xb", gate_verdict="above_max_edge"),
            _snap("0xc", gate_verdict="shadow_only", side="YES"),
            _snap("0xd", gate_verdict="traded_live", side="NO"),
        ]
        live_cand = _make_candidate(ticker="0xd", side="NO")

        _run_poll(db, [live_cand], snaps, execute_side_effect=lambda *a, **kw: "filled")

        rows = db.get_scan_decisions("KATL", snaps[0]["date"])
        assert len(rows) == 4
        verdicts = {r["ticker"]: r["gate_verdict"] for r in rows}
        assert verdicts["0xa"] == "below_min_edge"
        assert verdicts["0xb"] == "above_max_edge"
        assert verdicts["0xc"] == "shadow_only"
        assert verdicts["0xd"] == "traded_live"  # confirmed by the execution outcome
        # issue #780: every row this poll is stamped 'live' -- a live trader
        # was configured, regardless of each individual bracket's verdict.
        assert all(r["execution_mode"] == "live" for r in rows)

    def test_second_poll_same_day_upserts_not_appends(self, db):
        """Idempotent-per-poll semantics through the full run.py path, not
        just the raw DB call -- a second poll for the same bracket replaces
        the row rather than growing the table."""
        snap = _snap("0xa", gate_verdict="below_min_edge")
        _run_poll(db, [], [snap])
        snap2 = dict(snap)
        snap2["gate_verdict"] = "above_max_edge"
        _run_poll(db, [], [snap2])

        rows = db.get_scan_decisions("KATL", snap["date"])
        assert len(rows) == 1
        assert rows[0]["gate_verdict"] == "above_max_edge"
