# FundingEdge — MVP Spike (Observe-Only)

**Purpose:** Validate the core premise of the FundingEdge strategy before investing in the full build.
**Mode:** Observe-only. No orders placed. No real capital at risk.
**Duration:** 1 weekend to build, 2 weeks to run.
**Exit criterion:** Counterfactual 3-day-hold P&L positive after round-trip fees in ≥ 60% of flagged entry signals, with ≥ 30 signals total. If not, stop and revisit the spec.

---

## 1. What This Spike Does

Every 60 seconds, for the V1 universe (BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT):

1. Pulls the current funding rate and predicted-next-funding rate from Binance USDⓈ-M
2. Pulls the current spot bid/ask and perp bid/ask; computes basis in bps
3. Scores each symbol against the entry rules from `funding-edge-spec.md §7.1`
4. Flags any symbol crossing the entry threshold and logs a **virtual hedge** candidate to CSV
5. Tracks each virtual hedge forward in time at 1-minute granularity, accumulating the funding payments it *would have* collected and the basis drift it *would have* suffered
6. "Closes" each virtual hedge 3 days later (or sooner if exit conditions trigger) and records the counterfactual net P&L

At the end of 2 weeks, a report script computes:

- How many entry signals fired
- Win rate (% of signals where counterfactual net P&L > 0 after round-trip fees)
- Median and mean net yield in bps per signal
- Per-symbol and per-time-of-day attribution

If the win rate clears 60% on ≥ 30 signals, the premise is confirmed and we build the full system. If not, the strategy as specified does not work and we revisit.

## 2. What This Spike Deliberately Skips

- **No order placement.** Observation only. Binance read-only API key is sufficient.
- **No database.** Everything in CSV and JSONL files.
- **No services.** Run manually under `tmux` or `nohup`.
- **No dashboard.** Just log files and a report script.
- **No LLM sanity check.** The full spec adds this; the spike tests whether the rules engine alone produces a real edge.
- **No hedge executor, no reconciler.** Virtual positions are tracked in memory + CSV.
- **No margin calculation.** The spike is risk-free because nothing executes; margin modelling is Stage 1 concern.
- **Hardcoded config.** Symbol list, thresholds, and polling interval are constants in the script.

If the spike fails, none of the above would have saved it. If it succeeds, we know the edge exists before building the infrastructure around it. This is the same discipline that served MeteoEdge well.

## 3. Prerequisites

- Python 3.11+
- A Binance account with **read-only** API key (Settings → API Management → create key; disable trading and withdrawal permissions)
- Internet access to `api.binance.com` and `fapi.binance.com`

Install:

```bash
pip install python-binance httpx pandas python-dateutil pytz
```

No cryptographic signing library needed — `python-binance` handles HMAC signing internally.

## 4. Project Layout

```
fundingedge-spike/
├── spike.py                  # main polling loop + virtual hedge manager
├── report.py                 # computes win rate and net yield stats
├── config.py                 # hardcoded config
├── binance_client.py         # thin Binance API wrapper (read-only)
├── scorer.py                 # funding score + entry/exit logic (stateless)
├── logs/
│   ├── signals.csv           # every entry signal flagged
│   ├── cycles.csv            # closed virtual hedges (P&L per cycle)
│   ├── snapshots.jsonl       # raw market + funding snapshots (debugging gold)
│   └── open_hedges.json      # in-flight virtual hedges (atomic, rewritten each poll)
└── keys/
    └── binance_keys.env      # chmod 600
```

## 5. The Code

### 5.1 `config.py`

```python
"""Hardcoded config for the spike. Promote to env vars in the full build."""
from pathlib import Path

# Universe
UNIVERSE = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

# Polling cadence
POLL_INTERVAL_SECONDS = 60

# Strategy thresholds (mirror funding-edge-spec.md §7.1–§7.2)
ENTRY_THRESHOLD_BPS = 3.0           # 0.03% per 8h funding interval
EXIT_THRESHOLD_BPS = 1.0
ENTRY_BASIS_CEILING_BPS = 20.0
EMERGENCY_BASIS_BPS = 100.0
PERSISTENCE_LOOKBACK_HOURS = 72     # require rate > threshold in 60% of last 72h
PERSISTENCE_FRACTION_MIN = 0.60
MIN_MINUTES_TO_NEXT_FUNDING = 30

# Fee model (round-trip, matches spec §7.4)
SPOT_TAKER_FEE_BPS = 7.5            # 0.075% with BNB discount
PERP_TAKER_FEE_BPS = 5.0            # 0.050%
# Round-trip: entry + exit on both legs = 2 * (7.5 + 5.0) = 25 bps
ROUND_TRIP_FEE_BPS = 2 * (SPOT_TAKER_FEE_BPS + PERP_TAKER_FEE_BPS)

# Virtual hedge sizing (notional only — not real capital)
VIRTUAL_NOTIONAL_USD = 500.0

# Holding behaviour
MAX_HOLD_HOURS = 14 * 24            # 14-day max, matches spec
TARGET_HOLD_HOURS = 3 * 24          # 3-day target — long enough to amortise round-trip fees

# Output paths
LOG_DIR = Path("logs")
SIGNALS_CSV = LOG_DIR / "signals.csv"
CYCLES_CSV = LOG_DIR / "cycles.csv"
SNAPSHOTS_JSONL = LOG_DIR / "snapshots.jsonl"
OPEN_HEDGES_JSON = LOG_DIR / "open_hedges.json"

# HTTP
HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "FundingEdge-Spike/0.1 (contact: you@example.com)"
```

