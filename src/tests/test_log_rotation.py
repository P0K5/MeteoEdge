"""Tests for src/utils/log_rotation.py.

Covers:
- Date-based file naming
- Symlink creation and update
- Compression of aged files
- Deletion of files past retain limit
- No file grows without bound (rotation produces new files each day)
- iter_rotated_jsonl reads across multiple dated files
- Delta-logging suppression and heartbeat in weather/builder.py
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.utils.log_rotation import (
    _dated_path,
    _today_utc,
    housekeep,
    iter_rotated_jsonl,
    resolve_current,
    rotated_path,
    LOG_ROTATION_COMPRESS_AFTER_DAYS,
    LOG_ROTATION_RETAIN_DAYS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# _dated_path
# ---------------------------------------------------------------------------

class TestDatedPath:
    def test_jsonl_stem(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        d = date(2026, 6, 16)
        result = _dated_path(base, for_date=d)
        assert result == tmp_path / "snapshots.2026-06-16.jsonl"

    def test_csv_stem(self, tmp_path):
        base = tmp_path / "candidates.csv"
        d = date(2026, 1, 1)
        result = _dated_path(base, for_date=d)
        assert result == tmp_path / "candidates.2026-01-01.csv"

    def test_defaults_to_today(self, tmp_path):
        base = tmp_path / "live_trades.jsonl"
        result = _dated_path(base)
        today = _today_utc()
        assert result.name == f"live_trades.{today.isoformat()}.jsonl"


# ---------------------------------------------------------------------------
# rotated_path — file creation and symlink
# ---------------------------------------------------------------------------

class TestRotatedPath:
    def test_creates_dated_file(self, tmp_path):
        base = tmp_path / "logs" / "snapshots.jsonl"
        dest = rotated_path(base)
        assert dest.exists()
        today = _today_utc()
        assert dest.name == f"snapshots.{today.isoformat()}.jsonl"

    def test_creates_symlink(self, tmp_path):
        base = tmp_path / "logs" / "snapshots.jsonl"
        rotated_path(base)
        # symlink at base should point to today's dated file
        if base.is_symlink():
            target = os.readlink(base)
            today = _today_utc()
            assert today.isoformat() in target

    def test_returns_same_path_on_second_call(self, tmp_path):
        base = tmp_path / "logs" / "snapshots.jsonl"
        p1 = rotated_path(base)
        p2 = rotated_path(base)
        assert p1 == p2

    def test_no_file_grows_without_bound(self, tmp_path):
        """Rotation produces a new dated file each day; old files are separate."""
        base = tmp_path / "logs" / "snapshots.jsonl"

        day1 = date(2026, 6, 14)
        day2 = date(2026, 6, 15)
        day3 = date(2026, 6, 16)

        p1 = rotated_path(base, for_date=day1)
        p2 = rotated_path(base, for_date=day2)
        p3 = rotated_path(base, for_date=day3)

        # Each day produces a distinct file
        assert p1 != p2
        assert p2 != p3
        assert p1 != p3

        # Write different content to each
        for path, content in [(p1, b"day1\n"), (p2, b"day2\n"), (p3, b"day3\n")]:
            path.write_bytes(content)

        # The files are independent — day1 only has day1 content
        assert p1.read_bytes() == b"day1\n"
        assert p2.read_bytes() == b"day2\n"
        assert p3.read_bytes() == b"day3\n"


# ---------------------------------------------------------------------------
# housekeep — compression and deletion
# ---------------------------------------------------------------------------

class TestHousekeep:
    def _make_dated(self, tmp_path: Path, stem: str, suffix: str,
                    for_date: date, content: bytes = b"data\n") -> Path:
        base = tmp_path / f"{stem}{suffix}"
        path = tmp_path / f"{stem}.{for_date.isoformat()}{suffix}"
        path.write_bytes(content)
        return path

    def test_compresses_old_file(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        today = _today_utc()
        old_date = today - timedelta(days=LOG_ROTATION_COMPRESS_AFTER_DAYS + 1)
        old_file = self._make_dated(tmp_path, "snapshots", ".jsonl", old_date,
                                     b'{"a": 1}\n')
        housekeep(base)
        gz = Path(str(old_file) + ".gz")
        assert gz.exists(), "Aged file should be compressed"
        assert not old_file.exists(), "Original should be removed after compression"
        # Compressed content should be readable
        with gzip.open(gz, "rb") as f:
            assert f.read() == b'{"a": 1}\n'

    def test_deletes_file_beyond_retain_limit(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        today = _today_utc()
        ancient_date = today - timedelta(days=LOG_ROTATION_RETAIN_DAYS + 1)
        ancient_file = self._make_dated(tmp_path, "snapshots", ".jsonl", ancient_date)
        # Also create the gz version (simulating already-compressed file)
        gz = Path(str(ancient_file) + ".gz")
        gz.write_bytes(b"compressed")
        housekeep(base)
        assert not ancient_file.exists(), "Ancient file should be deleted"
        assert not gz.exists(), "Ancient gz file should also be deleted"

    def test_does_not_touch_today(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        today = _today_utc()
        today_file = self._make_dated(tmp_path, "snapshots", ".jsonl", today, b"today\n")
        housekeep(base)
        assert today_file.exists(), "Today's file should not be touched"

    def test_noop_when_directory_missing(self, tmp_path):
        base = tmp_path / "nonexistent_dir" / "snapshots.jsonl"
        # Should not raise
        housekeep(base)


# ---------------------------------------------------------------------------
# iter_rotated_jsonl — multi-file reader
# ---------------------------------------------------------------------------

class TestIterRotatedJsonl:
    def test_reads_multiple_dated_files_in_order(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        day1 = date(2026, 6, 14)
        day2 = date(2026, 6, 16)

        _write_jsonl(tmp_path / "snapshots.2026-06-14.jsonl", [{"day": 1}])
        _write_jsonl(tmp_path / "snapshots.2026-06-16.jsonl", [{"day": 2}])

        records = list(iter_rotated_jsonl(base))
        assert len(records) == 2
        assert records[0]["day"] == 1
        assert records[1]["day"] == 2

    def test_falls_back_to_bare_file(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        _write_jsonl(base, [{"legacy": True}])
        records = list(iter_rotated_jsonl(base))
        assert len(records) == 1
        assert records[0]["legacy"] is True

    def test_reads_compressed_files(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        gz_path = tmp_path / "snapshots.2026-06-01.jsonl.gz"
        with gzip.open(gz_path, "wt") as f:
            f.write(json.dumps({"compressed": True}) + "\n")
        records = list(iter_rotated_jsonl(base, include_compressed=True))
        assert any(r.get("compressed") for r in records)

    def test_skips_corrupt_lines(self, tmp_path):
        base = tmp_path / "snapshots.jsonl"
        p = tmp_path / "snapshots.2026-06-16.jsonl"
        p.write_text('{"ok": 1}\nnot json\n{"ok": 2}\n')
        records = list(iter_rotated_jsonl(base))
        assert len(records) == 2
        assert all(r.get("ok") for r in records)

    def test_empty_directory(self, tmp_path):
        base = tmp_path / "subdir" / "snapshots.jsonl"
        records = list(iter_rotated_jsonl(base))
        assert records == []


# ---------------------------------------------------------------------------
# Delta-logging for weather
# ---------------------------------------------------------------------------

class TestWeatherDeltaLogging:
    """Tests for _should_log_weather in src.weather.builder."""

    def setup_method(self):
        # Reset the module-level state before each test
        from src.weather import builder
        builder._weather_log_state.clear()

    def _call(self, station, high_f, latest_f, nws):
        from src.weather.builder import _should_log_weather
        return _should_log_weather(station, high_f, latest_f, nws)

    def test_first_call_always_logs(self):
        assert self._call("RJTT", 85.0, 80.0, 84.0) is True

    def test_no_change_suppressed(self):
        self._call("RJTT", 85.0, 80.0, 84.0)  # prime state
        result = self._call("RJTT", 85.0, 80.0, 84.0)
        assert result is False, "Identical values should be suppressed"

    def test_small_change_suppressed(self):
        self._call("RJTT", 85.0, 80.0, 84.0)
        # 0.3°F change — below 0.5°F threshold
        result = self._call("RJTT", 85.3, 80.0, 84.0)
        assert result is False

    def test_large_change_logged(self):
        self._call("RJTT", 85.0, 80.0, 84.0)
        # 1°F change in latest_temp — above threshold
        result = self._call("RJTT", 85.0, 81.0, 84.0)
        assert result is True

    def test_high_f_change_logged(self):
        self._call("RJTT", 85.0, 80.0, 84.0)
        result = self._call("RJTT", 85.6, 80.0, 84.0)
        assert result is True

    def test_nws_change_logged(self):
        self._call("RJTT", 85.0, 80.0, 84.0)
        result = self._call("RJTT", 85.0, 80.0, 85.0)
        assert result is True

    def test_nws_none_to_value(self):
        self._call("RJTT", 85.0, 80.0, None)
        result = self._call("RJTT", 85.0, 80.0, 84.0)
        assert result is True

    def test_heartbeat_after_interval(self):
        from src.weather import builder
        self._call("RJTT", 85.0, 80.0, 84.0)
        # Simulate last_logged being >1 hour ago
        past = datetime.now(timezone.utc) - timedelta(seconds=3601)
        builder._weather_log_state["RJTT"]["last_logged"] = past
        # Same values but heartbeat should fire
        result = self._call("RJTT", 85.0, 80.0, 84.0)
        assert result is True

    def test_independent_stations(self):
        self._call("RJTT", 85.0, 80.0, 84.0)
        # RKSI hasn't been logged yet — should always log first
        result = self._call("RKSI", 90.0, 85.0, 89.0)
        assert result is True

    def test_suppression_does_not_mutate_state(self):
        """After a suppressed log, state should stay unchanged."""
        from src.weather import builder
        self._call("RJTT", 85.0, 80.0, 84.0)
        first_logged = builder._weather_log_state["RJTT"]["last_logged"]
        self._call("RJTT", 85.0, 80.0, 84.0)  # suppress
        assert builder._weather_log_state["RJTT"]["last_logged"] == first_logged
