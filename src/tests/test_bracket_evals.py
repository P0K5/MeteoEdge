"""Tests for bracket-evaluation snapshots (#826).

Verifies:
- Rows are written to the bracket_evals file with all required fields
- Hourly deduplication: one row per (station, ticker, hour) regardless of poll count
- Different hours write separate rows for the same (station, ticker)
- Paper mode downgrades execution_mode "live" to "paper"
- Live mode preserves execution_mode "live"
- Invalid/empty snapshots are skipped gracefully
- Archive rotation calls housekeep with 90-day retention
- The Edge tab / scan_decisions data contract is not touched
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import BRACKET_EVAL_RETAIN_DAYS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snap(
    station="KORD",
    ticker="KORD-high-81-83",
    poll_ts="2026-07-05T14:03:00+00:00",
    yes_ask=40,
    no_ask=60,
    p_yes=0.28,
    raw_p_yes=0.31,
    emos_mode="legacy",
    is_next_day=0,
    minutes_to_settlement=90.0,
    execution_mode="live",
    settlement_date="2026-07-05",
    **kwargs,
) -> dict:
    """Build a minimal snap dict with the required fields for bracket-eval write."""
    data = {
        "station": station,
        "ticker": ticker,
        "bracket_low": 81.0,
        "bracket_high": 83.0,
        "poll_ts": poll_ts,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "p_yes": p_yes,
        "raw_p_yes": raw_p_yes,
        "emos_mode": emos_mode,
        "is_next_day": is_next_day,
        "minutes_to_settlement": minutes_to_settlement,
        "execution_mode": execution_mode,
        "settlement_date": settlement_date,
    }
    data.update(kwargs)
    return data


def _read_jsonl(filepath: str | Path) -> list[dict]:
    """Read and parse a JSONL file into a list of dicts."""
    path = Path(filepath)
    if not path.exists():
        return []
    rows = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Basic write + field completeness
# ---------------------------------------------------------------------------

class TestBracketEvalsWrite:
    """Basic persistence: rows are written with all required fields populated."""

    def test_writes_one_row_per_hour_bucket(self, tmp_path):
        """A single snapshot writes exactly one row to the bracket_evals file."""
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations([_snap()], has_live_trader=True)

        rows = _read_jsonl(outfile)
        assert len(rows) == 1

    def test_all_required_fields_are_populated(self, tmp_path):
        """Every row must carry all minimum fields from the issue spec."""
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations([_snap()], has_live_trader=True)

        rows = _read_jsonl(outfile)
        assert len(rows) == 1
        row = rows[0]

        required_fields = [
            "station", "ticker", "bracket_low", "bracket_high",
            "poll_ts", "yes_ask", "no_ask", "p_yes", "p_yes_raw",
            "emos_mode", "is_next_day", "minutes_to_settlement",
            "execution_mode", "settlement_date",
        ]
        for field in required_fields:
            assert field in row, f"missing required field: {field}"
            assert row[field] is not None, f"field {field} is None"

    def test_poll_ts_is_hour_bucket_format(self, tmp_path):
        """poll_ts must be truncated to hour granularity with :00:00+00:00 suffix."""
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations(
                [_snap(poll_ts="2026-07-05T14:23:45+00:00")], has_live_trader=True,
            )

        rows = _read_jsonl(outfile)
        assert rows[0]["poll_ts"] == "2026-07-05T14:00:00+00:00"


# ---------------------------------------------------------------------------
# Hourly deduplication
# ---------------------------------------------------------------------------

class TestBracketEvalsDedup:
    """Hourly dedup: one row per (station, ticker, hour) regardless of poll count."""

    def test_same_hour_same_station_ticker_writes_once(self, tmp_path):
        """Three polls in the same hour for same (station, ticker) = 1 row."""
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            snaps = [
                _snap(poll_ts="2026-07-05T14:01:00+00:00"),
                _snap(poll_ts="2026-07-05T14:03:00+00:00"),
                _snap(poll_ts="2026-07-05T14:58:00+00:00"),
            ]
            _write_bracket_evaluations(snaps, has_live_trader=True)

        rows = _read_jsonl(outfile)
        assert len(rows) == 1, f"expected 1 row, got {len(rows)}"

    def test_different_hours_write_separate_rows(self, tmp_path):
        """Same (station, ticker) across different hour buckets writes one row per hour."""
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            snaps = [
                _snap(poll_ts="2026-07-05T14:01:00+00:00"),
                _snap(poll_ts="2026-07-05T15:01:00+00:00"),
                _snap(poll_ts="2026-07-05T16:01:00+00:00"),
            ]
            _write_bracket_evaluations(snaps, has_live_trader=True)

        rows = _read_jsonl(outfile)
        assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"

    def test_different_stations_same_hour_write_separate_rows(self, tmp_path):
        """Different stations in the same hour each get their own row."""
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            snaps = [
                _snap(station="KORD", ticker="KORD-high-81-83",
                      poll_ts="2026-07-05T14:01:00+00:00"),
                _snap(station="KMIA", ticker="KMIA-high-82-84",
                      poll_ts="2026-07-05T14:03:00+00:00"),
            ]
            _write_bracket_evaluations(snaps, has_live_trader=True)

        rows = _read_jsonl(outfile)
        assert len(rows) == 2

    def test_dedup_cache_persists_across_calls(self, tmp_path):
        """The dedup cache must persist across invocations (same process, separate polls)."""
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            # First poll: writes one row for KORD at 14:xx
            _write_bracket_evaluations(
                [_snap(poll_ts="2026-07-05T14:01:00+00:00")],
                has_live_trader=True,
            )
            # Second poll (same hour): must be deduped
            _write_bracket_evaluations(
                [_snap(poll_ts="2026-07-05T14:45:00+00:00")],
                has_live_trader=True,
            )

        rows = _read_jsonl(outfile)
        assert len(rows) == 1, "dedup cache didn't persist across calls"


# ---------------------------------------------------------------------------
# Execution mode
# ---------------------------------------------------------------------------

class TestBracketEvalsExecutionMode:
    """Paper mode downgrades "live" to "paper"; "shadow" stays "shadow"."""

    def test_live_with_trader_stays_live(self, tmp_path):
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations(
                [_snap(execution_mode="live")], has_live_trader=True,
            )

        rows = _read_jsonl(outfile)
        assert rows[0]["execution_mode"] == "live"

    def test_live_without_trader_becomes_paper(self, tmp_path):
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations(
                [_snap(execution_mode="live")], has_live_trader=False,
            )

        rows = _read_jsonl(outfile)
        assert rows[0]["execution_mode"] == "paper"

    def test_shadow_stays_shadow(self, tmp_path):
        """Shadow stations are shadow regardless of live_trader presence."""
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations(
                [_snap(execution_mode="shadow")], has_live_trader=True,
            )

        rows = _read_jsonl(outfile)
        assert rows[0]["execution_mode"] == "shadow"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestBracketEvalsEdgeCases:
    """Invalid/empty snapshots are skipped gracefully."""

    def test_empty_snapshot_list_no_error(self, tmp_path):
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations([], has_live_trader=True)

        rows = _read_jsonl(outfile)
        assert len(rows) == 0

    def test_missing_station_is_skipped(self, tmp_path):
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations(
                [_snap(station="", ticker="t1")], has_live_trader=True,
            )

        rows = _read_jsonl(outfile)
        assert len(rows) == 0

    def test_missing_ticker_is_skipped(self, tmp_path):
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations(
                [_snap(station="KORD", ticker="")], has_live_trader=True,
            )

        rows = _read_jsonl(outfile)
        assert len(rows) == 0

    def test_missing_poll_ts_is_skipped(self, tmp_path):
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations(
                [_snap(poll_ts="")], has_live_trader=True,
            )

        rows = _read_jsonl(outfile)
        assert len(rows) == 0

    def test_preserves_emos_mode_and_is_next_day(self, tmp_path):
        """Field values for emos_mode and is_next_day must be passed through faithfully."""
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations(
                [_snap(emos_mode="emos_primary", is_next_day=1, execution_mode="shadow")],
                has_live_trader=True,
            )

        rows = _read_jsonl(outfile)
        assert rows[0]["emos_mode"] == "emos_primary"
        assert rows[0]["is_next_day"] == 1

    def test_sub_penny_raw_prices_survive_end_to_end(self, tmp_path):
        """Issue #1076: a sub-penny yes_price_raw/no_price_raw (which yes_ask/
        no_ask's clamp would round away) survives a full write + JSONL
        round-trip with full float precision."""
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep"),
        ):
            _write_bracket_evaluations(
                [_snap(yes_ask=1, no_ask=99, yes_price_raw=0.003, no_price_raw=0.997)],
                has_live_trader=True,
            )

        rows = _read_jsonl(outfile)
        assert len(rows) == 1
        assert rows[0]["yes_ask"] == 1
        assert rows[0]["no_ask"] == 99
        assert rows[0]["yes_price_raw"] == pytest.approx(0.003)
        assert rows[0]["no_price_raw"] == pytest.approx(0.997)


# ---------------------------------------------------------------------------
# Archive rotation
# ---------------------------------------------------------------------------

class TestBracketEvalsRotation:
    """Housekeep is called with the 90-day retention constant."""

    def test_housekeep_called_with_correct_retention(self, tmp_path):
        """housekeep must be called with BRACKET_EVAL_RETAIN_DAYS (90)."""
        from src.scripts.run import _write_bracket_evaluations, _last_bracket_hour
        _last_bracket_hour.clear()

        outfile = tmp_path / "bracket_evals.jsonl"
        with (
            patch("src.scripts.run.LOG_DIR", tmp_path),
            patch("src.scripts.run.BRACKET_EVALS_JSONL", outfile),
            patch("src.scripts.run.rotated_path", return_value=outfile),
            patch("src.scripts.run.housekeep") as mock_housekeep,
        ):
            _write_bracket_evaluations(
                [_snap()], has_live_trader=True,
            )

            mock_housekeep.assert_called_once()
            _, kwargs = mock_housekeep.call_args
            assert kwargs.get("retain_days") == BRACKET_EVAL_RETAIN_DAYS, (
                f"expected retain_days={BRACKET_EVAL_RETAIN_DAYS}, "
                f"got {kwargs.get('retain_days')}"
            )


# ---------------------------------------------------------------------------
# Non-regression: Edge tab / scan_decisions data contract is not touched
# ---------------------------------------------------------------------------

class TestBracketEvalsNonRegression:
    """Adding bracket-eval writes does not alter scan_decisions or SNAPSHOTS_JSONL."""

    def test_writer_does_not_reference_other_log_paths(self):
        """_write_bracket_evaluations code must not touch SNAPSHOTS_JSONL, CANDIDATES_CSV,
        or the scan_decisions persistence path."""
        from src.scripts.run import _write_bracket_evaluations
        import inspect

        # Get code lines only (skip the def line and docstring).
        code_lines = inspect.getsourcelines(_write_bracket_evaluations)[0]
        # Find the first non-docstring, non-blank line after the signature.
        body_start = 0
        in_docstring = False
        for i, line in enumerate(code_lines):
            stripped = line.strip()
            if '"""' in stripped:
                if not in_docstring:
                    in_docstring = True
                else:
                    body_start = i + 1
                    break
        code_body = "".join(code_lines[body_start:])

        assert "SNAPSHOTS_JSONL" not in code_body, (
            "_write_bracket_evaluations must not reference SNAPSHOTS_JSONL"
        )
        assert "CANDIDATES_CSV" not in code_body, (
            "_write_bracket_evaluations must not reference CANDIDATES_CSV"
        )
        assert "scan_decisions" not in code_body, (
            "_write_bracket_evaluations must not reference scan_decisions"
        )