### 5.2 `binance_client.py`

```python
"""Read-only Binance API wrapper. Spike-scoped: no signing needed for public endpoints."""
import httpx
from config import HTTP_TIMEOUT_SECONDS, USER_AGENT

SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"


def _get(url: str, params: dict | None = None) -> dict | list:
    r = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json()


def get_spot_book_ticker(symbol: str) -> dict:
    """Best bid/ask + sizes for spot."""
    return _get(f"{SPOT_BASE}/api/v3/ticker/bookTicker", {"symbol": symbol})


def get_perp_book_ticker(symbol: str) -> dict:
    """Best bid/ask + sizes for USDⓈ-M perp."""
    return _get(f"{FUTURES_BASE}/fapi/v1/ticker/bookTicker", {"symbol": symbol})


def get_premium_index(symbol: str) -> dict:
    """Mark price, index price, last funding rate, predicted next rate, funding time."""
    return _get(f"{FUTURES_BASE}/fapi/v1/premiumIndex", {"symbol": symbol})


def get_funding_history(symbol: str, start_ms: int, end_ms: int, limit: int = 1000) -> list[dict]:
    """Historical realised funding rates — used for persistence score."""
    params = {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": limit}
    return _get(f"{FUTURES_BASE}/fapi/v1/fundingRate", params)
```

### 5.3 `scorer.py`

```python
"""Funding score + entry/exit logic. Stateless; same shape as production module."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from config import (
    ENTRY_THRESHOLD_BPS, EXIT_THRESHOLD_BPS, ENTRY_BASIS_CEILING_BPS,
    EMERGENCY_BASIS_BPS, PERSISTENCE_FRACTION_MIN, MIN_MINUTES_TO_NEXT_FUNDING,
)


@dataclass
class MarketState:
    symbol: str
    now_utc: datetime
    funding_rate: float           # realised, as decimal (not bps); 0.0003 = 3 bps = 0.03%/8h
    predicted_rate: float
    funding_time: datetime
    spot_bid: float
    spot_ask: float
    perp_bid: float
    perp_ask: float
    basis_bps: float
    persistence_fraction: float   # fraction of last 72h where rate > entry threshold


def rate_to_bps(rate: float) -> float:
    """Convert decimal (0.0003) to bps (3.0)."""
    return rate * 10_000


def should_enter(s: MarketState) -> tuple[bool, str]:
    """Return (decision, reason)."""
    rate_bps = rate_to_bps(s.funding_rate)
    if rate_bps < ENTRY_THRESHOLD_BPS:
        return False, f"rate {rate_bps:.2f} bps < entry threshold {ENTRY_THRESHOLD_BPS}"
    if s.persistence_fraction < PERSISTENCE_FRACTION_MIN:
        return False, f"persistence {s.persistence_fraction:.2f} < {PERSISTENCE_FRACTION_MIN}"
    if s.basis_bps > ENTRY_BASIS_CEILING_BPS:
        return False, f"basis {s.basis_bps:.2f} bps > ceiling {ENTRY_BASIS_CEILING_BPS}"
    minutes_to_funding = (s.funding_time - s.now_utc).total_seconds() / 60
    if minutes_to_funding < MIN_MINUTES_TO_NEXT_FUNDING:
        return False, f"only {minutes_to_funding:.1f} min to next funding"
    return True, "all entry rules passed"


def should_exit(s: MarketState, hedge_age_hours: float, negative_streak: int) -> tuple[bool, str]:
    """Decide whether to close an open virtual hedge."""
    rate_bps = rate_to_bps(s.funding_rate)
    if rate_bps < EXIT_THRESHOLD_BPS:
        return True, f"rate {rate_bps:.2f} bps < exit threshold"
    if negative_streak >= 2:
        return True, f"{negative_streak} consecutive negative-funding windows"
    if s.basis_bps > EMERGENCY_BASIS_BPS:
        return True, f"basis blow-out {s.basis_bps:.2f} bps"
    if hedge_age_hours > 14 * 24:
        return True, "max holding period reached"
    return False, "hold"


def compute_basis_bps(spot_mid: float, perp_mid: float) -> float:
    if spot_mid <= 0:
        return 0.0
    return (perp_mid - spot_mid) / spot_mid * 10_000


def persistence_fraction_from_history(history: list[dict], threshold_rate: float) -> float:
    """Fraction of historical funding payments at or above threshold.
    history items: {"fundingRate": "0.00012", "fundingTime": 1700000000000}."""
    if not history:
        return 0.0
    qualifying = sum(1 for h in history if float(h["fundingRate"]) >= threshold_rate)
    return qualifying / len(history)
```

