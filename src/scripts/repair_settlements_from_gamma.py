"""One-off repair: re-settle historical trades from actual Polymarket resolutions (issue #644).

The 2026-07-07 ground-truth audit found that settle.py's METAR-based truth
disagreed with the official market resolution in 28 of 126 audited live
settlements (wrong SIGN), because the 12:00 UTC settle run predates UMA
resolution and fell back to METAR silently. This script re-grades history
against the definitive Gamma resolution:

1. ``trades`` (mode='live' AND outcome='filled' AND 0x ticker): recompute pnl
   from the market's final resolution; fix rows whose stored pnl is wrong and
   settle rows that were never settled. Sold rows are untouched — their pnl
   is realized cash from an actual sell, not a settlement guess.
2. ``trades`` (mode='shadow', settled rows): same re-grade with the $1-notional
   shadow formula (see settle.settle_shadow_trades).
3. ``settlements``: fix resolved_yes / market_final_price / resolution_source
   where they disagree with the definitive resolution.
4. ``risk_state``: rebuild daily_pnl for every date that has live trades, as
   the sum of corrected trades.pnl attributed to each row's own trade date.

Dry-run by default — prints every proposed change. Pass --apply to write.

Usage:
    python -m src.scripts.repair_settlements_from_gamma            # dry run
    python -m src.scripts.repair_settlements_from_gamma --apply
"""
import argparse
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

log = logging.getLogger(__name__)

from src.data.db import Database
from src.data.polymarket import (
    fetch_market_final_price,
    RESOLVED_YES_MIN_CENTS,
    RESOLVED_NO_MAX_CENTS,
)
from src.data.settlements import SettlementWriter
from src.scripts.settle import resolve_trade_date

# stored pnl within this many EUR of the recomputed value counts as correct
PNL_TOLERANCE = 0.01


def fetch_resolutions(tickers: "set[str]", workers: int = 8) -> dict:
    """Return {ticker: (yes_won | None, final_price | None)} for 0x tickers."""
    out: dict = {}

    def one(t: str):
        price = fetch_market_final_price(t)
        if price is None:
            return t, (None, None)
        if price >= RESOLVED_YES_MIN_CENTS:
            return t, (True, price)
        if price <= RESOLVED_NO_MAX_CENTS:
            return t, (False, price)
        return t, (None, price)  # ambiguous — treat as unresolved

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t, res in ex.map(one, sorted(tickers)):
            out[t] = res
    return out


def expected_live_pnl(row: dict, yes_won: bool) -> "float | None":
    """Mirror settle.settle_live_trades' held-to-expiry PnL formula."""
    price_cents = float(row.get("actual_price") or 0)
    size_eur = float(row.get("capital_before") or 0)
    if not price_cents:
        return None
    shares = size_eur / (price_cents / 100)
    side = row.get("side", "NO")
    won = (not yes_won) if side == "NO" else yes_won
    per_share = (100 - price_cents) if won else -price_cents
    return round(per_share / 100 * shares, 4)


