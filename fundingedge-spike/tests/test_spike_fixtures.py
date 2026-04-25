"""Fixture-based integration tests for spike.poll_once using injected clock.

These tests freeze time using a clock parameter passed to poll_once, eliminating
dependency on datetime.now() and allowing deterministic replay of pre-recorded
Binance API responses.
"""
import sys
import os

# Ensure the fundingedge-spike package root is on the path when running from
# the repo root (e.g. pytest fundingedge-spike/tests/...)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import csv
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

import config
import spike
import binance_client


BASE_TIME = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
FUNDING_TIME = datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc)  # 2h away from BASE_TIME


def _spot_ticker(bid="60000.0", ask="60001.0"):
    """Simulate Binance spot book ticker response."""
    return {"bidPrice": bid, "askPrice": ask}


def _perp_ticker(bid="60003.0", ask="60004.0"):
    """Simulate Binance perp book ticker response."""
    return {"bidPrice": bid, "askPrice": ask}


def _premium_index(rate="0.0005", next_funding_ms=None):
    """Simulate Binance premium index response."""
    if next_funding_ms is None:
        next_funding_ms = int(FUNDING_TIME.timestamp() * 1000)
    return {
        "lastFundingRate": rate,
        "nextFundingTime": str(next_funding_ms),
    }


def _funding_history(rate="0.0005", n=9):
    """Simulate Binance funding history response with n settlements all at rate."""
    return [{"fundingRate": rate, "fundingTime": str(i * 1000)} for i in range(n)]


@pytest.fixture
def redirect_logs(tmp_path, monkeypatch):
    """Redirect all config log paths to tmp_path to isolate test runs."""
    # Patch config module
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(config, "SIGNALS_CSV", tmp_path / "signals.csv")
    monkeypatch.setattr(config, "CYCLES_CSV", tmp_path / "cycles.csv")
    monkeypatch.setattr(config, "SNAPSHOTS_JSONL", tmp_path / "snapshots.jsonl")
    monkeypatch.setattr(config, "OPEN_HEDGES_JSON", tmp_path / "open_hedges.json")
    monkeypatch.setattr(config, "UNIVERSE", ["BTCUSDT"])

    # Also patch in spike module since it imports these at load time
    monkeypatch.setattr(spike, "LOG_DIR", tmp_path)
    monkeypatch.setattr(spike, "SIGNALS_CSV", tmp_path / "signals.csv")
    monkeypatch.setattr(spike, "CYCLES_CSV", tmp_path / "cycles.csv")
    monkeypatch.setattr(spike, "SNAPSHOTS_JSONL", tmp_path / "snapshots.jsonl")
    monkeypatch.setattr(spike, "OPEN_HEDGES_JSON", tmp_path / "open_hedges.json")
    monkeypatch.setattr(spike, "UNIVERSE", ["BTCUSDT"])

    return tmp_path


# ---------------------------------------------------------------------------
# Scenario A: Steady positive funding for 72h
# ---------------------------------------------------------------------------