### 5.4 `spike.py`

```python
"""Main polling loop + virtual hedge bookkeeping."""
import csv
import json
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


def fetch_market_state(symbol: str) -> MarketState | None:
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

        return MarketState(
            symbol=symbol,
            now_utc=datetime.now(timezone.utc),
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
    """If a funding settlement happened since the last poll, accrue the payment into the hedge."""
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


def poll_once(open_hedges: dict[str, dict]) -> dict[str, dict]:
    ts = datetime.now(timezone.utc).isoformat()
    print(f"\n=== Poll at {ts} ===")

    for symbol in UNIVERSE:
        state = fetch_market_state(symbol)
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


def main() -> None:
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
```

### 5.5 `report.py`

```python
"""Summarize hit rate, net yield, and per-symbol attribution after N days."""
import csv
import statistics
from collections import defaultdict
from config import CYCLES_CSV


def main() -> None:
    if not CYCLES_CSV.exists():
        print("No closed cycles yet.")
        return

    rows = list(csv.DictReader(open(CYCLES_CSV)))
    n = len(rows)
    if n == 0:
        print("Zero closed cycles.")
        return

    wins = sum(1 for r in rows if float(r["net_pnl_usd"]) > 0)
    win_rate = wins / n
    net_pnl = [float(r["net_pnl_usd"]) for r in rows]
    net_bps = [float(r["net_bps"]) for r in rows]
    total_pnl = sum(net_pnl)
    median_bps = statistics.median(net_bps)
    mean_bps = statistics.mean(net_bps)
    stdev_bps = statistics.pstdev(net_bps) if n > 1 else 0.0

    by_symbol = defaultdict(list)
    for r in rows:
        by_symbol[r["symbol"]].append(r)

    print("\n=== FundingEdge Spike Report ===")
    print(f"Closed virtual cycles: {n}")
    print(f"Wins: {wins}  Win rate: {win_rate:.2%}")
    print(f"Total net P&L (USD): {total_pnl:+.2f}")
    print(f"Median net yield per cycle: {median_bps:+.2f} bps")
    print(f"Mean net yield per cycle:   {mean_bps:+.2f} bps (stdev {stdev_bps:.2f})")
    print("\nBy symbol:")
    for symbol, items in sorted(by_symbol.items()):
        w = sum(1 for r in items if float(r["net_pnl_usd"]) > 0)
        avg_bps = statistics.mean(float(r["net_bps"]) for r in items)
        print(f"  {symbol}: {len(items)} cycles, {w} wins ({w/len(items):.1%}), avg {avg_bps:+.2f} bps")

    print("\n" + "=" * 40)
    if n >= 30 and win_rate >= 0.60 and median_bps > 0:
        print(f"GREEN LIGHT: {win_rate:.2%} win rate on {n} cycles, median {median_bps:+.2f} bps. Proceed to full build.")
    elif n >= 30 and win_rate >= 0.55:
        print(f"YELLOW: win rate {win_rate:.2%} is marginal. Run 1 more week. Investigate per-symbol patterns.")
    elif n < 30:
        print(f"INCONCLUSIVE: only {n} cycles. Run until n >= 30 before deciding.")
    else:
        print(f"RED LIGHT: win rate {win_rate:.2%} < 55% on {n} cycles. Do not proceed. Revisit spec.")


if __name__ == "__main__":
    main()
```

## 6. How to Run

### Day 0 — one-time setup

```bash
cd fundingedge-spike
python -m venv .venv && source .venv/bin/activate
pip install python-binance httpx pandas python-dateutil pytz
mkdir -p logs keys
chmod 700 keys
# Create a Binance API key with READ-ONLY permissions (uncheck trading and withdrawal)
# Save keys to keys/binance_keys.env (not strictly needed — spike uses public endpoints only)
```

### Run for 2 weeks

