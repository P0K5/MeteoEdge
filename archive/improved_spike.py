"""Improved spike detector with paper trading simulation."""
import sys
from datetime import datetime, timezone
from pathlib import Path
import pytz
from astral import LocationInfo
from astral.sun import sun

from http_client import fetch, get_nws_forecast_url, cached_fetch_json
from improved_envelope import WeatherState, Bracket, true_probability_yes, fetch_secondary_forecast
from paper_trader import PaperTrader

# Config
STATIONS = [
    ("KLGA", 40.7790, -73.8740, "New York City", "KLGA"),
    ("KORD", 41.9742, -87.9073, "Chicago", "KORD"),
    ("KMIA", 25.7953, -80.2901, "Miami", "KMIA"),
    ("KAUS", 30.1944, -97.6700, "Austin", "KAUS"),
    ("KLAX", 33.9425, -118.4081, "Los Angeles", "KLAX"),
    ("KDAL", 32.8470, -96.8517, "Dallas", "KDAL"),
    ("KATL", 33.6367, -84.4281, "Atlanta", "KATL"),
    ("KBKF", 39.7017, -104.7517, "Denver", "KBKF"),
    ("KHOU", 29.6454, -95.2789, "Houston", "KHOU"),
    ("KSFO", 37.6213, -122.3790, "San Francisco", "KSFO"),
    ("KSEA", 47.4502, -122.3088, "Seattle", "KSEA"),
]

STATION_TZ = {
    s[0]: {
        "KLGA": "America/New_York", "KORD": "America/Chicago",
        "KMIA": "America/New_York", "KAUS": "America/Chicago",
        "KLAX": "America/Los_Angeles", "KDAL": "America/Chicago",
        "KATL": "America/New_York", "KBKF": "America/Denver",
        "KHOU": "America/Chicago", "KSFO": "America/Los_Angeles",
        "KSEA": "America/Los_Angeles",
    }[s[0]] for s in STATIONS
}

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_WEATHER_TAG_ID = "84"
MIN_EDGE_CENTS = 3.0
MIN_CONFIDENCE = 0.80
MAX_CONFIDENCE_NO = 0.20

def fetch_metar(station: str) -> dict | None:
    """Fetch latest METAR observation.

    METAR data is real-time (updated every ~20 min) so we do NOT cache it,
    but the request still goes through the shared rate-limiter and retry logic.
    """
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json&hours=2"
        r = fetch(url, timeout=10)
        data = r.json()
        return data[0] if data else None
    except Exception as e:
        print(f"[metar] {station} error: {e}")
        return None

def extract_temp_from_metar(metar: dict) -> tuple[float, datetime] | None:
    """Extract temperature from METAR."""
    try:
        temp_c = metar.get('temp')
        if temp_c is None:
            return None
        temp_f = temp_c * 9/5 + 32
        obs_time = metar.get('obsTime')
        return temp_f, datetime.fromisoformat(obs_time.replace('Z', '+00:00')) if obs_time else datetime.now(timezone.utc)
    except:
        return None

def fetch_nws_forecast(lat: float, lon: float) -> float | None:
    """Fetch NWS forecast high.

    Two-tier caching:
    - /points URL is cached permanently (static per coordinate).
    - Hourly forecast payload is cached 30 min (NWS updates ~hourly).
    """
    try:
        forecast_url = get_nws_forecast_url(lat, lon)
        if not forecast_url:
            return None
        data = cached_fetch_json(forecast_url, ttl_minutes=30)
        if not data:
            return None
        periods = data["properties"]["periods"]
        highs = [p["temperature"] for p in periods[:18] if p.get("temperatureUnit") == "F"]
        return max(highs) if highs else None
    except Exception as e:
        print(f"[nws] ({lat},{lon}) error: {e}")
        return None

def get_weather_state(station_code: str, lat: float, lon: float) -> WeatherState | None:
    """Fetch all weather data for a station."""
    metar = fetch_metar(station_code)
    if not metar:
        return None

    temp_data = extract_temp_from_metar(metar)
    if not temp_data:
        return None

    latest_temp_f, latest_time = temp_data
    local_tz = pytz.timezone(STATION_TZ[station_code])
    now_local = datetime.now(local_tz)

    # Sunset for end-of-day reference
    loc = LocationInfo(station_code, "US", STATION_TZ[station_code], lat, lon)
    s = sun(loc.observer, date=now_local.date(), tzinfo=local_tz)
    sunset = s["sunset"]

    # Get current high from METAR
    current_high = metar.get('maxTemp')
    if current_high is not None:
        current_high = current_high * 9/5 + 32
    else:
        current_high = latest_temp_f  # Fallback

    # Forecasts
    nws_forecast = fetch_nws_forecast(lat, lon)
    secondary_forecast = fetch_secondary_forecast(lat, lon)

    return WeatherState(
        station=station_code,
        now_local=now_local,
        sunset_local=sunset,
        current_high_f=current_high,
        current_high_time=latest_time,
        latest_temp_f=latest_temp_f,
        latest_temp_time=latest_time,
        forecast_high_f=nws_forecast,
        secondary_forecast_f=secondary_forecast,
    )

def get_weather_markets() -> list[dict]:
    """Fetch weather-tagged markets from Polymarket.

    Cached for 5 minutes — market open/close events happen infrequently
    within a single session and do not need sub-minute freshness.
    """
    url = f"{POLYMARKET_GAMMA_API}/markets?tag_id={POLYMARKET_WEATHER_TAG_ID}&limit=1000"
    data = cached_fetch_json(url, ttl_minutes=5)
    if data is None:
        print("[polymarket] failed to fetch markets (returned None)")
        return []
    return data if isinstance(data, list) else []

