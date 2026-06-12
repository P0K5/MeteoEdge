"""Fee model calibration against actual CLOB fill history.

Fetches all live filled orders from the Polymarket CLOB trade history,
computes actual fee vs estimate_fee_cents(price) across the traded price
range, and reports mean signed error and mean absolute error by price decile.

Usage:
    python scripts/fee_calibration.py

Requirements:
    - POLYMARKET_* env vars must be set (same as live trading)
    - At least 30 fills required; script aborts with a clear message otherwise

Output:
    - Console report: price decile table + summary statistics
    - Exit 0 if MAE <= 0.25¢ (formula validated)
    - Exit 1 if MAE > 0.25¢ (formula needs recalibration — see recommendation)
"""
import os
import sys
from pathlib import Path

# Allow running from repo root or scripts/ directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.execution.auth import get_clob_client
from src.strategy.fee import estimate_fee_cents

MIN_FILLS = 30
MAE_THRESHOLD = 0.25  # cents


def fetch_fills(client) -> list[dict]:
    """Fetch taker-side fill records from CLOB trade history.

    Returns a list of dicts with keys: price_cents, actual_fee_cents, size.
    Only includes records where fee information is present.
    """
    fills = []
    try:
        # py_clob_client_v2 trade history endpoint
        # Common method names across CLOB client versions
        trades = None
        for method_name in ("get_trades", "get_trade_history", "get_last_trades"):
            method = getattr(client, method_name, None)
            if method is not None:
                try:
                    resp = method()
                    if isinstance(resp, list):
                        trades = resp
                    elif isinstance(resp, dict):
                        trades = resp.get("data") or resp.get("trades") or []
                    break
                except Exception:
                    continue

        if trades is None:
            # Fallback: try get_trades with empty params
            try:
                from py_clob_client_v2.clob_types import TradeParams
                trades = client.get_trades(TradeParams()) or []
            except Exception:
                trades = []

    except Exception as e:
        print(f"ERROR: Failed to fetch trades: {e}", file=sys.stderr)
        return []

    for trade in trades:
        # Fee fields vary by API version; try common names
        fee_amount = None
        for fee_field in ("fee", "fee_amount", "taker_fee", "makerFee", "takerFee"):
            val = trade.get(fee_field)
            if val is not None:
                try:
                    fee_amount = float(val)
                    break
                except (TypeError, ValueError):
                    continue

        if fee_amount is None:
            continue

        # Price field
        price = None
        for price_field in ("price", "trade_price", "executedPrice"):
            val = trade.get(price_field)
            if val is not None:
                try:
                    price = float(val)
                    break
                except (TypeError, ValueError):
                    continue

        if price is None or price <= 0:
            continue

        # Size (contracts filled)
        size = None
        for size_field in ("size", "size_matched", "matched_amount", "quantity"):
            val = trade.get(size_field)
            if val is not None:
                try:
                    size = float(val)
                    break
                except (TypeError, ValueError):
                    continue

        if size is None or size <= 0:
            continue

        price_cents = round(price * 100)
        if price_cents < 1 or price_cents > 99:
            continue

        # Fee per contract in cents
        actual_fee_per_contract = (fee_amount / size) * 100  # convert to cents

        fills.append({
            "price_cents": price_cents,
            "actual_fee_cents": actual_fee_per_contract,
            "size": size,
        })

    return fills


