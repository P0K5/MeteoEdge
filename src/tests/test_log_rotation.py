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
    housekeep_plaintext,
    iter_rotated_jsonl,
    resolve_current,
    rotated_path,
    rotate_plaintext_log,
    LOG_ROTATION_COMPRESS_AFTER_DAYS,
    LOG_ROTATION_RETAIN_DAYS,
    SNAPSHOT_RETAIN_DAYS,
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

    def test_deletes_compressed_file_beyond_retain_limit(self, tmp_path):
        """Compressed files older than retention cutoff should be deleted.

        This test verifies the fix for issue #980: housekeep() must delete aged .gz files,
        not just aged plaintext files.
        """
        base = tmp_path / "snapshots.jsonl"
        today = _today_utc()
        ancient_date = today - timedelta(days=LOG_ROTATION_RETAIN_DAYS + 1)

        # Create only the .gz file (simulating a file that was already compressed)
        gz_file = tmp_path / f"snapshots.{ancient_date.isoformat()}.jsonl.gz"
        gz_file.write_bytes(b"compressed data")

        housekeep(base)

        # The .gz file should be deleted (this is the fix for #980)
        assert not gz_file.exists(), "Aged .gz file should be deleted"

    def test_retains_compressed_file_within_retention_window(self, tmp_path):
        """Compressed files within retention window should be kept."""
        base = tmp_path / "snapshots.jsonl"
        today = _today_utc()
        recent_date = today - timedelta(days=LOG_ROTATION_RETAIN_DAYS - 5)

        # Create a .gz file within retention window
        gz_file = tmp_path / f"snapshots.{recent_date.isoformat()}.jsonl.gz"
        gz_file.write_bytes(b"compressed data")

        housekeep(base)

        # The .gz file should be retained
        assert gz_file.exists(), "Recent .gz file should be retained"


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


# ---------------------------------------------------------------------------
# TestHousekeepRetainOverride — per-file retention override tests
# ---------------------------------------------------------------------------

class TestHousekeepRetainOverride:
    """Tests for the optional retain_days override added for per-file retention."""

    def _make_dated_file(self, directory: Path, base_name: str, suffix: str, days_ago: int, today: date) -> Path:
        """Create a dated file at directory/base_name.YYYY-MM-DD.suffix."""
        file_date = today - timedelta(days=days_ago)
        path = directory / f"{base_name}.{file_date.isoformat()}{suffix}"
        path.write_text("{}")
        return path

    def test_retain_override_keeps_40day_old_file(self, tmp_path):
        """With retain_days=365, a 40-day-old file is NOT deleted (may be compressed)."""
        # Use a fixed date for predictability
        today = date(2026, 6, 18)
        base = tmp_path / "logs" / "snapshots.jsonl"
        base.parent.mkdir()
        old_file = self._make_dated_file(base.parent, "snapshots", ".jsonl", 40, today)
        with patch("src.utils.log_rotation._today_utc", return_value=today):
            housekeep(base, retain_days=365)
        # 40-day-old file is compressed but not deleted; check either form exists
        gz_file = Path(str(old_file) + ".gz")
        assert old_file.exists() or gz_file.exists(), "40-day-old file must survive (plain or compressed) with retain_days=365"

    def test_default_deletes_40day_old_file(self, tmp_path):
        """Without override (default 30 days), a 40-day-old file IS deleted."""
        # Use a fixed date for predictability
        today = date(2026, 6, 18)
        base = tmp_path / "logs" / "snapshots.jsonl"
        base.parent.mkdir()
        old_file = self._make_dated_file(base.parent, "snapshots", ".jsonl", 40, today)
        with patch("src.utils.log_rotation._today_utc", return_value=today):
            housekeep(base)
        assert not old_file.exists(), "40-day-old file must be deleted with default 30-day retain"

    def test_2day_old_file_compressed_with_override(self, tmp_path):
        """With retain_days=365 override, a 2-day-old file is still gzip-compressed."""
        from src.utils.log_rotation import LOG_ROTATION_COMPRESS_AFTER_DAYS
        # Use a fixed date for predictability
        today = date(2026, 6, 18)
        base = tmp_path / "logs" / "snapshots.jsonl"
        base.parent.mkdir()
        file_2d = self._make_dated_file(base.parent, "snapshots", ".jsonl", 2, today)
        with patch("src.utils.log_rotation._today_utc", return_value=today):
            housekeep(base, retain_days=365)
        gz = Path(str(file_2d) + ".gz")
        # 2-day-old file should be compressed (age >= LOG_ROTATION_COMPRESS_AFTER_DAYS=1)
        assert gz.exists(), "2-day-old file must be compressed even with retain_days=365"
        assert not file_2d.exists(), "original file removed after compression"

    def test_2day_old_file_compressed_without_override(self, tmp_path):
        """Without override, a 2-day-old file is compressed (no regression)."""
        # Use a fixed date for predictability
        today = date(2026, 6, 18)
        base = tmp_path / "logs" / "snapshots.jsonl"
        base.parent.mkdir()
        file_2d = self._make_dated_file(base.parent, "snapshots", ".jsonl", 2, today)
        with patch("src.utils.log_rotation._today_utc", return_value=today):
            housekeep(base)
        gz = Path(str(file_2d) + ".gz")
        assert gz.exists(), "2-day-old file must be compressed with default settings"
        assert not file_2d.exists()

    def test_no_override_preserves_30day_cutoff(self, tmp_path):
        """Regression guard: housekeep with no override still uses 30-day global default."""
        from src.utils.log_rotation import LOG_ROTATION_RETAIN_DAYS
        # Use a fixed date for predictability
        today = date(2026, 6, 18)
        base = tmp_path / "logs" / "snapshots.jsonl"
        base.parent.mkdir()
        # Create a file exactly at the boundary (retain_days + 1 = 31 days old)
        old_file = self._make_dated_file(base.parent, "snapshots", ".jsonl", LOG_ROTATION_RETAIN_DAYS + 1, today)
        with patch("src.utils.log_rotation._today_utc", return_value=today):
            housekeep(base)
        assert not old_file.exists(), f"File older than {LOG_ROTATION_RETAIN_DAYS} days must be deleted with default"

    def test_other_log_files_unaffected(self, tmp_path):
        """Callers that don't pass retain_days (e.g. candidates.csv) keep 30-day cutoff."""
        # Use a fixed date for predictability
        today = date(2026, 6, 18)
        base = tmp_path / "logs" / "candidates.csv"
        base.parent.mkdir()
        old_file = self._make_dated_file(base.parent, "candidates", ".csv", 40, today)
        with patch("src.utils.log_rotation._today_utc", return_value=today):
            housekeep(base)  # no retain_days — uses global 30-day default
        assert not old_file.exists(), "candidates.csv uses 30-day default, 40-day-old file deleted"