def parse_bracket(market: dict) -> Bracket | None:
    """Parse a weather bracket from Polymarket market data."""
    try:
        # Simplified parsing - extract from question text
        question = market.get('question', '')
        ticker = market.get('conditionId', '')

        # Try to extract bracket from outcomes
        outcomes = market.get('outcomes', [])
        if len(outcomes) < 2:
            return None

        # Assuming binary outcome structure
        yes_outcome = outcomes[0]
        no_outcome = outcomes[1]

        # Get prices from orderbook
        prices = market.get('outcomePrices', [0.0, 1.0])
        yes_price = int(prices[0] * 100)
        no_price = int(prices[1] * 100)

        # Extract bracket from question (simple heuristic)
        # Question format: "Will the high in [city] be between X and Y°F?"
        import re
        match = re.search(r'between (\d+) and (\d+)', question)
        if match:
            low = float(match.group(1))
            high = float(match.group(2))

            return Bracket(
                ticker=ticker,
                low_f=low,
                high_f=high,
                yes_ask_cents=yes_price,
                yes_ask_size=100,
                no_ask_cents=no_price,
                no_ask_size=100,
            )
    except:
        pass
    return None

def run_polling_cycle(trader: PaperTrader, log_file: Path):
    """Run one poll cycle: fetch data, identify opportunities, execute simulated trades."""
    ts = datetime.now(timezone.utc)
    print(f"\n=== Poll at {ts.isoformat()} ===")

    # Fetch weather for all stations
    weather = {}
    for station_code, lat, lon, city, *_ in STATIONS:
        state = get_weather_state(station_code, lat, lon)
        if state:
            weather[station_code] = state
            print(f"[{station_code}] temp={state.latest_temp_f:.1f}°F forecast={state.forecast_high_f}")

    # Fetch markets
    markets = get_weather_markets()
    print(f"[polymarket] {len(markets)} weather markets fetched")

    # Scan and trade
    flagged_count = 0
    for market in markets:
        bracket = parse_bracket(market)
        if not bracket:
            continue

        # Match to station
        for station_code in weather.keys():
            state = weather[station_code]

            # Minutes to market resolution
            mins_left = 24 * 60  # Assume 24h markets for now

            # Calculate probability with improved model
            p_yes = true_probability_yes(bracket, state, mins_left)

            # Edge calculation
            ev_yes = p_yes * 100 - bracket.yes_ask_cents - 1  # 1¢ fee
            ev_no = (1 - p_yes) * 100 - bracket.no_ask_cents - 1

            # Flag opportunities
            if ev_yes >= MIN_EDGE_CENTS and p_yes >= MIN_CONFIDENCE:
                trade = trader.execute_trade(
                    station=station_code,
                    ticker=bracket.ticker,
                    bracket_low=bracket.low_f,
                    bracket_high=bracket.high_f,
                    side="YES",
                    predicted_price=bracket.yes_ask_cents,
                    predicted_edge=ev_yes,
                    actual_daily_high=state.latest_temp_f,  # Placeholder
                    minutes_to_settlement=mins_left,
                )
                if trade:
                    flagged_count += 1
                    print(f"  ** TRADE {bracket.ticker[:16]}… YES @ {bracket.yes_ask_cents}¢ slippage={trade.slippage}¢ pnl={trade.pnl/100:.2f}€")

            elif ev_no >= MIN_EDGE_CENTS and p_yes <= MAX_CONFIDENCE_NO:
                trade = trader.execute_trade(
                    station=station_code,
                    ticker=bracket.ticker,
                    bracket_low=bracket.low_f,
                    bracket_high=bracket.high_f,
                    side="NO",
                    predicted_price=bracket.no_ask_cents,
                    predicted_edge=ev_no,
                    actual_daily_high=state.latest_temp_f,
                    minutes_to_settlement=mins_left,
                )
                if trade:
                    flagged_count += 1
                    print(f"  ** TRADE {bracket.ticker[:16]}… NO @ {bracket.no_ask_cents}¢ slippage={trade.slippage}¢ pnl={trade.pnl/100:.2f}€")

    print(f"[traded] {flagged_count} opportunities flagged")
    trader.log_summary(ts)

if __name__ == '__main__':
    print("MeteoEdge Improved Spike - Paper Trading Simulation")
    print("Starting capital: €500.00, position size: €5.00 per trade")

    trader = PaperTrader(starting_capital_eur=500.0, position_size_eur=5.0)
    log_file = Path("paper_trading_logs/simulation.log")

    # Run for ~48 hours with polls every 5 minutes
    import time as time_module
    poll_count = 0
    while poll_count < 576:  # 5 min * 576 = 48 hours
        try:
            run_polling_cycle(trader, log_file)
            poll_count += 1
            print(f"Poll {poll_count}/576 complete. Capital: €{trader.capital:.2f}")
            time_module.sleep(5 * 60)  # 5 minute poll interval
        except KeyboardInterrupt:
            print("\nSimulation interrupted by user.")
            break
        except Exception as e:
            print(f"Error in poll {poll_count}: {e}")
            time_module.sleep(5 * 60)

    # Final report
    report = trader.daily_report()
    print(f"\n=== Final Report ===")
    print(f"Total trades: {report['trades']}")
    print(f"Final capital: €{report['capital']:.2f}")
    print(f"Total PnL: €{report['pnl_eur']:.2f}")
    print(f"ROI: {report['roi_pct']:.1f}%")
    print(f"Win rate: {report['win_rate_pct']:.1f}%")
    print(f"Avg slippage: {report['avg_slippage_cents']:.2f}¢")

    # Save trades
    trader.save_trades(Path("paper_trading_logs/trades.jsonl"))
