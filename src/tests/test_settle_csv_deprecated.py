"""Regression tests for the settlements.csv deprecation (issue #683).

``settle_yesterday()`` used to join yesterday's rows out of the *live*
``logs/candidates.csv``. Date-based log rotation (#169, 2026-06-16) moves
each day's rows into ``candidates.YYYY-MM-DD.csv`` before the daily settle
run, so that join stopped matching anything on 2026-06-18 and
``logs/settlements.csv`` has been frozen (dead) ever since. Even revived, the
file never carried ``p_yes_raw``/``ev_no_raw`` (pre-#564 schema), so it could
not serve the #682 report either.

Settlement truth is DB-first (``settlements``/``trades`` tables), so the CSV
leg is deprecated rather than fixed. These tests assert:

- ``settle_yesterday()`` no longer creates or writes ``settlements.csv``,
  even when matching candidates exist in ``candidates.csv``.
- The DB-side settlement calls (``settle_live_trades``, ``settle_shadow_trades``)
  are still invoked -- deprecating the CSV leg must not touch that path.
"""
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.scripts import settle


def _write_candidates_csv(path: Path, rows: "list[dict]") -> None:
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _candidate_row(yesterday: date, station: str = "KORD") -> dict:
    return {
        "ts": f"{yesterday.isoformat()}T12:00:00+00:00",
        "station": station,
        "question": "q",
        "end_date": yesterday.isoformat(),
        "ticker": "SYN-1",
        "bracket_low": "70.0",
        "bracket_high": "72.0",
        "yes_ask": "40",
        "no_ask": "60",
        "p_yes": "0.4",
        "ev_yes": "1.0",
        "ev_no": "1.0",
        "flagged_side": "NO",
        "flagged_edge": "5.0",
        "flagged_price": "60",
        "flagged_confidence": "0.6",
        "minutes_to_settlement": "120",
    }


class TestSettlementsCsvDeprecated:
    def _setup(self, tmp_path, monkeypatch):
        yesterday = date.today() - timedelta(days=1)
        candidates_csv = tmp_path / "logs" / "candidates.csv"
        _write_candidates_csv(candidates_csv, [_candidate_row(yesterday)])
        monkeypatch.setattr(settle, "CANDIDATES_CSV", candidates_csv)

        # Avoid real network calls / DB access -- irrelevant to the CSV leg.
        monkeypatch.setattr(settle, "STATIONS", [("KORD", 41.9, -87.9, "Chicago", "KORD", "F", "America/Chicago")])
        monkeypatch.setattr(settle, "fetch_daily_climate_high", lambda station, target: 71.0)
        monkeypatch.setattr(settle, "_open_db", lambda: None)

        return candidates_csv, yesterday

    def test_settle_yesterday_does_not_write_settlements_csv(self, tmp_path, monkeypatch):
        candidates_csv, yesterday = self._setup(tmp_path, monkeypatch)
        settlements_csv = tmp_path / "logs" / "settlements.csv"

        with patch.object(settle, "settle_live_trades") as mock_live, \
             patch.object(settle, "settle_shadow_trades") as mock_shadow:
            settle.settle_yesterday()

        assert not settlements_csv.exists(), (
            "settlements.csv must never be written -- the CSV leg is deprecated (#683)"
        )
        # DB-side settlement is unaffected by the CSV deprecation.
        mock_live.assert_called_once()
        mock_shadow.assert_called_once()

    def test_settle_yesterday_logs_deprecation_note(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)
        with patch.object(settle, "settle_live_trades"), \
             patch.object(settle, "settle_shadow_trades"):
            with caplog.at_level("INFO"):
                settle.settle_yesterday()

        assert any("deprecated" in rec.message and "#683" in rec.message for rec in caplog.records)

    def test_no_settlements_csv_config_import_needed(self):
        """SETTLEMENTS_CSV is no longer referenced by settle.py's write path."""
        assert not hasattr(settle, "SETTLEMENTS_CSV")
