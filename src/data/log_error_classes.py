"""Aggregate WARNING/ERROR/CRITICAL log lines by logger + message class (#1385).

Follow-up to #913: silent failures that only reach ``logs/*.log`` were
invisible in the health report. This module is read-only and self-contained.

Log line format (``src/logging_config.py``)::

    YYYY-MM-DDTHH:MM:SS LEVEL [pid] [logger] message

Bounded runtime: each file is read from its END backwards in blocks, stopping
at the first line older than the baseline window (24h + 7 days), and capped at
``MAX_LINES_PER_FILE`` lines / ``MAX_BYTES_PER_FILE`` bytes. A multi-GB log is
never read whole; hitting a cap is reported in the section.

Timestamps: UTC since #1312 (deployed 2026-10-14). Older lines are host-local
(WEST, UTC+1). Lines dated before ``UTC_SWITCH`` are shifted back by
``LOCAL_OFFSET``; the switch day itself cannot be told apart, so it is read as
UTC and may be off by at most 1h at a window edge (tolerated, not corrected).

Trend: window count vs the average daily count over the prior ``BASELINE_DAYS``
(same idea as ``copy_skip_reasons.trend`` but with its own thresholds, so that
module's copy-trading vocabulary and constants are not shared).
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEVELS = ("WARNING", "ERROR", "CRITICAL")
WINDOW_HOURS = 24
BASELINE_DAYS = 7
MIN_COUNT = 3
SPIKE_RATIO = 3.0
TOP_N = 10

MAX_LINES_PER_FILE = 1_000_000
MAX_BYTES_PER_FILE = 256 * 1024 * 1024
_BLOCK = 1024 * 1024

UTC_SWITCH = datetime(2026, 10, 14, tzinfo=timezone.utc)
LOCAL_OFFSET = timedelta(hours=1)
# Stop reading a file only once a line is this much older than the cutoff, so
# the 1h local-time shift and mild interleaving of concurrent writers are safe.
_STOP_SLACK = timedelta(hours=2)
_FUTURE_SLACK = timedelta(hours=1)

_LINE_RE = re.compile(
    r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d) (WARNING|ERROR|CRITICAL) "
    r"\[\d+\] \[([^\]]*)\] (.*)$"
)

_RECORD_PREFIX_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d ")

_NORMALIZERS = (
    (re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://\S+"), "<url>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<hex>"),
    (re.compile(r"(?<![\w<])(?:~|\.{0,2})?/[\w.\-@+]+(?:/[\w.\-@+]*)+"), "<path>"),
    (re.compile(r"(?<![\w<])[\w.\-]+(?:/[\w.\-]+)+\.\w+"), "<path>"),
    (re.compile(r"-?\d+(?:[.,:]\d+)*"), "<n>"),
)
_MAX_CLASS_LEN = 160


def normalize_message(msg: str) -> str:
    """Collapse urls, hex addresses/condition ids, uuids, paths and numbers."""
    for pat, repl in _NORMALIZERS:
        msg = pat.sub(repl, msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg[:_MAX_CLASS_LEN]


def parse_line(line: str) -> "tuple[datetime, str, str, str] | None":
    """``(utc_ts, level, logger, message)`` for a WARNING+ line, else None."""
    m = _LINE_RE.match(line)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if ts < UTC_SWITCH:
        ts -= LOCAL_OFFSET
    return ts, m.group(2), m.group(3), m.group(4)


def _iter_lines_reversed(path: Path, max_bytes: int):
    """Yield decoded lines from the end of ``path`` backwards, bounded bytes."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        pos = fh.tell()
        floor = max(0, pos - max_bytes)
        carry = b""
        while pos > floor:
            step = min(_BLOCK, pos - floor)
            pos -= step
            fh.seek(pos)
            parts = (fh.read(step) + carry).split(b"\n")
            carry = parts[0]
            for raw in reversed(parts[1:]):
                yield raw.decode("utf-8", "replace").rstrip("\r")
        if floor == 0 and carry:
            yield carry.decode("utf-8", "replace").rstrip("\r")


def log_files(log_dir: Path) -> "list[Path]":
    """``*.log`` plus the newest rotation ``*.log.1`` (by name; cheap)."""
    if not log_dir.is_dir():
        return []
    return sorted(set(log_dir.glob("*.log")) | set(log_dir.glob("*.log.1")))