def test_scenario_a_steady_positive_funding(redirect_logs, monkeypatch):
    """Steady positive funding (>3 bps) for 72h → 1 signal, 9 funding accruals, net_pnl > 0.

    Entry fires on first poll (rate=5 bps, persistence=1.0, basis=0.83 bps, 2h to funding).
    Advance time by 8h per poll for 8 more polls (simulating 3 days of accruals).
    At 72h, target hold is reached → cycle closes.
    """
    tmp_path = redirect_logs

    # --- entry poll at clock_0 ---
    clock_0 = BASE_TIME

    def setup_mocks_for_clock(clock_time):
        """Setup mocks for a specific clock time, which is used for time.time() calculations."""
        # Monkeypatch time.time to return the timestamp for the given clock_time
        mock_time = clock_time.timestamp()
        monkeypatch.setattr(time, "time", lambda: mock_time)

        # Create closures that capture clock_time to compute funding time offsets correctly
        next_ft_ms = int((FUNDING_TIME + (clock_time - BASE_TIME)).timestamp() * 1000)

        # Patch in spike module (where they are imported) not in binance_client
        monkeypatch.setattr(spike, "get_spot_book_ticker", lambda s: _spot_ticker())
        monkeypatch.setattr(spike, "get_perp_book_ticker", lambda s: _perp_ticker())
        monkeypatch.setattr(spike, "get_premium_index", lambda s: _premium_index("0.0005", next_funding_ms=next_ft_ms))
        monkeypatch.setattr(spike, "get_funding_history", lambda s, st, et, **kw: _funding_history("0.0005", 9))

    setup_mocks_for_clock(clock_0)
    hedges = spike.poll_once({}, clock=clock_0)
    assert len(hedges) == 1, "Signal should fire on first poll"

    # Verify signals.csv has 1 row
    signals_csv_path = tmp_path / "signals.csv"
    assert signals_csv_path.exists(), f"signals.csv should exist at {signals_csv_path}, files: {list(tmp_path.iterdir())}"
    signals = list(csv.DictReader(open(signals_csv_path)))
    assert len(signals) == 1, "Should have exactly 1 signal"
    assert signals[0]["symbol"] == "BTCUSDT"
    assert float(signals[0]["funding_rate_bps"]) == 5.0

    # --- advance 8h per poll for 8 more polls (simulate 8 more funding cycles) ---
    for i in range(1, 9):
        clock_i = clock_0 + timedelta(hours=8 * i)
        setup_mocks_for_clock(clock_i)
        hedges = spike.poll_once(hedges, clock=clock_i)
        assert len(hedges) == 1, f"Hedge should still be open after poll {i}"

    # --- close poll at 72h (target hold reached) ---
    clock_close = clock_0 + timedelta(hours=72)
    setup_mocks_for_clock(clock_close)
    hedges = spike.poll_once(hedges, clock=clock_close)

    # Hedge should be closed (target hold reached)
    assert len(hedges) == 0, "Hedge should be closed at 72h"

    # Verify cycles.csv
    cycles = list(csv.DictReader(open(tmp_path / "cycles.csv")))
    assert len(cycles) == 1, "Should have exactly 1 closed cycle"
    cycle = cycles[0]
    assert float(cycle["net_pnl_usd"]) > 0, "Net P&L should be positive with steady 5 bps funding"
    assert "target_hold" in cycle["reason"].lower(), f"Reason should be target_hold, got: {cycle['reason']}"
    assert float(cycle["hold_hours"]) >= 72.0, "Hold time should be at least 72h"


# ---------------------------------------------------------------------------
# Scenario B: Funding flip to negative after 2 accruals
# ---------------------------------------------------------------------------

def test_scenario_b_funding_flip_to_negative(redirect_logs, monkeypatch):
    """Funding flips negative after 2 accruals → exit fires on negative streak or rate threshold.

    Entry fires on first poll (rate=4 bps, all rules pass).
    2 polls with positive funding (4 bps), then funding flips to -0.5 bps.
    On the 2nd negative poll, either negative_streak or rate threshold triggers exit.

    Note: -0.5 bps is < 1 bps threshold, so may exit on rate rule rather than negative_streak rule.
    We verify the hedge closes and the reason explains why.
    """
    tmp_path = redirect_logs

    def setup_mocks_for_clock(clock_time, rate="0.0004"):
        """Setup mocks for a specific clock time with optional rate."""
        mock_time = clock_time.timestamp()
        monkeypatch.setattr(time, "time", lambda: mock_time)

        next_ft_ms = int((FUNDING_TIME + (clock_time - BASE_TIME)).timestamp() * 1000)
        monkeypatch.setattr(spike, "get_spot_book_ticker", lambda s: _spot_ticker())
        monkeypatch.setattr(spike, "get_perp_book_ticker", lambda s: _perp_ticker())
        monkeypatch.setattr(spike, "get_premium_index", lambda s: _premium_index(rate, next_funding_ms=next_ft_ms))
        monkeypatch.setattr(spike, "get_funding_history", lambda s, st, et, **kw: _funding_history(rate, 9))

    # --- entry poll ---
    clock_0 = BASE_TIME
    setup_mocks_for_clock(clock_0, "0.0004")
    hedges = spike.poll_once({}, clock=clock_0)
    assert len(hedges) == 1, "Signal should fire"

    signals = list(csv.DictReader(open(tmp_path / "signals.csv")))
    assert len(signals) == 1

    # --- 2 polls with positive funding (4 bps) ---
    for i in range(1, 3):
        clock_i = clock_0 + timedelta(hours=8 * i)
        setup_mocks_for_clock(clock_i, "0.0004")
        hedges = spike.poll_once(hedges, clock=clock_i)
        assert len(hedges) == 1, f"Hedge should still be open at poll {i}"

    # --- funding flips to barely positive (1.5 bps, above threshold) but about to go negative ---
    clock_3 = clock_0 + timedelta(hours=8 * 3)
    setup_mocks_for_clock(clock_3, "0.00015")
    hedges = spike.poll_once(hedges, clock=clock_3)
    assert len(hedges) == 1, "Hedge should still be open (rate=1.5 bps >= threshold)"

    # --- first negative poll → rate < threshold → should_exit fires ---
    clock_4 = clock_0 + timedelta(hours=8 * 4)
    setup_mocks_for_clock(clock_4, "-0.0005")
    hedges = spike.poll_once(hedges, clock=clock_4)
    assert len(hedges) == 0, "Hedge should be closed when rate becomes negative (< threshold)"

    # Verify cycles.csv
    cycles = list(csv.DictReader(open(tmp_path / "cycles.csv")))
    assert len(cycles) == 1, "Should have exactly 1 closed cycle"
    cycle = cycles[0]
    # The exit could be due to rate threshold or negative streak, just verify it exited
    assert cycle["reason"] != "", f"Reason should be set, got: {cycle['reason']}"