def compute_calibration_report(fills: list[dict]) -> dict:
    """Compute error statistics by price decile.

    Returns dict with keys: decile_table, mae, mean_signed_error, n_fills,
    recommended_coefficient (if MAE > threshold).
    """
    import math

    errors = []
    for f in fills:
        pc = f["price_cents"]
        estimated = estimate_fee_cents(pc)
        actual = f["actual_fee_cents"]
        errors.append({
            "price_cents": pc,
            "actual": actual,
            "estimated": estimated,
            "error": actual - estimated,
            "abs_error": abs(actual - estimated),
        })

    # Sort by price for decile grouping
    errors.sort(key=lambda x: x["price_cents"])
    n = len(errors)

    # Build deciles (10 groups)
    decile_size = max(1, n // 10)
    decile_table = []
    for i in range(0, n, decile_size):
        chunk = errors[i:i + decile_size]
        if not chunk:
            continue
        prices = [e["price_cents"] for e in chunk]
        mean_price = sum(prices) / len(prices)
        mean_signed = sum(e["error"] for e in chunk) / len(chunk)
        mean_abs = sum(e["abs_error"] for e in chunk) / len(chunk)
        decile_table.append({
            "price_range": f"{min(prices)}-{max(prices)}¢",
            "mean_price_cents": round(mean_price, 1),
            "n": len(chunk),
            "mean_signed_error": round(mean_signed, 4),
            "mae": round(mean_abs, 4),
        })

    overall_mae = sum(e["abs_error"] for e in errors) / n
    overall_mse = sum(e["error"] for e in errors) / n

    # Empirically fit coefficient for p*(1-p) model: minimize sum of squared errors
    # fee = max(1, k * p * (1-p))  → for p*(1-p) > 1/k, fee = k*p*(1-p)
    # Simple linear regression on the quadratic term (for unclamped points only)
    quadratic_fits = [(e["price_cents"] / 100) * (1 - e["price_cents"] / 100)
                      for e in errors]
    actuals_unclamped = [e["actual"] for e in errors]
    # OLS: k = sum(q_i * a_i) / sum(q_i^2)
    sum_qa = sum(q * a for q, a in zip(quadratic_fits, actuals_unclamped))
    sum_q2 = sum(q * q for q in quadratic_fits)
    recommended_coefficient = sum_qa / sum_q2 if sum_q2 > 0 else 7.0

    return {
        "decile_table": decile_table,
        "mae": round(overall_mae, 4),
        "mean_signed_error": round(overall_mse, 4),
        "n_fills": n,
        "recommended_coefficient": round(recommended_coefficient, 4),
    }


def print_report(report: dict, validation_date: str) -> None:
    print()
    print("=" * 65)
    print("  Fee Model Calibration Report")
    print(f"  Date: {validation_date}   N={report['n_fills']} fills")
    print("=" * 65)
    print(f"{'Price Range':<15} {'N':>4} {'Mean Price':>11} {'Signed Err':>12} {'MAE':>8}")
    print("-" * 65)
    for row in report["decile_table"]:
        print(
            f"{row['price_range']:<15} {row['n']:>4} {row['mean_price_cents']:>10.1f}¢"
            f" {row['mean_signed_error']:>+11.4f}¢ {row['mae']:>7.4f}¢"
        )
    print("-" * 65)
    print(f"{'OVERALL':<15} {report['n_fills']:>4}              "
          f" {report['mean_signed_error']:>+11.4f}¢ {report['mae']:>7.4f}¢")
    print()
    if report["mae"] <= MAE_THRESHOLD:
        print(f"✓ VALIDATED: MAE {report['mae']:.4f}¢ ≤ {MAE_THRESHOLD}¢ threshold")
        print(f"  Current formula max(1.0, 7.0*p*(1-p)) is within acceptable error.")
    else:
        print(f"✗ RECALIBRATION NEEDED: MAE {report['mae']:.4f}¢ > {MAE_THRESHOLD}¢ threshold")
        print(f"  Recommended coefficient: {report['recommended_coefficient']:.4f}")
        print(f"  Replace 7.0 with {report['recommended_coefficient']:.4f} in fee.py")
    print()


def main() -> int:
    from datetime import datetime, timezone
    validation_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("Connecting to Polymarket CLOB...")
    try:
        client = get_clob_client()
    except Exception as e:
        print(f"ERROR: Could not initialize CLOB client: {e}", file=sys.stderr)
        print("Check that POLYMARKET_* env vars are set.", file=sys.stderr)
        return 2

    print("Fetching trade history...")
    fills = fetch_fills(client)

    if len(fills) < MIN_FILLS:
        print(
            f"ERROR: Only {len(fills)} fills with fee data found — "
            f"need at least {MIN_FILLS} for calibration. Aborting.",
            file=sys.stderr,
        )
        return 2

    print(f"Found {len(fills)} fills with fee data. Computing calibration report...")
    report = compute_calibration_report(fills)
    print_report(report, validation_date)

    return 0 if report["mae"] <= MAE_THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(main())