def expected_shadow_pnl(row: dict, yes_won: bool) -> "float | None":
    """Mirror settle.settle_shadow_trades' $1-notional PnL formula."""
    if row.get("actual_price") is None:
        return None
    ask = float(row["actual_price"])
    side = row.get("side", "YES")
    won = yes_won if side == "YES" else (not yes_won)
    return round(((100 - ask) if won else -ask) / 100, 6)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write fixes (default: dry run)")
    ap.add_argument("--workers", type=int, default=8, help="parallel Gamma fetches")
    args = ap.parse_args()

    db = Database()
    now_iso = datetime.now(timezone.utc).isoformat()

    live_rows = [
        dict(r) for r in db._conn.execute(
            "SELECT * FROM trades WHERE mode='live' AND outcome='filled' "
            "AND ticker LIKE '0x%'"
        )
    ]
    shadow_rows = [
        dict(r) for r in db._conn.execute(
            "SELECT * FROM trades WHERE mode='shadow' AND ticker LIKE '0x%' "
            "AND settled_at IS NOT NULL AND direction != 'low'"
        )
    ]
    settlement_rows = [
        dict(r) for r in db._conn.execute(
            "SELECT * FROM settlements WHERE ticker LIKE '0x%'"
        )
    ]

    tickers = (
        {r["ticker"] for r in live_rows}
        | {r["ticker"] for r in shadow_rows}
        | {r["ticker"] for r in settlement_rows}
    )
    print(f"live filled rows: {len(live_rows)}, settled shadow rows: {len(shadow_rows)}, "
          f"settlement rows: {len(settlement_rows)}, unique markets: {len(tickers)}")
    print(f"fetching resolutions from Gamma ({args.workers} workers)...")
    resolutions = fetch_resolutions(tickers, workers=args.workers)
    n_resolved = sum(1 for y, _ in resolutions.values() if y is not None)
    print(f"definitively resolved: {n_resolved}/{len(tickers)}")

    # ---- trades (live) -------------------------------------------------
    n_fixed = n_newly_settled = n_pending = n_ok = 0
    pnl_delta = 0.0
    for r in live_rows:
        yes_won, _price = resolutions.get(r["ticker"], (None, None))
        if yes_won is None:
            n_pending += 1
            continue
        want = expected_live_pnl(r, yes_won)
        if want is None:
            continue
        have = r.get("pnl")
        if have is not None and abs(have - want) <= PNL_TOLERANCE:
            n_ok += 1
            continue
        tag = "FIX " if have is not None else "SETTLE"
        print(f"  [{tag}] trade id={r['id']} {r.get('station')} {r.get('side')} "
              f"@{r.get('actual_price')}c {str(r.get('end_date') or r.get('ts'))[:10]} "
              f"pnl {have} -> {want:+.4f}")
        pnl_delta += want - (have or 0.0)
        if have is not None:
            n_fixed += 1
        else:
            n_newly_settled += 1
        if args.apply:
            db.update_trade_by_id(r["id"], pnl=want, settled_at=now_iso)
            if r.get("order_id"):
                db.close_position(r["order_id"])
    print(f"live trades: {n_ok} correct, {n_fixed} wrong (fixed), "
          f"{n_newly_settled} newly settled, {n_pending} unresolved; "
          f"total pnl correction {pnl_delta:+.2f} EUR")

    # ---- trades (shadow) ------------------------------------------------
    n_sh_fixed = n_sh_ok = 0
    for r in shadow_rows:
        yes_won, _price = resolutions.get(r["ticker"], (None, None))
        if yes_won is None:
            continue
        want = expected_shadow_pnl(r, yes_won)
        if want is None:
            continue
        have = r.get("pnl")
        if have is not None and abs(have - want) <= PNL_TOLERANCE:
            n_sh_ok += 1
            continue
        n_sh_fixed += 1
        if args.apply:
            db.update_trade_by_id(r["id"], pnl=want, capital_after=want)
    print(f"shadow trades: {n_sh_ok} correct, {n_sh_fixed} re-graded")

    # ---- settlements table ----------------------------------------------
    writer = SettlementWriter(db)
    n_set_fixed = 0
    for r in settlement_rows:
        yes_won, price = resolutions.get(r["ticker"], (None, None))
        if yes_won is None:
            continue
        if bool(r["resolved_yes"]) == yes_won and r.get("market_final_price") is not None:
            continue
        n_set_fixed += 1
        print(f"  [SETTLEMENT] {r['ticker'][:14]}... {r.get('station')} "
              f"resolved_yes {r['resolved_yes']} -> {int(yes_won)} (final price {price})")
        if args.apply:
            writer.record_settlement(
                ticker=r["ticker"],
                station=r.get("station", ""),
                bracket_low=r.get("bracket_low"),
                bracket_high=r.get("bracket_high"),
                actual_high_f=r.get("actual_high_f"),
                resolved_yes=yes_won,
                market_final_price=price,
                resolution_source="gamma_repair",
            )
    print(f"settlements: {n_set_fixed} rows corrected")

    # ---- risk_state rebuild ----------------------------------------------
    # Attribute each live trade's pnl to its own trade date and overwrite the
    # daily_pnl rows for dates that have live trades. Dates without live
    # trades are left untouched.
    daily: dict = defaultdict(float)
    for r in db._conn.execute(
        "SELECT * FROM trades WHERE mode='live' AND pnl IS NOT NULL"
    ):
        row = dict(r)
        # re-read corrected pnl when applying; dry-run shows projected values
        d = resolve_trade_date(row)
        if d is None:
            continue
        daily[d.isoformat()] += row["pnl"]
    print("\nrebuilt daily_pnl (projected)" if not args.apply else "\nrebuilt daily_pnl:")
    for day in sorted(daily):
        old = db.get_daily_pnl(day)
        marker = "" if abs(old - daily[day]) <= PNL_TOLERANCE else "   <-- changed"
        print(f"  {day}: {old:+.2f} -> {daily[day]:+.2f}{marker}")
        if args.apply:
            db._conn.execute(
                "INSERT INTO risk_state(trade_date, daily_pnl, open_positions, updated_at) "
                "VALUES(?, ?, 0, ?) "
                "ON CONFLICT(trade_date) DO UPDATE SET "
                "daily_pnl=excluded.daily_pnl, updated_at=excluded.updated_at",
                (day, daily[day], now_iso),
            )
    if args.apply:
        db._conn.commit()
        print("\nAPPLIED.")
    else:
        print("\nDRY RUN — nothing written. Re-run with --apply to write. "
              "(risk_state projection above uses PRE-fix pnl values; the "
              "--apply run recomputes it after the trade fixes.)")


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    main()
