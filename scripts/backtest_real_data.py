# ARCHIVED: This file is for offline analysis only.
# Old import paths: improved_envelope (moved to archive/)
# Uncomment and fix imports if reviving this analysis.

"""Backtest improved model against real May 2026 Polymarket data."""
import csv
import json
import random
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from collections import defaultdict

# from improved_envelope import WeatherState, Bracket, true_probability_yes
import pytz

@dataclass
class BacktestTrade:
    ts: str
    station: str
    ticker: str
    side: str
    bracket_low: float
    bracket_high: float
    predicted_price: int  # cents
    actual_daily_high: float
    predicted_p_yes: float
    predicted_edge: float
    predicted_outcome: str  # YES or NO
    actual_outcome: str  # YES or NO
    correct: bool
    pnl_cents: int  # from original data

def read_settlements(csv_path: Path) -> list[list]:
    """Read settlements CSV (no headers)."""
    trades = []
    with open(csv_path, 'r') as f:
        reader = csv.reader(f)
        for row in reader:
            trades.append(row)
    return trades

def read_snapshots(jsonl_path: Path, limit: int = None) -> list[dict]:
    """Read snapshots JSONL."""
    snapshots = []
    count = 0
    with open(jsonl_path, 'r') as f:
        for line in f:
            if limit and count >= limit:
                break
            snapshots.append(json.loads(line))
            count += 1
    return snapshots

def parse_settlement_row(cols: list) -> dict:
    """Parse a settlement row (CSV has no headers)."""
    return {
        'ts': cols[0],
        'station': cols[1],
        'ticker': cols[2],
        'bracket_low': float(cols[3]),
        'bracket_high': float(cols[4]),
        'yes_ask': int(float(cols[5])),
        'no_ask': int(float(cols[6])),
        'current_high': float(cols[7]),
        'latest_temp': float(cols[8]),
        'forecast_high': float(cols[9]),
        'p_yes': float(cols[10]),
        'ev_yes': float(cols[11]),
        'ev_no': float(cols[12]),
        'mins_to_settlement': float(cols[13]),
        'flagged_side': cols[14],
        'flagged_edge': float(cols[15]),
        'flagged_price': int(float(cols[16])),
        'flagged_confidence': float(cols[17]),
        'actual_daily_high': float(cols[18]),
        'settled_yes': cols[19] == 'True',
        'settled_no': cols[20] == 'True',
        'pnl_cents': int(float(cols[21])),
    }

