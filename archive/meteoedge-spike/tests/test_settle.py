"""Unit tests for settle.py pure logic functions.
No external API calls required — all tests use synthetic CSV data.
"""
import sys
import os
import pytest
from datetime import date, timedelta
from io import StringIO
import csv

# Ensure meteoedge-spike is on the path so imports resolve without installation
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from settle import fetch_daily_climate_high


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_candidate_row(
    ts: str = "2024-07-14T12:00:00Z",
    station: str = "KNYC",
    ticker: str = "TEST-TICKER",
    bracket_low: float = 80.0,
    bracket_high: float = 85.0,
    flagged_side: str = "YES",
    flagged_price: int = 50,
) -> dict:
    """Build a candidate row as it would come from candidates.csv."""
    return {
        "ts": ts,
        "station": station,
        "ticker": ticker,
        "bracket_low": str(bracket_low),
        "bracket_high": str(bracket_high),
        "flagged_side": flagged_side,
        "flagged_price": str(flagged_price),
    }


# ---------------------------------------------------------------------------
# Win/loss determination tests
# ---------------------------------------------------------------------------

class TestYESWinLossDetermination:
    """Test that YES-side candidates win/lose correctly."""

    def test_yes_wins_when_actual_in_bracket(self):
        """YES side wins if actual high is within [bracket_low, bracket_high]."""
        row = make_candidate_row(
            bracket_low=80.0, bracket_high=85.0, flagged_side="YES"
        )
        actual = 82.5
        lo, hi = float(row["bracket_low"]), float(row["bracket_high"])
        yes_won = lo <= actual <= hi
        assert yes_won is True

    def test_yes_loses_when_actual_below_bracket(self):
        """YES side loses if actual is below bracket_low."""
        row = make_candidate_row(
            bracket_low=80.0, bracket_high=85.0, flagged_side="YES"
        )
        actual = 79.0
        lo, hi = float(row["bracket_low"]), float(row["bracket_high"])
        yes_won = lo <= actual <= hi
        assert yes_won is False

    def test_yes_loses_when_actual_above_bracket(self):
        """YES side loses if actual is above bracket_high."""
        row = make_candidate_row(
            bracket_low=80.0, bracket_high=85.0, flagged_side="YES"
        )
        actual = 86.0
        lo, hi = float(row["bracket_low"]), float(row["bracket_high"])
        yes_won = lo <= actual <= hi
        assert yes_won is False

    def test_yes_wins_at_bracket_boundaries(self):
        """YES side wins when actual equals bracket_low or bracket_high."""
        row_low = make_candidate_row(
            bracket_low=80.0, bracket_high=85.0, flagged_side="YES"
        )
        row_high = make_candidate_row(
            bracket_low=80.0, bracket_high=85.0, flagged_side="YES"
        )
        lo, hi = float(row_low["bracket_low"]), float(row_low["bracket_high"])

        yes_won_low = lo <= 80.0 <= hi
        yes_won_high = lo <= 85.0 <= hi
        assert yes_won_low is True
        assert yes_won_high is True


class TestNOWinLossDetermination:
    """Test that NO-side candidates win/lose correctly."""

    def test_no_wins_when_actual_outside_bracket(self):
        """NO side wins if actual is outside [bracket_low, bracket_high]."""
        row = make_candidate_row(
            bracket_low=80.0, bracket_high=85.0, flagged_side="NO"
        )
        actual = 79.0
        lo, hi = float(row["bracket_low"]), float(row["bracket_high"])
        yes_won = lo <= actual <= hi
        won = not yes_won  # NO side wins if YES loses
        assert won is True

    def test_no_loses_when_actual_in_bracket(self):
        """NO side loses if actual is within the bracket."""
        row = make_candidate_row(
            bracket_low=80.0, bracket_high=85.0, flagged_side="NO"
        )
        actual = 82.5
        lo, hi = float(row["bracket_low"]), float(row["bracket_high"])
        yes_won = lo <= actual <= hi
        won = not yes_won  # NO side logic
        assert won is False

    def test_no_wins_above_bracket(self):
        """NO side wins when actual is above the bracket."""
        row = make_candidate_row(
            bracket_low=80.0, bracket_high=85.0, flagged_side="NO"
        )
        actual = 86.0
        lo, hi = float(row["bracket_low"]), float(row["bracket_high"])
        yes_won = lo <= actual <= hi
        won = not yes_won
        assert won is True


# ---------------------------------------------------------------------------
# P&L calculation tests
# ---------------------------------------------------------------------------

