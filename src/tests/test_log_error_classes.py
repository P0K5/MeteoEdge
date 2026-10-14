"""Issue #1385: WARNING/ERROR log-line aggregation by message class."""
import time
from datetime import datetime, timedelta, timezone

from src.data import log_error_classes as lec
from src.scripts.daily_health_report import _build_verdict

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _ln(ts, level, logger, msg, pid=1):
    return f"{ts.strftime('%Y-%m-%dT%H:%M:%S')} {level} [{pid}] [{logger}] {msg}\n"


def _write(path, lines):
    path.write_text("".join(lines))


def test_normalize_collapses_ids_numbers_paths():
    a = lec.normalize_message("fetch failed for 0xabc123def456 at /home/p0k5/x/y.json after 3 tries (position 42)")
    b = lec.normalize_message("fetch failed for 0xFFFFFFFF0000 at /tmp/z/q.json after 9 tries (position 7)")
    assert a == b == "fetch failed for <hex> at <path> after <n> tries (position <n>)"
    assert lec.normalize_message("GET https://x.io/a/b?c=1 -> 503") == "GET <url> -> <n>"
    long_id = "0x" + "a" * 64
    assert lec.normalize_message(f"cond {long_id}") == "cond <hex>"


def test_classes_counted_and_info_skipped(tmp_path):
    lines = [_ln(NOW - timedelta(hours=i), "WARNING", "src.a", f"timeout {i}ms") for i in range(1, 5)]
    lines.append(_ln(NOW - timedelta(hours=1), "INFO", "httpx", "HTTP Request: GET ok"))
    lines.append("Traceback (most recent call last):\n")
    _write(tmp_path / "run.log", lines)
    d = lec.collect(tmp_path, NOW)
    assert d["window"] == {("src.a", "timeout <n>ms"): 4}
    assert d["unparsed"] == 0


def test_trend_new_spike_steady():
    assert lec.classify_trend(2, 0) == "STEADY"
    assert lec.classify_trend(3, 0) == "NEW"
    assert lec.classify_trend(30, 14) == "SPIKE"   # avg 2/d
    assert lec.classify_trend(5, 14) == "STEADY"


def test_section_trends_and_flags(tmp_path):
    ls = []
    ls += [_ln(NOW - timedelta(hours=2, minutes=i), "ERROR", "src.new", "boom") for i in range(4)]
    ls += [_ln(NOW - timedelta(days=2, hours=1), "WARNING", "src.sp", "slow") for _ in range(7)]
    ls += [_ln(NOW - timedelta(hours=3, minutes=i), "WARNING", "src.sp", "slow") for i in range(10)]
    _write(tmp_path / "a.log", sorted(ls))
    lines, flags = lec.build_log_errors(tmp_path, NOW)
    text = "\n".join(lines)
    assert flags == {"n_new": 1, "n_spike": 1}
    assert "[NEW]" in text and "[SPIKE]" in text and "[src.new] boom" in text


def test_unparseable_lines_tolerated(tmp_path):
    _write(tmp_path / "a.log", [
        "2026-13-99T99:99:99 ERROR [1] [x] bad ts\n",
        "2026garbage\n",
        "\xff\xfe binary\n",
        _ln(NOW - timedelta(hours=1), "ERROR", "src.a", "ok"),
    ])
    d = lec.collect(tmp_path, NOW)
    assert d["window"] == {("src.a", "ok"): 1}
    assert d["unparsed"] == 2


def test_missing_dir_and_empty(tmp_path):
    lines, flags = lec.build_log_errors(tmp_path / "nope", NOW)
    assert "no log files" in "\n".join(lines) and flags["n_new"] == 0


def test_pre_utc_lines_shifted_one_hour():
    ts = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
    parsed = lec.parse_line(_ln(ts, "ERROR", "x", "m"))
    assert parsed[0] == ts - timedelta(hours=1)
    # switch day itself is read as UTC
    ts2 = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
    assert lec.parse_line(_ln(ts2, "ERROR", "x", "m"))[0] == ts2


def test_utc_boundary_window_edge(tmp_path):
    now = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)  # window starts 13th 10:00 UTC
    # Host-local (WEST) lines on the 13th: 10:30 local == 09:30 UTC -> outside
    # the window (read as UTC it would wrongly be inside); 11:30 local == 10:30 UTC -> inside.
    _write(tmp_path / "a.log", [
        _ln(datetime(2026, 10, 13, 10, 30, tzinfo=timezone.utc), "ERROR", "x", "m"),
        _ln(datetime(2026, 10, 13, 11, 30, tzinfo=timezone.utc), "ERROR", "x", "m"),
    ])
    d = lec.collect(tmp_path, now)
    assert d["window"][("x", "m")] == 1
    assert d["baseline"][("x", "m")] == 1


def test_large_file_bound(tmp_path):
    old = NOW - timedelta(days=30)
    p = tmp_path / "big.log"
    with open(p, "w") as fh:
        for i in range(60000):  # ~ old filler, read never
            fh.write(_ln(old, "ERROR", "src.old", f"old {i}"))
        for i in range(5):
            fh.write(_ln(NOW - timedelta(minutes=i + 1), "ERROR", "src.cur", "cur"))
    t = time.time()
    d = lec.collect(tmp_path, NOW)
    assert time.time() - t < 5
    assert d["window"] == {("src.cur", "cur"): 5}
    assert d["baseline"] == {}
    assert d["truncated"] == []


def test_caps_report_truncation(tmp_path):
    p = tmp_path / "big.log"
    _write(p, [_ln(NOW - timedelta(minutes=1), "ERROR", "src.a", f"m{i % 2}x") for i in range(500)])
    d = lec.collect(tmp_path, NOW, max_lines=100)
    assert sum(d["window"].values()) == 100
    assert d["truncated"] == ["big.log"]
    d2 = lec.collect(tmp_path, NOW, max_bytes=2000)
    assert 0 < sum(d2["window"].values()) < 500
    assert d2["truncated"] == ["big.log"]


def test_block_boundaries_do_not_split_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(lec, "_BLOCK", 64)
    _write(tmp_path / "a.log", [_ln(NOW - timedelta(minutes=i + 1), "ERROR", "src.a", f"line {i} z") for i in range(40)])
    d = lec.collect(tmp_path, NOW)
    assert d["window"] == {("src.a", "line <n> z"): 40}


def test_verdict_hook_warns():
    v = _build_verdict([("log_errors", [], {"n_new": 1, "n_spike": 2})])
    assert "log errors: 1 new, 2 spiking" in "\n".join(v)
    assert "[OK]" in "\n".join(_build_verdict([("log_errors", [], {"n_new": 0, "n_spike": 0})]))
