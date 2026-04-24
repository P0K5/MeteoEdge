"""Main polling loop + virtual hedge bookkeeping."""
import argparse
import csv
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    UNIVERSE, POLL_INTERVAL_SECONDS, PERSISTENCE_LOOKBACK_HOURS,
    ENTRY_THRESHOLD_BPS, ROUND_TRIP_FEE_BPS, VIRTUAL_NOTIONAL_USD,
    TARGET_HOLD_HOURS, LOG_DIR, SIGNALS_CSV, CYCLES_CSV,
    SNAPSHOTS_JSONL, OPEN_HEDGES_JSON,
)
from scorer import (
    MarketState, should_enter, should_exit,
    compute_basis_bps, persistence_fraction_from_history, rate_to_bps,
)
from binance_client import (
    get_spot_book_ticker, get_perp_book_ticker,
    get_premium_index, get_funding_history,
)


def load_open_hedges() -> dict[str, dict]:
    if not OPEN_HEDGES_JSON.exists():
        return {}
    return json.loads(OPEN_HEDGES_JSON.read_text())


def save_open_hedges(hedges: dict[str, dict]) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    tmp = OPEN_HEDGES_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(hedges, indent=2, default=str))
    tmp.replace(OPEN_HEDGES_JSON)


def append_csv(path: Path, row: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    new_file = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            w.writeheader()
        w.writerow(row)


def append_snapshot(snap: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with open(SNAPSHOTS_JSONL, "a") as f:
        f.write(json.dumps(snap, default=str) + "\n")


def fetch_market_state(symbol: str, clock=None) -> "MarketState | None":
    """Fetch all Binance data and build a MarketState. Returns None on any error.

    clock: if provided, used as the 'now_utc' timestamp; otherwise datetime.now(utc) is used.
    This allows E0-S4 tests to freeze time without monkeypatching the datetime module.
    """
    try:
        spot = get_spot_book_ticker(symbol)
        perp = get_perp_book_ticker(symbol)
        prem = get_premium_index(symbol)

        spot_bid, spot_ask = float(spot["bidPrice"]), float(spot["askPrice"])
        perp_bid, perp_ask = float(perp["bidPrice"]), float(perp["askPrice"])
        spot_mid = (spot_bid + spot_ask) / 2
        perp_mid = (perp_bid + perp_ask) / 2
        basis_bps = compute_basis_bps(spot_mid, perp_mid)

        funding_rate = float(prem["lastFundingRate"])
        predicted_rate = float(prem.get("predictedFundingRate", funding_rate))
        funding_time = datetime.fromtimestamp(int(prem["nextFundingTime"]) / 1000, tz=timezone.utc)

        # Persistence: 72h of funding history (9 settlements)
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - PERSISTENCE_LOOKBACK_HOURS * 3600 * 1000
        history = get_funding_history(symbol, start_ms, now_ms)
        threshold_rate = ENTRY_THRESHOLD_BPS / 10_000
        persistence = persistence_fraction_from_history(history, threshold_rate)

        now_utc = clock if clock is not None else datetime.now(timezone.utc)

        return MarketState(
            symbol=symbol,
            now_utc=now_utc,
            funding_rate=funding_rate,
            predicted_rate=predicted_rate,
            funding_time=funding_time,
            spot_bid=spot_bid, spot_ask=spot_ask,
            perp_bid=perp_bid, perp_ask=perp_ask,
            basis_bps=basis_bps,
            persistence_fraction=persistence,
        )
    except Exception as e:
        print(f"[fetch] {symbol} error: {e}")
        return None


def accrue_funding(hedge: dict, state: MarketState) -> None:
    """If a funding settlement happened since the last poll, accrue the payment into the hedge.

    Uses state.now_utc (which comes from the injected clock) rather than datetime.now()
    directly — this makes the function testable with a fake clock.

    Fires exactly once per 8h boundary: the guard on last_accrued_at prevents double-accrual
    if poll_once is called repeatedly without a real settlement occurring.
    """
    last_accrued = datetime.fromisoformat(hedge["last_accrued_at"]) if hedge.get("last_accrued_at") else None
    funding_time = state.funding_time
    # Binance's `nextFundingTime` updates after each settlement; the *previous* settlement was 8h before.
    prev_settlement = funding_time - timedelta(hours=8)
    if last_accrued is None or prev_settlement > last_accrued:
        # We would have collected one funding payment at prev_settlement
        rate = state.funding_rate  # realised rate at that settlement
        payment = rate * hedge["notional_usd"]
        hedge["accrued_funding_usd"] += payment
        hedge["funding_events_count"] += 1
        hedge["last_accrued_at"] = prev_settlement.isoformat()
        print(f"  [{state.symbol}] accrued funding ${payment:+.4f} (rate {rate_to_bps(rate):+.2f} bps)")


def open_virtual_hedge(state: MarketState) -> dict:
    hedge = {
        "id": str(uuid.uuid4())[:8],
        "symbol": state.symbol,
        "notional_usd": VIRTUAL_NOTIONAL_USD,
        "opened_at": state.now_utc.isoformat(),
        "spot_entry_price": state.spot_ask,   # we'd be taker on the buy
        "perp_entry_price": state.perp_bid,   # we'd be taker on the sell
        "entry_basis_bps": state.basis_bps,
        "entry_funding_rate_bps": rate_to_bps(state.funding_rate),
        "entry_persistence": state.persistence_fraction,
        "accrued_funding_usd": 0.0,
        "funding_events_count": 0,
        "negative_streak": 0,
        "last_accrued_at": None,
    }
    append_csv(SIGNALS_CSV, {
        "signal_at": state.now_utc.isoformat(),
        "hedge_id": hedge["id"],
        "symbol": state.symbol,
        "funding_rate_bps": rate_to_bps(state.funding_rate),
        "predicted_rate_bps": rate_to_bps(state.predicted_rate),
        "basis_bps": state.basis_bps,
        "persistence": state.persistence_fraction,
        "spot_ask": state.spot_ask,
        "perp_bid": state.perp_bid,
    })
    print(f"  ** SIGNAL {state.symbol} enter @ rate={rate_to_bps(state.funding_rate):.2f} bps basis={state.basis_bps:.2f} bps")
    return hedge


def close_virtual_hedge(hedge: dict, state: MarketState, reason: str) -> None:
    """Compute P&L and append to cycles.csv.

    Uses state.now_utc (which comes from the injected clock) for the close timestamp,
    so the function is testable without monkeypatching datetime.
    """
    # Basis P&L: the spread between perp and spot has moved since entry. A widening basis (perp > spot)
    # costs the hedge when we unwind (buy perp back expensive, sell spot cheap).
    exit_basis_bps = state.basis_bps
    entry_basis_bps = hedge["entry_basis_bps"]
    basis_drift_bps = exit_basis_bps - entry_basis_bps     # positive = hedge cost us
    basis_pnl_usd = -basis_drift_bps / 10_000 * hedge["notional_usd"]

    fees_usd = ROUND_TRIP_FEE_BPS / 10_000 * hedge["notional_usd"]

    net_pnl_usd = hedge["accrued_funding_usd"] + basis_pnl_usd - fees_usd
    net_bps = net_pnl_usd / hedge["notional_usd"] * 10_000

    hold_hours = (state.now_utc - datetime.fromisoformat(hedge["opened_at"])).total_seconds() / 3600

    row = {
        "hedge_id": hedge["id"],
        "symbol": hedge["symbol"],
        "opened_at": hedge["opened_at"],
        "closed_at": state.now_utc.isoformat(),
        "hold_hours": round(hold_hours, 2),
        "funding_events": hedge["funding_events_count"],
        "accrued_funding_usd": round(hedge["accrued_funding_usd"], 4),
        "basis_pnl_usd": round(basis_pnl_usd, 4),
        "fees_usd": round(fees_usd, 4),
        "net_pnl_usd": round(net_pnl_usd, 4),
        "net_bps": round(net_bps, 2),
        "entry_basis_bps": round(entry_basis_bps, 2),
        "exit_basis_bps": round(exit_basis_bps, 2),
        "entry_rate_bps": round(hedge["entry_funding_rate_bps"], 2),
        "exit_rate_bps": round(rate_to_bps(state.funding_rate), 2),
        "reason": reason,
    }
    append_csv(CYCLES_CSV, row)
    print(f"  ** CLOSE {hedge['symbol']} hedge {hedge['id']} net={net_pnl_usd:+.3f} USD ({net_bps:+.1f} bps) reason={reason}")


def track_negative_streak(hedge: dict, state: MarketState) -> None:
    if rate_to_bps(state.funding_rate) < 0:
        hedge["negative_streak"] = hedge.get("negative_streak", 0) + 1
    else:
        hedge["negative_streak"] = 0


def poll_once(open_hedges: dict[str, dict], clock=None) -> dict[str, dict]:
    """Run one full poll over all UNIVERSE symbols.

    clock: if provided, used as 'now_utc' for every MarketState built during this poll.
    Pass a datetime to freeze time in tests; leave as None in production.
    """
    if clock is None:
        clock = datetime.now(timezone.utc)
    ts = clock.isoformat()
    print(f"\n=== Poll at {ts} ===")

    for symbol in UNIVERSE:
        state = fetch_market_state(symbol, clock=clock)
        if not state:
            continue

        append_snapshot({
            "ts": ts,
            "symbol": symbol,
            "funding_rate_bps": rate_to_bps(state.funding_rate),
            "predicted_rate_bps": rate_to_bps(state.predicted_rate),
            "basis_bps": state.basis_bps,
            "persistence": state.persistence_fraction,
            "spot_mid": (state.spot_bid + state.spot_ask) / 2,
            "perp_mid": (state.perp_bid + state.perp_ask) / 2,
        })

        print(
            f"[{symbol}] rate={rate_to_bps(state.funding_rate):+.2f} bps "
            f"basis={state.basis_bps:+.2f} bps persistence={state.persistence_fraction:.2f}"
        )

        # 1. Update any open hedge on this symbol
        if symbol in open_hedges:
            hedge = open_hedges[symbol]
            accrue_funding(hedge, state)
            track_negative_streak(hedge, state)

            hold_hours = (state.now_utc - datetime.fromisoformat(hedge["opened_at"])).total_seconds() / 3600
            # Target-hold close (deterministic cycle end for comparability)
            if hold_hours >= TARGET_HOLD_HOURS:
                close_virtual_hedge(hedge, state, reason=f"target_hold_reached_{TARGET_HOLD_HOURS}h")
                del open_hedges[symbol]
                continue
            # Rule-based close
            exit_flag, exit_reason = should_exit(state, hold_hours, hedge.get("negative_streak", 0))
            if exit_flag:
                close_virtual_hedge(hedge, state, reason=exit_reason)
                del open_hedges[symbol]
                continue

        # 2. Otherwise consider entering
        if symbol not in open_hedges:
            enter_flag, enter_reason = should_enter(state)
            if enter_flag:
                open_hedges[symbol] = open_virtual_hedge(state)
            else:
                # Only log rejection at INFO level occasionally
                pass

    save_open_hedges(open_hedges)
    return open_hedges


def smoke_test() -> None:
    """Smoke test: fetch spot book ticker for BTCUSDT and exit cleanly."""
    try:
        result = get_spot_book_ticker("BTCUSDT")
        print(f"Spot book ticker for BTCUSDT: {result}")
        print("Smoke test passed.")
        sys.exit(0)
    except Exception as e:
        print(f"Smoke test failed: {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="FundingEdge spike: observe-only virtual hedge tracker")
    parser.add_argument("--smoke-test", action="store_true", help="Run smoke test (fetch data for 1 symbol and exit)")
    args = parser.parse_args()

    if args.smoke_test:
        smoke_test()
        return

    print("FundingEdge spike starting. Observe-only mode.")
    print("Ctrl-C to stop. Logs under ./logs/")
    LOG_DIR.mkdir(exist_ok=True)
    open_hedges = load_open_hedges()
    if open_hedges:
        print(f"Resumed with {len(open_hedges)} open virtual hedges: {list(open_hedges)}")

    while True:
        try:
            open_hedges = poll_once(open_hedges)
        except KeyboardInterrupt:
            print("\nStopping. Open virtual hedges persisted to logs/open_hedges.json.")
            break
        except Exception as e:
            print(f"[loop] unhandled error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