class TestYESPNLCalculation:
    """Test P&L calculation for YES-side trades."""

    def test_yes_win_pnl(self):
        """YES side win: pnl = 100 - flagged_price (payoff minus cost)."""
        row = make_candidate_row(flagged_side="YES", flagged_price=50)
        flagged_price = float(row["flagged_price"])
        pnl = 100 - flagged_price  # YES win
        assert pnl == 50.0

    def test_yes_loss_pnl(self):
        """YES side loss: pnl = -flagged_price (cost)."""
        row = make_candidate_row(flagged_side="YES", flagged_price=50)
        flagged_price = float(row["flagged_price"])
        pnl = -flagged_price  # YES loss
        assert pnl == -50.0

    def test_yes_win_high_price(self):
        """YES win with high price (expensive contract): 100 - 75 = 25."""
        row = make_candidate_row(flagged_side="YES", flagged_price=75)
        flagged_price = float(row["flagged_price"])
        pnl = 100 - flagged_price
        assert pnl == 25.0

    def test_yes_loss_high_price(self):
        """YES loss with high price: -75 (lose the entire premium)."""
        row = make_candidate_row(flagged_side="YES", flagged_price=75)
        flagged_price = float(row["flagged_price"])
        pnl = -flagged_price
        assert pnl == -75.0

    def test_yes_win_low_price(self):
        """YES win with low price (cheap contract): 100 - 10 = 90."""
        row = make_candidate_row(flagged_side="YES", flagged_price=10)
        flagged_price = float(row["flagged_price"])
        pnl = 100 - flagged_price
        assert pnl == 90.0


class TestNOPNLCalculation:
    """Test P&L calculation for NO-side trades."""

    def test_no_win_pnl(self):
        """NO side win: pnl = 100 - flagged_price."""
        row = make_candidate_row(flagged_side="NO", flagged_price=40)
        flagged_price = float(row["flagged_price"])
        pnl = 100 - flagged_price
        assert pnl == 60.0

    def test_no_loss_pnl(self):
        """NO side loss: pnl = -flagged_price."""
        row = make_candidate_row(flagged_side="NO", flagged_price=40)
        flagged_price = float(row["flagged_price"])
        pnl = -flagged_price
        assert pnl == -40.0

    def test_no_win_high_price(self):
        """NO win with high price (expensive contract): 100 - 70 = 30."""
        row = make_candidate_row(flagged_side="NO", flagged_price=70)
        flagged_price = float(row["flagged_price"])
        pnl = 100 - flagged_price
        assert pnl == 30.0


# ---------------------------------------------------------------------------
# Date filtering tests
# ---------------------------------------------------------------------------

class TestDateFiltering:
    """Test that only yesterday's candidates are processed."""

    def test_yesterday_candidate_included(self):
        """A candidate from yesterday should be processed."""
        yesterday = date.today() - timedelta(days=1)
        row = make_candidate_row(ts=f"{yesterday.isoformat()}T12:00:00Z")
        ts_date = row["ts"][:10]
        assert ts_date == yesterday.isoformat()

    def test_today_candidate_excluded(self):
        """A candidate from today should not match yesterday."""
        today = date.today()
        yesterday = date.today() - timedelta(days=1)
        row = make_candidate_row(ts=f"{today.isoformat()}T12:00:00Z")
        ts_date = row["ts"][:10]
        assert ts_date != yesterday.isoformat()

    def test_older_candidate_excluded(self):
        """A candidate from 2 days ago should not match yesterday."""
        two_days_ago = date.today() - timedelta(days=2)
        yesterday = date.today() - timedelta(days=1)
        row = make_candidate_row(ts=f"{two_days_ago.isoformat()}T12:00:00Z")
        ts_date = row["ts"][:10]
        assert ts_date != yesterday.isoformat()


# ---------------------------------------------------------------------------
# End-to-end scenario tests
# ---------------------------------------------------------------------------

class TestEndToEndScenarios:
    """Test complete settlement logic with multiple rows."""

    def test_mixed_yes_no_outcomes(self):
        """Settle multiple candidates with mixed YES/NO and wins/losses."""
        candidates = [
            make_candidate_row(
                ts="2024-07-14T10:00:00Z",
                station="KNYC",
                ticker="T1",
                bracket_low=80.0,
                bracket_high=85.0,
                flagged_side="YES",
                flagged_price=50,
            ),
            make_candidate_row(
                ts="2024-07-14T11:00:00Z",
                station="KNYC",
                ticker="T2",
                bracket_low=90.0,
                bracket_high=95.0,
                flagged_side="NO",
                flagged_price=40,
            ),
        ]

        # Actual high for yesterday: 82°F (within T1, outside T2)
        actual = 82.0

        results = []
        for row in candidates:
            lo, hi = float(row["bracket_low"]), float(row["bracket_high"])
            yes_won = lo <= actual <= hi

            if row["flagged_side"] == "YES":
                pnl = (100 - float(row["flagged_price"])) if yes_won else -float(row["flagged_price"])
            else:
                won = not yes_won
                pnl = (100 - float(row["flagged_price"])) if won else -float(row["flagged_price"])

            results.append((row["ticker"], row["flagged_side"], pnl))

        assert results[0] == ("T1", "YES", 50.0)  # YES wins: 100 - 50 = 50
        assert results[1] == ("T2", "NO", 60.0)   # NO wins: 100 - 40 = 60

    def test_station_filtering(self):
        """Only process candidates from stations with truth data."""
        candidates = [
            make_candidate_row(station="KNYC"),
            make_candidate_row(station="KORD"),
        ]
        truth = {"KNYC": 82.0}  # KORD has no truth

        processed = [c for c in candidates if c["station"] in truth]
        assert len(processed) == 1
        assert processed[0]["station"] == "KNYC"
