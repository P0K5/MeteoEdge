"""Paper trading engine with realistic slippage, queue delays, and capital tracking."""
import json
import random
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

@dataclass
class Trade:
    ts: str
    station: str
    ticker: str
    bracket_low: float
    bracket_high: float
    actual_daily_high: float
    side: str  # YES or NO
    predicted_price: int  # cents
    actual_price: int  # cents with slippage
    slippage: int  # cents
    queue_delay_ms: int
    predicted_edge: float  # cents
    win: bool
    pnl: int  # cents
    position_size_eur: float  # euros per trade
    capital_before: float
    capital_after: float

class PaperTrader:
    def __init__(self, db=None, starting_capital_eur: float = 500.0, position_size_eur: float = 5.0):
        self._db = db
        self.starting_capital = starting_capital_eur
        self.position_size = position_size_eur
        self.capital = starting_capital_eur
        self.trades: list[Trade] = []
        self.log_dir = Path("paper_trading_logs")
        self.log_dir.mkdir(exist_ok=True)
        if db is not None:
            rows = db.get_trades(limit=1, mode="paper")
            if rows and rows[0].get("capital_after") is not None:
                self.capital = float(rows[0]["capital_after"])

    def realistic_slippage(self) -> int:
        """Random slippage model: 0.5 to 3 cents with distribution favoring smaller slips."""
        slip = max(0, random.gauss(1.5, 0.8))
        slip = min(3.0, slip)
        return int(slip * 100) // 100

    def queue_delay(self) -> int:
        """Simulate realistic order queue delay: 50-500ms."""
        delay = max(50, int(random.gauss(150, 80)))
        return min(500, delay)

    def execute_trade(self, station: str, ticker: str, bracket_low: float, bracket_high: float,
                      side: str, predicted_price: int, predicted_edge: float,
                      actual_daily_high: float, minutes_to_settlement: float):
        capital_needed = self.position_size
        if self.capital < capital_needed:
            return None

        delay_ms = self.queue_delay()
        time.sleep(delay_ms / 1000.0)

        slippage = self.realistic_slippage()
        actual_price = predicted_price + slippage
        actual_price = max(1, min(99, actual_price))

        if side == "YES":
            win = bracket_low <= actual_daily_high <= bracket_high
            payout = 100 if win else 0
        else:
            win = not (bracket_low <= actual_daily_high <= bracket_high)
            payout = 100 if win else 0

        cost_eur = self.position_size * (actual_price / 100)
        revenue_eur = self.position_size * (payout / 100)
        pnl_eur = revenue_eur - cost_eur
        pnl_cents = int(pnl_eur * 100)

        capital_before = self.capital
        self.capital += pnl_eur

        trade = Trade(
            ts=datetime.utcnow().isoformat(),
            station=station,
            ticker=ticker[:16],
            bracket_low=bracket_low,
            bracket_high=bracket_high,
            actual_daily_high=actual_daily_high,
            side=side,
            predicted_price=predicted_price,
            actual_price=actual_price,
            slippage=slippage,
            queue_delay_ms=delay_ms,
            predicted_edge=predicted_edge,
            win=win,
            pnl=pnl_cents,
            position_size_eur=self.position_size,
            capital_before=capital_before,
            capital_after=self.capital,
        )

        if self._db is not None:
            try:
                self._db.insert_trade(
                    ts=trade.ts,
                    station=station,
                    ticker=ticker[:16],
                    bracket_low=bracket_low,
                    bracket_high=bracket_high,
                    side=side,
                    predicted_price=predicted_price,
                    actual_price=actual_price,
                    slippage=slippage,
                    predicted_edge=predicted_edge,
                    mode="paper",
                    capital_before=capital_before,
                    outcome="filled" if win else "expired",
                    pnl=pnl_eur,
                    capital_after=trade.capital_after,
                )
            except Exception as e:
                print(f"[paper] DB write failed: {e}")

        self.trades.append(trade)
        return trade

    def daily_report(self) -> dict:
        if not self.trades:
            return {"trades": 0, "pnl_eur": 0.0, "roi_pct": 0.0}

        total_pnl = sum(t.pnl for t in self.trades) / 100
        win_count = sum(1 for t in self.trades if t.win)
        total_slippage = sum(t.slippage for t in self.trades)
        avg_slippage = total_slippage / len(self.trades) if self.trades else 0

        return {
            "trades": len(self.trades),
            "pnl_eur": round(total_pnl, 2),
            "capital": round(self.capital, 2),
            "roi_pct": round((total_pnl / self.starting_capital) * 100, 2),
            "win_rate_pct": round((win_count / len(self.trades)) * 100, 1),
            "avg_slippage_cents": round(avg_slippage, 2),
        }

    def save_trades(self, filepath: Path):
        """Save all trades to JSONL (backward compat for non-DB mode)."""
        with open(filepath, "w") as f:
            for trade in self.trades:
                f.write(json.dumps(asdict(trade)) + "\n")

    def log_summary(self, timestamp: datetime):
        """Log hourly summary."""
        report = self.daily_report()
        summary = {
            "ts": timestamp.isoformat(),
            **report
        }
        summary_path = self.log_dir / "summary.jsonl"
        with open(summary_path, "a") as f:
            f.write(json.dumps(summary) + "\n")
        print(f"[summary] {timestamp.isoformat()}: {report['trades']} trades, "
              f"EUR{report['pnl_eur']:.2f} PnL, capital: EUR{report['capital']:.2f}")
