"""Unit tests for report.py hit rate and P&L calculations.
No external API calls or CSV file dependencies — tests use in-memory CSV data.
"""
import sys
import os
import pytest
import csv
import io
from pathlib import Path
from unittest.mock import patch, mock_open

# Ensure meteoedge-spike is on the path so imports resolve without installation
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from report import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_settlements_csv(rows_list: list[dict]) -> io.StringIO:
    """Create an in-memory CSV file from a list of dicts."""
    output = io.StringIO()
    if rows_list:
        fieldnames = list(rows_list[0].keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows_list:
            writer.writerow(row)
    output.seek(0)
    return output


# ---------------------------------------------------------------------------
# Test basic hit rate calculation
# ---------------------------------------------------------------------------

class TestHitRateCalculation:
    def test_hit_rate_simple(self, capsys):
        """3 wins out of 4 candidates -> 75% hit rate."""
        rows = [
            {
                "ts": "2024-07-15T10:00:00",
                "station": "KNYC",
                "ticker": "T-1",
                "flagged_side": "YES",
                "candidate_won": "True",
                "pnl_cents": "45.0",
            },
            {
                "ts": "2024-07-15T11:00:00",
                "station": "KNYC",
                "ticker": "T-2",
                "flagged_side": "NO",
                "candidate_won": "True",
                "pnl_cents": "50.0",
            },
            {
                "ts": "2024-07-15T12:00:00",
                "station": "KORD",
                "ticker": "T-3",
                "flagged_side": "YES",
                "candidate_won": "False",
                "pnl_cents": "-40.0",
            },
            {
                "ts": "2024-07-15T13:00:00",
                "station": "KORD",
                "ticker": "T-4",
                "flagged_side": "YES",
                "candidate_won": "True",
                "pnl_cents": "55.0",
            },
        ]

        csv_content = make_settlements_csv(rows)

        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=csv_content.getvalue())):
                main()
                captured = capsys.readouterr()
                assert "Hit rate: 75.00%" in captured.out
                assert "Wins: 3" in captured.out


class TestDeduplication:
    def test_deduplicate_same_day_same_ticker_same_side(self, capsys):
        """Same (date, ticker, side) appears twice -> counted once."""
        rows = [
            {
                "ts": "2024-07-15T10:00:00",
                "station": "KNYC",
                "ticker": "T-1",
                "flagged_side": "YES",
                "candidate_won": "True",
                "pnl_cents": "45.0",
            },
            {
                "ts": "2024-07-15T11:00:00",
                "station": "KNYC",
                "ticker": "T-1",
                "flagged_side": "YES",
                "candidate_won": "False",
                "pnl_cents": "-40.0",
            },
            {
                "ts": "2024-07-15T12:00:00",
                "station": "KNYC",
                "ticker": "T-2",
                "flagged_side": "YES",
                "candidate_won": "True",
                "pnl_cents": "50.0",
            },
        ]

        csv_content = make_settlements_csv(rows)

        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=csv_content.getvalue())):
                main()
                captured = capsys.readouterr()
                # Should have 2 unique candidates (T-1 appears twice but counts as 1)
                assert "Unique flagged candidates: 2" in captured.out
                # Should have 2 wins (T-1's first occurrence and T-2)
                assert "Wins: 2" in captured.out


class TestPnlCalculation:
    def test_total_pnl_sum(self, capsys):
        """Total P&L is sum of all pnl_cents."""
        rows = [
            {
                "ts": "2024-07-15T10:00:00",
                "station": "KNYC",
                "ticker": "T-1",
                "flagged_side": "YES",
                "candidate_won": "True",
                "pnl_cents": "45.0",
            },
            {
                "ts": "2024-07-15T11:00:00",
                "station": "KNYC",
                "ticker": "T-2",
                "flagged_side": "NO",
                "candidate_won": "True",
                "pnl_cents": "50.0",
            },
            {
                "ts": "2024-07-15T12:00:00",
                "station": "KORD",
                "ticker": "T-3",
                "flagged_side": "YES",
                "candidate_won": "False",
                "pnl_cents": "-40.0",
            },
        ]

        csv_content = make_settlements_csv(rows)

        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=csv_content.getvalue())):
                main()
                captured = capsys.readouterr()
                # 45 + 50 - 40 = 55.0
                assert "Total P&L (cents, pre-fee): +55.0" in captured.out

    def test_avg_pnl_calculation(self, capsys):
        """Average P&L is mean of all pnl_cents."""
        rows = [
            {
                "ts": "2024-07-15T10:00:00",
                "station": "KNYC",
                "ticker": "T-1",
                "flagged_side": "YES",
                "candidate_won": "True",
                "pnl_cents": "40.0",
            },
            {
                "ts": "2024-07-15T11:00:00",
                "station": "KNYC",
                "ticker": "T-2",
                "flagged_side": "YES",
                "candidate_won": "True",
                "pnl_cents": "50.0",
            },
        ]

        csv_content = make_settlements_csv(rows)

        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=csv_content.getvalue())):
                main()
                captured = capsys.readouterr()
                # (40 + 50) / 2 = 45.0
                assert "Avg P&L per trade: +45.00¢" in captured.out


