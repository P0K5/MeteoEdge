# ARCHIVED: This file is for offline testing only.
# Old import paths: improved_envelope, paper_trader (no longer in src/)
# Uncomment and fix imports if reviving this simulation.

"""Test simulation - simplified version to verify everything works."""
from datetime import datetime, timedelta, timezone
import random
import pytz

# from improved_envelope import WeatherState, Bracket, true_probability_yes, fetch_secondary_forecast
# from paper_trader import PaperTrader

# Test configuration
STATIONS_TEST = [
    ("KLGA", 40.7790, -73.8740),
    ("KMIA", 25.7953, -80.2901),
    ("KLAX", 33.9425, -118.4081),
]

def run_test_simulation():
    """Run a simplified test with synthetic weather data."""
    print("MeteoEdge Improved Spike - TEST SIMULATION")
    print("=" * 50)
    print("Starting capital: €500.00, position size: €5.00 per trade")
    print()

    trader = PaperTrader(starting_capital_eur=500.0, position_size_eur=5.0)

    # Generate synthetic trading opportunities
    for poll_num in range(24):  # Run 24 polls (2 hours, 5-min intervals)
        ts = datetime.now(timezone.utc) - timedelta(hours=2) + timedelta(minutes=5*poll_num)
        print(f"\n=== Poll {poll_num+1}/24 at {ts.isoformat()[:19]}Z ===")

        # Create synthetic weather states
        for station_code, lat, lon in STATIONS_TEST:
            # Simulate weather progression through the day
            hour = (ts.hour + 12) % 24  # Offset for testing
            base_temp = 50 + hour * 1.5  # Temperature rises through day
            latest_temp = base_temp + random.uniform(-2, 2)
            forecast_high = base_temp + 15 + random.uniform(-3, 3)
            current_high = min(latest_temp, forecast_high * 0.7)

            state = WeatherState(
                station=station_code,
                now_local=ts.astimezone(pytz.UTC),
                sunset_local=ts + timedelta(hours=4),
                current_high_f=current_high,
                current_high_time=ts,
                latest_temp_f=latest_temp,
                latest_temp_time=ts,
                forecast_high_f=forecast_high,
                secondary_forecast_f=forecast_high + random.uniform(-1, 1),
            )

            # Generate 3-5 synthetic brackets per station
            num_brackets = random.randint(3, 5)
            for b in range(num_brackets):
                bracket_low = 60 + b * 5
                bracket_high = bracket_low + 4

                bracket = Bracket(
                    ticker=f"0x{random.randbytes(20).hex()[:16]}",
                    low_f=float(bracket_low),
                    high_f=float(bracket_high),
                    yes_ask_cents=random.randint(20, 60),
                    yes_ask_size=100,
                    no_ask_cents=random.randint(40, 80),
                    no_ask_size=100,
                )

                # Calculate probability with improved model
                mins_to_settlement = 24 * 60 - poll_num * 5
                p_yes = true_probability_yes(bracket, state, mins_to_settlement)

                # Fee estimate
                fee = max(1.0, 7.0 * (bracket.yes_ask_cents / 100) * (1 - bracket.yes_ask_cents / 100))

                # Edge calculation
                ev_yes = p_yes * 100 - bracket.yes_ask_cents - fee
                ev_no = (1 - p_yes) * 100 - bracket.no_ask_cents - fee

                # Trade if edge is good
                min_edge = 5.0  # Lower threshold for testing
                if ev_yes >= min_edge and p_yes >= 0.75:
                    # Determine outcome: use current forecast as proxy for final
                    actual_high = forecast_high + random.uniform(-3, 3)

                    trade = trader.execute_trade(
                        station=station_code,
                        ticker=bracket.ticker,
                        bracket_low=bracket.low_f,
                        bracket_high=bracket.high_f,
                        side="YES",
                        predicted_price=bracket.yes_ask_cents,
                        predicted_edge=ev_yes,
                        actual_daily_high=actual_high,
                        minutes_to_settlement=mins_to_settlement,
                    )
                    if trade:
                        status = "WIN" if trade.win else "LOSS"
                        print(f"  YES  {bracket.ticker[:8]}  "
                              f"edge={trade.predicted_edge:.1f}¢ "
                              f"slip={trade.slippage}¢ "
                              f"pnl={trade.pnl/100:+.2f}€ [{status}]")

                elif ev_no >= min_edge and p_yes <= 0.25:
                    actual_high = forecast_high + random.uniform(-3, 3)

                    trade = trader.execute_trade(
                        station=station_code,
                        ticker=bracket.ticker,
                        bracket_low=bracket.low_f,
                        bracket_high=bracket.high_f,
                        side="NO",
                        predicted_price=bracket.no_ask_cents,
                        predicted_edge=ev_no,
                        actual_daily_high=actual_high,
                        minutes_to_settlement=mins_to_settlement,
                    )
                    if trade:
                        status = "WIN" if trade.win else "LOSS"
                        print(f"  NO   {bracket.ticker[:8]}  "
                              f"edge={trade.predicted_edge:.1f}¢ "
                              f"slip={trade.slippage}¢ "
                              f"pnl={trade.pnl/100:+.2f}€ [{status}]")

        # Log summary
        report = trader.daily_report()
        print(f"  [summary] trades={report['trades']} pnl=€{report['pnl_eur']:.2f} "
              f"capital=€{report['capital']:.2f} roi={report['roi_pct']:.1f}% "
              f"winrate={report['win_rate_pct']:.0f}%")

    # Final report
    print("\n" + "=" * 50)
    print("FINAL REPORT")
    print("=" * 50)
    report = trader.daily_report()
    print(f"Total trades:      {report['trades']}")
    print(f"Starting capital:  €{trader.starting_capital:.2f}")
    print(f"Final capital:     €{report['capital']:.2f}")
    print(f"Total PnL:         €{report['pnl_eur']:.2f}")
    print(f"ROI:               {report['roi_pct']:.1f}%")
    print(f"Win rate:          {report['win_rate_pct']:.1f}%")
    print(f"Avg slippage:      {report['avg_slippage_cents']:.2f}¢")
    print()

    # Save trades
    from pathlib import Path
    trader.save_trades(Path("paper_trading_logs/trades.jsonl"))
    print(f"Trades saved to: paper_trading_logs/trades.jsonl")

if __name__ == '__main__':
    run_test_simulation()