def collect(log_dir: Path, now: datetime, *,
            max_lines: int = MAX_LINES_PER_FILE,
            max_bytes: int = MAX_BYTES_PER_FILE) -> dict:
    """Count classes in the window and baseline.

    Returns ``{"window": Counter, "baseline": Counter, "files": int,
    "unparsed": int, "truncated": [names]}`` keyed by ``(logger, class)``.
    """
    win_start = now - timedelta(hours=WINDOW_HOURS)
    base_start = win_start - timedelta(days=BASELINE_DAYS)
    stop_before = base_start - _STOP_SLACK
    out = {"window": Counter(), "baseline": Counter(), "files": 0,
           "unparsed": 0, "truncated": []}
    for path in log_files(log_dir):
        out["files"] += 1
        scanned = 0
        reached_cutoff = False
        try:
            for line in _iter_lines_reversed(path, max_bytes):
                if not line:
                    continue
                scanned += 1
                if scanned > max_lines:
                    break
                parsed = parse_line(line)
                if parsed is None:
                    # Count only malformed log records (bad timestamp, or a
                    # digit-led line that is not a record); traceback lines,
                    # INFO/DEBUG records and blanks are expected noise.
                    if _LINE_RE.match(line) or (
                        line[:1].isdigit() and not _RECORD_PREFIX_RE.match(line)
                    ):
                        out["unparsed"] += 1
                    continue
                ts, level, logger_name, msg = parsed
                if ts < stop_before:
                    reached_cutoff = True
                    break
                if ts > now + _FUTURE_SLACK:
                    continue
                key = (logger_name, normalize_message(msg))
                if ts >= win_start:
                    out["window"][key] += 1
                elif ts >= base_start:
                    out["baseline"][key] += 1
        except OSError:
            out["unparsed"] += 1
            continue
        if not reached_cutoff and (scanned > max_lines or _was_byte_capped(path, max_bytes)):
            out["truncated"].append(path.name)
    return out


def _was_byte_capped(path: Path, max_bytes: int) -> bool:
    try:
        return path.stat().st_size > max_bytes
    except OSError:
        return False


def classify_trend(window: int, baseline_total: int) -> str:
    """``NEW`` (never in baseline), ``SPIKE`` (>= 3x daily average) or ``STEADY``.

    Below ``MIN_COUNT`` occurrences in the window everything is ``STEADY``.
    """
    if window < MIN_COUNT:
        return "STEADY"
    if baseline_total == 0:
        return "NEW"
    if window >= SPIKE_RATIO * (baseline_total / BASELINE_DAYS):
        return "SPIKE"
    return "STEADY"


def build_log_errors(log_dir: Path, now: datetime) -> "tuple[list[str], dict]":
    """Health-report section: lines plus flags ``n_new`` / ``n_spike``."""
    lines = [f"Log WARNING/ERROR classes ({WINDOW_HOURS}h)", "-" * 30]
    flags: dict = {"n_new": 0, "n_spike": 0}
    try:
        data = collect(log_dir, now)
    except Exception:
        lines.append("  (unavailable -- log scan error)")
        lines.append("")
        return lines, flags
    window, baseline = data["window"], data["baseline"]
    if not data["files"]:
        lines.append(f"  (no log files in {log_dir})")
        lines.append("")
        return lines, flags
    rows = []
    for key, count in window.items():
        rows.append((classify_trend(count, baseline.get(key, 0)), count, key))
    flags["n_new"] = sum(1 for r in rows if r[0] == "NEW")
    flags["n_spike"] = sum(1 for r in rows if r[0] == "SPIKE")
    order = {"NEW": 0, "SPIKE": 1, "STEADY": 2}
    rows.sort(key=lambda r: (order[r[0]], -r[1], r[2]))
    lines.append(
        f"  {sum(window.values())} line(s) in {len(window)} class(es) across "
        f"{data['files']} file(s); NEW {flags['n_new']}, SPIKE {flags['n_spike']}"
    )
    for tr, count, (logger_name, cls) in rows[:TOP_N]:
        avg = baseline.get((logger_name, cls), 0) / BASELINE_DAYS
        lines.append(f"  [{tr}] {count:>5} (7d avg {avg:.1f}/d) [{logger_name}] {cls}")
    if len(rows) > TOP_N:
        lines.append(f"  ... {len(rows) - TOP_N} more class(es) not shown")
    if data["unparsed"]:
        lines.append(f"  {data['unparsed']} unparseable line(s)/file(s) skipped")
    if data["truncated"]:
        lines.append(
            "  scan capped (older lines not read): " + ", ".join(sorted(data["truncated"]))
        )
    lines.append("")
    return lines, flags


def section(db, today: datetime) -> "tuple[list[str], dict]":
    """Report-section adapter (``(db, today)`` builder signature)."""
    log_dir = Path(__file__).resolve().parents[2] / "logs"
    return build_log_errors(log_dir, today)