```bash
# Under tmux or nohup so it survives disconnects
tmux new -s fundingedge-spike
python spike.py
# Detach: Ctrl-b d
# Reattach: tmux attach -t fundingedge-spike
```

The loop polls every 60s across 4 symbols. Expected log volume: ~8 MB/day of snapshots, ~1 KB/day of signals and cycles.

### Check progress any time

```bash
python report.py
```

## 7. Decision Criteria

After 2 weeks of clean data, run `report.py`:

| Outcome | Decision |
|---|---|
| n ≥ 30 AND win rate ≥ 60% AND median net yield > 0 bps | **Green light.** Proceed to the full build per `funding-edge-spec.md`. |
| n ≥ 30 AND win rate 55–60% | **Yellow.** Extend to 3 weeks. Look at per-symbol breakdown — maybe 2 symbols have edge and 2 don't. Consider narrowing the universe. |
| n ≥ 30 AND win rate < 55% | **Red light.** Premise is weaker than expected. Do not build. Investigate: is the entry threshold too aggressive? Is basis drift systematically eating the funding? Was the 2-week window dominated by a funding-regime change? |
| n < 30 after 2 weeks | Signals too rare. Either lower `ENTRY_THRESHOLD_BPS` to 2, extend persistence lookback, or accept that current funding regime is too quiet. Do NOT lower the round-trip fee assumption — that's the one number that has to be conservative. |

## 8. Known Spike Limitations

These are accepted for the spike and addressed in the full build:

- **Fee model is flat round-trip bps.** Real Binance fees vary by VIP tier and BNB balance; the spike assumes BNB-paid taker fees. Real fees should be within ±1 bps of this estimate.
- **Slippage is zero in the counterfactual.** The virtual hedge enters at the current book-top price on both legs. Real execution adds 1–5 bps of slippage depending on depth. The full backtest models this; the spike doesn't.
- **Funding accrual timing.** The spike detects a funding event by observing `nextFundingTime` advance. If the poll misses a beat, accrual is still correct (the rate applies to the whole position) but timestamps may be off by a minute.
- **No LLM sanity check.** The full spec gates every entry/exit on LLM approval. The spike tests whether the rules engine alone flags an edge. If it does, the LLM layer can only improve things (by catching anomalies) — never worsen them.
- **Single-threaded.** A slow Binance response backs up the loop. At 60s cadence with 4 symbols this is extremely unlikely. Acceptable.
- **No liquidation / margin modelling.** Virtual hedges never liquidate; real perp positions can. Stage 1 backtest and Stage 2 testnet are where liquidation logic lives.
- **Persistence uses only realised funding.** Live pipeline should blend realised + predicted rates. Spike keeps it simple — if this alone finds an edge, the full build finds more.

## 9. What to Hand Off to the Full Build

After the spike concludes, package these artifacts:

1. `logs/signals.csv` — every entry signal flagged with full market state at signal time
2. `logs/cycles.csv` — every closed virtual hedge with P&L attribution
3. `logs/snapshots.jsonl` — full market + funding state at each poll (gold for backtest calibration and parameter sweeps)
4. A short written retro:
   - Which symbols worked, which didn't
   - Funding regime over the observation window (was it a bullish carry-heavy period or quiet?)
   - Anomalies: did any symbol have a sustained negative funding episode? What caused it?
   - Parameter instincts: did `ENTRY_THRESHOLD_BPS = 3.0` feel too strict or too loose?

This becomes the seed dataset for Epic 7 (Backtest Harness) and calibration input for the production scorer.

## 10. Why This Spike Is Different from MeteoEdge's

Both spikes test whether a structural edge exists before committing to infrastructure. The mechanic differs:

| Dimension | MeteoEdge spike | FundingEdge spike |
|---|---|---|
| Edge flags | Bracket EV from envelope vs. ask | Funding rate > threshold with persistence |
| Counterfactual | Would the bracket have settled YES/NO? | What would 3 days of accrued funding net against fees + basis drift? |
| Settlement source | NWS Daily Climate Report (next day) | Binance's own funding payments (every 8h) |
| Time to conclusion | 5–10 trading days | 2 weeks (funding cycles are slower to accumulate stats) |
| Gate metric | ≥ 55% hit rate on ≥ 30 candidates | ≥ 60% win rate on ≥ 30 cycles + positive median |
| Failure cost | One weekend | One weekend |

The higher gate bar (60% vs 55%) reflects that funding-rate arbitrage has a *deterministic income stream*. A 55% bar was appropriate for Kalshi weather because outcomes were binary and settled once a day; funding is paid three times a day with known rate, so we should see a cleaner signal. If the spike can't clear 60%, something about our model of the edge is wrong.

---

*End of spike design. Ready to implement.*