# ---------------------------------------------------------------------------
# Scenario C: Basis blow-out to > 100 bps mid-hold
# ---------------------------------------------------------------------------

def test_scenario_c_basis_blow_out(redirect_logs, monkeypatch):
    """Basis spikes to 120 bps (> EMERGENCY_BASIS_BPS=100) → exit fires on basis blow-out.

    Entry fires on first poll (rate=5 bps, basis=0.83 bps, all rules pass).
    Next poll: basis spikes to 120 bps while funding rate stays high.
    should_exit fires on "basis blow-out" rule → cycle closes.
    """
    tmp_path = redirect_logs

    # --- entry poll ---
    clock_0 = BASE_TIME

    def setup_mocks_for_clock_with_basis(clock_time, rate="0.0005", perp_bid="60003.0", perp_ask="60004.0"):
        """Setup mocks for a specific clock time with optional perp prices."""
        mock_time = clock_time.timestamp()
        monkeypatch.setattr(time, "time", lambda: mock_time)

        next_ft_ms = int((FUNDING_TIME + (clock_time - BASE_TIME)).timestamp() * 1000)
        monkeypatch.setattr(spike, "get_spot_book_ticker", lambda s: _spot_ticker())
        monkeypatch.setattr(spike, "get_perp_book_ticker", lambda s: {"bidPrice": perp_bid, "askPrice": perp_ask})
        monkeypatch.setattr(spike, "get_premium_index", lambda s: _premium_index(rate, next_funding_ms=next_ft_ms))
        monkeypatch.setattr(spike, "get_funding_history", lambda s, st, et, **kw: _funding_history(rate, 9))

    setup_mocks_for_clock_with_basis(clock_0)
    hedges = spike.poll_once({}, clock=clock_0)
    assert len(hedges) == 1, "Signal should fire"

    signals = list(csv.DictReader(open(tmp_path / "signals.csv")))
    assert len(signals) == 1

    # --- next poll with basis blow-out (120 bps) ---
    # To create 120 bps basis, we need perp much higher than spot
    # basis_bps = (perp_mid - spot_mid) / spot_mid * 10_000
    # For spot_mid = 60000.5, to get 120 bps: perp_mid - spot_mid = 0.012 * 60000.5 = 720
    # So perp_mid ≈ 60720
    clock_1 = clock_0 + timedelta(hours=8)
    setup_mocks_for_clock_with_basis(clock_1, rate="0.0005", perp_bid="60719.0", perp_ask="60721.0")

    hedges = spike.poll_once(hedges, clock=clock_1)
    assert len(hedges) == 0, "Hedge should be closed when basis blow-out (> 100 bps) is detected"

    # Verify cycles.csv
    cycles = list(csv.DictReader(open(tmp_path / "cycles.csv")))
    assert len(cycles) == 1, "Should have exactly 1 closed cycle"
    cycle = cycles[0]
    assert "basis" in cycle["reason"].lower(), f"Reason should mention basis blow-out, got: {cycle['reason']}"