class TestDecisionLogic:
    def test_green_light_condition(self, capsys):
        """GREEN LIGHT: n >= 30 AND hit_rate >= 55%."""
        rows = []
        for i in range(30):
            rows.append({
                "ts": f"2024-07-15T{i%10:02d}:{i%60:02d}:00",
                "station": "KNYC",
                "ticker": f"T-{i}",
                "flagged_side": "YES",
                "candidate_won": "True" if i < 17 else "False",  # 17/30 = 56.67%
                "pnl_cents": "50.0",
            })

        csv_content = make_settlements_csv(rows)

        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=csv_content.getvalue())):
                main()
                captured = capsys.readouterr()
                assert "GREEN LIGHT" in captured.out
                assert "Proceed to full build" in captured.out

    def test_provisional_green_condition(self, capsys):
        """PROVISIONAL GREEN: hit_rate >= 55% but n < 30."""
        rows = []
        for i in range(10):
            rows.append({
                "ts": f"2024-07-15T{i%10:02d}:{i%60:02d}:00",
                "station": "KNYC",
                "ticker": f"T-{i}",
                "flagged_side": "YES",
                "candidate_won": "True" if i < 6 else "False",  # 6/10 = 60%
                "pnl_cents": "50.0",
            })

        csv_content = make_settlements_csv(rows)

        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=csv_content.getvalue())):
                main()
                captured = capsys.readouterr()
                assert "PROVISIONAL GREEN" in captured.out
                assert "Run more days" in captured.out

    def test_red_light_condition(self, capsys):
        """RED LIGHT: hit_rate < 55% AND n >= 30."""
        rows = []
        for i in range(30):
            rows.append({
                "ts": f"2024-07-15T{i%10:02d}:{i%60:02d}:00",
                "station": "KNYC",
                "ticker": f"T-{i}",
                "flagged_side": "YES",
                "candidate_won": "True" if i < 14 else "False",  # 14/30 = 46.67%
                "pnl_cents": "50.0",
            })

        csv_content = make_settlements_csv(rows)

        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=csv_content.getvalue())):
                main()
                captured = capsys.readouterr()
                assert "RED LIGHT" in captured.out
                assert "Do not proceed" in captured.out


class TestPerStationBreakdown:
    def test_per_station_breakdown(self, capsys):
        """Per-station breakdown is printed correctly."""
        rows = [
            {
                "ts": "2024-07-15T10:00:00",
                "station": "KNYC",
                "ticker": "T-1",
                "flagged_side": "YES",
                "candidate_won": "True",
                "pnl_cents": "45.0",
            },
            {
                "ts": "2024-07-15T11:00:00",
                "station": "KNYC",
                "ticker": "T-2",
                "flagged_side": "YES",
                "candidate_won": "True",
                "pnl_cents": "50.0",
            },
            {
                "ts": "2024-07-15T12:00:00",
                "station": "KORD",
                "ticker": "T-3",
                "flagged_side": "YES",
                "candidate_won": "False",
                "pnl_cents": "-40.0",
            },
            {
                "ts": "2024-07-15T13:00:00",
                "station": "KORD",
                "ticker": "T-4",
                "flagged_side": "YES",
                "candidate_won": "True",
                "pnl_cents": "55.0",
            },
        ]

        csv_content = make_settlements_csv(rows)

        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=csv_content.getvalue())):
                main()
                captured = capsys.readouterr()
                # KNYC: 2 flagged, 2 won (100%)
                assert "KNYC: 2 flagged, 2 won (100.0%)" in captured.out
                # KORD: 2 flagged, 1 won (50%)
                assert "KORD: 2 flagged, 1 won (50.0%)" in captured.out


class TestEmptyOrMissing:
    def test_empty_settlements_file(self, capsys):
        """Empty settlements.csv is handled gracefully."""
        csv_content = make_settlements_csv([])

        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=csv_content.getvalue())):
                main()
                captured = capsys.readouterr()
                assert "Zero unique flagged candidates" in captured.out

    def test_missing_settlements_file(self, capsys):
        """Missing settlements.csv is handled gracefully."""
        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = False
            main()
            captured = capsys.readouterr()
            assert "No settlements yet" in captured.out


class TestCandidateWonParsing:
    def test_candidate_won_string_true(self, capsys):
        """candidate_won='True' (string) is counted as a win."""
        rows = [
            {
                "ts": "2024-07-15T10:00:00",
                "station": "KNYC",
                "ticker": "T-1",
                "flagged_side": "YES",
                "candidate_won": "True",
                "pnl_cents": "45.0",
            },
            {
                "ts": "2024-07-15T11:00:00",
                "station": "KNYC",
                "ticker": "T-2",
                "flagged_side": "YES",
                "candidate_won": "False",
                "pnl_cents": "-40.0",
            },
        ]

        csv_content = make_settlements_csv(rows)

        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=csv_content.getvalue())):
                main()
                captured = capsys.readouterr()
                assert "Wins: 1" in captured.out
                assert "Hit rate: 50.00%" in captured.out

    def test_candidate_won_string_lowercase(self, capsys):
        """candidate_won='true' (lowercase) is counted as a win."""
        rows = [
            {
                "ts": "2024-07-15T10:00:00",
                "station": "KNYC",
                "ticker": "T-1",
                "flagged_side": "YES",
                "candidate_won": "true",
                "pnl_cents": "45.0",
            },
        ]

        csv_content = make_settlements_csv(rows)

        with patch("report.SETTLEMENTS_CSV") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=csv_content.getvalue())):
                main()
                captured = capsys.readouterr()
                assert "Wins: 1" in captured.out
                assert "Hit rate: 100.00%" in captured.out