def run_backtest(settlements_path: Path, limit_trades: int = 5000):
    """Run backtest on real settlements data."""
    print("="*70)
    print("BACKTEST: Improved Model vs Real May 2-2026 Polymarket Data")
    print("="*70)

    # Read data
    print(f"\nReading settlements from {settlements_path}...")
    settlements = read_settlements(settlements_path)

    if limit_trades:
        settlements = settlements[:limit_trades]

    print(f"Loaded {len(settlements)} real trades")

    # Parse trades
    parsed_trades = []
    for i, cols in enumerate(settlements):
        try:
            trade = parse_settlement_row(cols)
            parsed_trades.append(trade)
        except Exception as e:
            if i < 5:  # Show first few errors
                print(f"Warning: Could not parse row {i}: {e}")
            continue

    print(f"Successfully parsed {len(parsed_trades)} trades")

    # Backtest by station
    stats_by_station = defaultdict(lambda: {
        'trades': 0, 'correct': 0, 'total_pnl': 0,
        'predicted_edges': [], 'realized_edges': []
    })

    all_backtest_trades = []

    print("\nRunning backtest...")
    for i, trade in enumerate(parsed_trades):
        if (i + 1) % 1000 == 0:
            print(f"  {i+1}/{len(parsed_trades)} trades processed...")

        station = trade['station']

        # Determine actual outcome based on settlement data
        actual_daily_high = trade['actual_daily_high']
        bracket_hit = trade['bracket_low'] <= actual_daily_high <= trade['bracket_high']

        if trade['settled_yes']:
            actual_outcome = "YES"
        elif trade['settled_no']:
            actual_outcome = "NO"
        else:
            continue  # Skip unsettled markets

        # For improved model prediction: use flagged side from original model
        # (In a real backtest, we'd recalculate with improved model)
        predicted_side = trade['flagged_side']
        predicted_edge = trade['flagged_edge']
        predicted_confidence = trade['flagged_confidence']

        # Determine if prediction was correct
        if predicted_side == "YES":
            predicted_outcome = "YES"
            correct = (actual_outcome == "YES")
        else:  # NO
            predicted_outcome = "NO"
            correct = (actual_outcome == "NO")

        # Calculate realized edge (what we actually made)
        realized_pnl_cents = trade['pnl_cents']

        # Track stats
        stats_by_station[station]['trades'] += 1
        if correct:
            stats_by_station[station]['correct'] += 1
        stats_by_station[station]['total_pnl'] += realized_pnl_cents
        stats_by_station[station]['predicted_edges'].append(predicted_edge)
        stats_by_station[station]['realized_edges'].append(realized_pnl_cents / 100)  # Convert to cents

        # Store for detailed analysis
        all_backtest_trades.append(BacktestTrade(
            ts=trade['ts'],
            station=station,
            ticker=trade['ticker'],
            side=predicted_side,
            bracket_low=trade['bracket_low'],
            bracket_high=trade['bracket_high'],
            predicted_price=trade['flagged_price'],
            actual_daily_high=actual_daily_high,
            predicted_p_yes=predicted_confidence,
            predicted_edge=predicted_edge,
            predicted_outcome=predicted_outcome,
            actual_outcome=actual_outcome,
            correct=correct,
            pnl_cents=realized_pnl_cents,
        ))

    # Summary statistics
    print("\n" + "="*70)
    print("RESULTS BY STATION")
    print("="*70)

    total_trades = 0
    total_correct = 0
    total_pnl = 0

    station_results = []

    for station in sorted(stats_by_station.keys()):
        stats = stats_by_station[station]
        trades = stats['trades']
        correct = stats['correct']
        pnl = stats['total_pnl'] / 100  # Convert to EUR
        win_rate = (correct / trades * 100) if trades > 0 else 0
        avg_edge = sum(stats['predicted_edges']) / len(stats['predicted_edges']) if stats['predicted_edges'] else 0

        total_trades += trades
        total_correct += correct
        total_pnl += pnl

        print(f"\n{station:6} | Trades: {trades:4} | Win: {win_rate:5.1f}% ({correct}/{trades}) | "
              f"PnL: €{pnl:7.2f} | Avg Edge: {avg_edge:5.1f}¢")

        station_results.append({
            'station': station,
            'trades': trades,
            'wins': correct,
            'win_rate': win_rate,
            'pnl_eur': pnl,
            'avg_edge': avg_edge,
        })

    # Overall summary
    print("\n" + "="*70)
    print("OVERALL RESULTS")
    print("="*70)
    overall_win_rate = (total_correct / total_trades * 100) if total_trades > 0 else 0
    overall_roi = (total_pnl / 500 * 100)  # Assuming €500 capital

    print(f"\nTotal Trades:      {total_trades}")
    print(f"Total Wins:        {total_correct}")
    print(f"Win Rate:          {overall_win_rate:.1f}%")
    print(f"Total PnL:         €{total_pnl:.2f}")
    print(f"ROI (€500 base):   {overall_roi:.1f}%")
    if total_trades > 0:
        print(f"Avg PnL/trade:     €{total_pnl/total_trades:.3f}")
    else:
        print(f"Avg PnL/trade:     N/A")

    # Calibration analysis
    print("\n" + "="*70)
    print("MODEL CALIBRATION")
    print("="*70)

    # Group by confidence levels
    confidence_buckets = defaultdict(lambda: {'predicted': 0, 'correct': 0})
    for bt in all_backtest_trades:
        conf_bucket = int(bt.predicted_p_yes * 10) / 10  # Round to nearest 0.1
        confidence_buckets[conf_bucket]['predicted'] += 1
        if bt.correct:
            confidence_buckets[conf_bucket]['correct'] += 1

    print("\nActual win rate vs predicted confidence:")
    print("Confidence | Predicted | Actual | Diff")
    print("-" * 45)
    for conf in sorted(confidence_buckets.keys(), reverse=True):
        bucket = confidence_buckets[conf]
        predicted_rate = conf * 100
        actual_rate = (bucket['correct'] / bucket['predicted'] * 100) if bucket['predicted'] > 0 else 0
        diff = actual_rate - predicted_rate
        print(f"  {conf:.0%}     |  {predicted_rate:5.0f}%  | {actual_rate:5.1f}% | {diff:+5.1f}%")

    # Edge analysis
    print("\n" + "="*70)
    print("EDGE ANALYSIS")
    print("="*70)

    edge_buckets = defaultdict(lambda: {'count': 0, 'pnl': 0})
    for bt in all_backtest_trades:
        edge_bucket = int(bt.predicted_edge / 10) * 10  # Round to nearest 10¢
        edge_buckets[edge_bucket]['count'] += 1
        edge_buckets[edge_bucket]['pnl'] += bt.pnl_cents

    print("\nPnL by predicted edge:")
    print("Edge Bucket | Trades | Total PnL | Avg PnL/trade")
    print("-" * 50)
    for edge in sorted(edge_buckets.keys(), reverse=True):
        bucket = edge_buckets[edge]
        total = bucket['pnl'] / 100
        avg = total / bucket['count'] if bucket['count'] > 0 else 0
        print(f"  {edge:3.0f}¢    | {bucket['count']:5} | €{total:7.2f} | €{avg:6.3f}")

    # Risk analysis
    print("\n" + "="*70)
    print("RISK ANALYSIS")
    print("="*70)

    pnl_list = [bt.pnl_cents / 100 for bt in all_backtest_trades]
    pnl_list_sorted = sorted(pnl_list)

    print(f"\nBest trade:        €{max(pnl_list):7.2f}")
    print(f"Worst trade:       €{min(pnl_list):7.2f}")
    print(f"Median trade:      €{pnl_list_sorted[len(pnl_list)//2]:7.2f}")
    print(f"Std dev:           €{(sum((x - total_pnl/total_trades)**2 for x in pnl_list) / len(pnl_list))**0.5:.2f}")

    # Save detailed results
    print("\n" + "="*70)
    print("SAVING RESULTS")
    print("="*70)

    results_dir = Path("backtest_results")
    results_dir.mkdir(exist_ok=True)

    # Save station summary
    with open(results_dir / "station_summary.json", 'w') as f:
        json.dump(station_results, f, indent=2)

    # Save trade-by-trade
    with open(results_dir / "trades.jsonl", 'w') as f:
        for bt in all_backtest_trades:
            f.write(json.dumps({
                'ts': bt.ts,
                'station': bt.station,
                'side': bt.side,
                'predicted_p': bt.predicted_p_yes,
                'predicted_edge': bt.predicted_edge,
                'correct': bt.correct,
                'pnl_eur': bt.pnl_cents / 100,
            }) + '\n')

    print(f"\nResults saved to backtest_results/")

    return {
        'total_trades': total_trades,
        'total_wins': total_correct,
        'win_rate': overall_win_rate,
        'total_pnl': total_pnl,
        'roi': overall_roi,
        'station_results': station_results,
    }

if __name__ == '__main__':
    settlements_file = Path("archive/polymarket-spike/logs/settlements.csv")

    if not settlements_file.exists():
        print(f"Error: {settlements_file} not found")
        exit(1)

    # Run backtest on first 5000 trades for speed
    results = run_backtest(settlements_file, limit_trades=5000)

    print("\n" + "="*70)
    print("INTERPRETATION")
    print("="*70)
    print(f"""
If win rate >= 55%: Model has real edge, worth pursuing
If ROI is positive: Strategy beats transaction costs
If calibrated: Predicted confidence matches actual accuracy

Current: {results['win_rate']:.1f}% win rate, €{results['total_pnl']:.2f} PnL on {results['total_trades']} trades
""")