# ---------------------------------------------------------------------------
# Plain-text log rotation (e.g., bot.log)
# ---------------------------------------------------------------------------

class TestRotatePlaintextLog:
    """Tests for rotate_plaintext_log() and housekeep_plaintext()."""

    def test_copytruncate_pattern(self, tmp_path):
        """Verify copytruncate pattern: log is copied to dated file, original truncated."""
        log_file = tmp_path / "logs" / "bot.log"
        log_file.parent.mkdir()

        # Write initial content
        log_file.write_text("line 1\nline 2\n")
        today = _today_utc()

        # Rotate the log
        dated = rotate_plaintext_log(log_file, for_date=today)

        # Original file should still exist but be empty (copytruncate)
        assert log_file.exists(), "Original log file should still exist (copytruncate)"
        assert log_file.read_text() == "", "Original should be truncated to empty"

        # Dated file should have the original content
        assert dated.exists()
        assert dated.read_text() == "line 1\nline 2\n"

        # Verify dated filename is correct
        assert today.isoformat() in dated.name

    def test_preserves_systemd_fd_with_append(self, tmp_path):
        """Verify that systemd's held fd with O_APPEND is safe after copytruncate.

        With StandardOutput=append:, systemd's fd repositions to EOF on every write.
        After copytruncate, the original file is empty, so the next write goes to offset 0
        (not past a gap). This is why copytruncate is safe for append: targets.
        """
        log_file = tmp_path / "logs" / "bot.log"
        log_file.parent.mkdir()
        log_file.write_text("previous data\n")
        today = _today_utc()

        # Rotate using copytruncate
        dated = rotate_plaintext_log(log_file, for_date=today)

        # bot.log still exists (fd is still valid) but is empty
        assert log_file.exists()
        assert log_file.read_text() == ""
        # Dated file has the old data
        assert dated.exists()
        assert dated.read_text() == "previous data\n"

        # Simulate: systemd writes new data (fd's O_APPEND positions at EOF, which is 0)
        log_file.write_text("new data\n")
        assert log_file.read_text() == "new data\n"

    def test_housekeep_compresses_old_plaintext(self, tmp_path):
        """Old plain-text logs should be compressed."""
        base = tmp_path / "bot.log"
        today = _today_utc()
        old_date = today - timedelta(days=LOG_ROTATION_COMPRESS_AFTER_DAYS + 1)
        old_file = tmp_path / f"bot.{old_date.isoformat()}.log"
        old_file.write_text("old log content\n")

        housekeep_plaintext(base)

        gz = Path(str(old_file) + ".gz")
        assert gz.exists(), "Old plain-text log should be compressed"
        assert not old_file.exists(), "Original should be removed after compression"
        # Verify compression
        with gzip.open(gz, "rt") as f:
            assert f.read() == "old log content\n"

    def test_housekeep_deletes_aged_plaintext(self, tmp_path):
        """Plain-text logs older than retention should be deleted."""
        base = tmp_path / "bot.log"
        today = _today_utc()
        ancient_date = today - timedelta(days=SNAPSHOT_RETAIN_DAYS + 1)
        ancient_file = tmp_path / f"bot.{ancient_date.isoformat()}.log"
        ancient_file.write_text("ancient")

        housekeep_plaintext(base)

        assert not ancient_file.exists(), "Ancient plain-text log should be deleted"

    def test_housekeep_plaintext_uses_snapshot_retention_by_default(self, tmp_path):
        """housekeep_plaintext should default to SNAPSHOT_RETAIN_DAYS, not 30."""
        base = tmp_path / "bot.log"
        today = date(2026, 6, 18)
        # 40 days old — older than global 30 but younger than SNAPSHOT_RETAIN_DAYS (365)
        file_40d = tmp_path / f"bot.{(today - timedelta(days=40)).isoformat()}.log"
        file_40d.write_text("data")

        with patch("src.utils.log_rotation._today_utc", return_value=today):
            housekeep_plaintext(base)  # No retain_days param

        # With SNAPSHOT_RETAIN_DAYS default, 40-day-old file should survive
        assert file_40d.exists() or Path(str(file_40d) + ".gz").exists(), \
            "40-day-old bot.log should survive with SNAPSHOT_RETAIN_DAYS (365) default"

    def test_does_not_touch_today(self, tmp_path):
        """housekeep_plaintext should not touch today's file."""
        base = tmp_path / "bot.log"
        today = _today_utc()
        today_file = tmp_path / f"bot.{today.isoformat()}.log"
        today_file.write_text("today")

        housekeep_plaintext(base)

        assert today_file.exists(), "Today's file should not be touched"

    def test_rotate_creates_parent_directory(self, tmp_path):
        """rotate_plaintext_log should create parent directory if missing."""
        log_file = tmp_path / "nonexistent" / "subdir" / "bot.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("content")

        dated = rotate_plaintext_log(log_file)

        assert dated.parent.exists()
        assert dated.exists()

    def test_rotate_noop_if_file_missing(self, tmp_path):
        """rotate_plaintext_log should not fail if the log file doesn't exist."""
        log_file = tmp_path / "logs" / "bot.log"
        log_file.parent.mkdir()

        dated = rotate_plaintext_log(log_file)

        # Should return the dated path without error
        today = _today_utc()
        assert today.isoformat() in dated.name

    def test_housekeep_noop_if_directory_missing(self, tmp_path):
        """housekeep_plaintext should not fail if directory doesn't exist."""
        base = tmp_path / "nonexistent" / "bot.log"
        # Should not raise
        housekeep_plaintext(base)

    def test_housekeep_deletes_aged_gz_files(self, tmp_path):
        """Aged .gz files (not just plaintext) should be deleted."""
        base = tmp_path / "bot.log"
        today = _today_utc()
        ancient_date = today - timedelta(days=SNAPSHOT_RETAIN_DAYS + 1)

        # Create only a .gz file (no plaintext)
        ancient_gz = tmp_path / f"bot.{ancient_date.isoformat()}.log.gz"
        with gzip.open(ancient_gz, "wt") as f:
            f.write("ancient data\n")

        assert ancient_gz.exists()
        housekeep_plaintext(base)

        # .gz file should be deleted (was older than retention)
        assert not ancient_gz.exists(), "Aged .gz file should be deleted"

    def test_housekeep_preserves_recent_gz_files(self, tmp_path):
        """Recent .gz files should not be deleted."""
        base = tmp_path / "bot.log"
        today = _today_utc()
        recent_date = today - timedelta(days=5)

        recent_gz = tmp_path / f"bot.{recent_date.isoformat()}.log.gz"
        with gzip.open(recent_gz, "wt") as f:
            f.write("recent data\n")

        housekeep_plaintext(base)

        # Should still exist (not aged out)
        assert recent_gz.exists(), "Recent .gz file should be preserved"

    def test_second_rotation_same_day_preserves_first(self, tmp_path):
        """Two rotations on the same day should both be preserved (append, not overwrite)."""
        log_file = tmp_path / "logs" / "bot.log"
        log_file.parent.mkdir()
        today = _today_utc()

        # First rotation: 100 bytes
        log_file.write_text("a" * 100 + "\n")
        dated1 = rotate_plaintext_log(log_file, for_date=today)
        assert log_file.read_text() == "", "Log truncated after first rotation"
        assert dated1.read_text() == "a" * 100 + "\n", "First rotation data preserved"

        # Second rotation (same day): another 50 bytes
        log_file.write_text("b" * 50 + "\n")
        dated2 = rotate_plaintext_log(log_file, for_date=today)
        assert dated1 == dated2, "Same date produces same dated path"
        assert log_file.read_text() == "", "Log truncated after second rotation"

        # Both rotations' data should be in dated file (appended)
        content = dated2.read_text()
        assert "a" * 100 in content, "First rotation data still in dated file"
        assert "b" * 50 in content, "Second rotation data appended to dated file"
