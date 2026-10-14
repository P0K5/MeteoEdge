"""Issue #1314: skip-reason aggregation, classification and health section."""
from datetime import datetime, timedelta, timezone

import pytest

from src.data.copy_skip_reasons import (
    BREAKER, LIFECYCLE, ROUTINE, STRUCTURAL, UNKNOWN, SKIP_REASONS,
    classify, trend,
)
from src.data.db import Database
from src.scripts.daily_health_report import _build_copy_skips, _copy_skip_counts, _build_verdict

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def _sig(db, addr, reason, age_h, placed=False):
    db.insert_copy_signal(
        address=addr, market="m", outcome_index=0, source_price=0.5,
        source_trade_id=None, detected_at=(NOW - timedelta(hours=age_h)).isoformat(),
        order_placed=1 if placed else 0,
        fill_price=0.5 if placed else None, size_usd=1.0 if placed else None,
        skip_reason=None if placed else reason,
    )


def test_classify():
    assert classify("non_binary_outcome_index") == STRUCTURAL
    assert classify("below_min_entry_price") == ROUTINE
    assert classify("wallet_newly_followed") == LIFECYCLE
    assert classify("circuit_breaker_daily_loss") == BREAKER
    assert classify("live_circuit_breaker_x") == BREAKER
    assert classify("typo_reason") == UNKNOWN


def test_vocabulary_matches_loop_reasons():
    import re
    src = open("src/scripts/copy_signal_loop.py").read()
    used = set(re.findall(r'skip_reason = "([a-z_]+)"', src))
    used -= {r for r in used if r.startswith("live_")}
    assert used <= set(SKIP_REASONS), used - set(SKIP_REASONS)


def test_trend():
    assert trend(4, 0) == "STEADY"      # below MIN_COUNT
    assert trend(30, 0) == "NEW"
    assert trend(30, 210) == "STEADY"   # 30/day baseline
    assert trend(30, 7) == "SPIKE"      # 1/day baseline


def test_counts_window_and_baseline(db):
    for _ in range(3):
        _sig(db, "0xa", "below_min_entry_price", 2)
    for _ in range(14):
        _sig(db, "0xa", "below_min_entry_price", 72)
    _sig(db, "0xa", None, 1, placed=True)
    _sig(db, "0xa", "below_min_entry_price", 24 * 12)  # outside baseline
    out = _copy_skip_counts(db, NOW)
    assert out["0xa"]["placed"] == 1
    assert out["0xa"]["skipped"]["below_min_entry_price"] == (3, 14)


def test_section_structural_and_verdict(db):
    for _ in range(30):
        _sig(db, "0xbca08c1b", "non_binary_outcome_index", 3)
    _sig(db, "0xbca08c1b", None, 1, placed=True)
    lines, flags = _build_copy_skips(db, NOW)
    text = "\n".join(lines)
    assert "placed=1 skipped=30" in text
    assert "structural" in text and "NEW" in text
    assert flags["n_structural"] == 30
    verdict = "\n".join(_build_verdict([("copy_skips", lines, flags)]))
    assert "30 structural" in verdict


def test_section_empty_and_routine_no_verdict(db):
    lines, flags = _build_copy_skips(db, NOW)
    assert "no skipped signals" in "\n".join(lines)
    for _ in range(10):
        _sig(db, "0xb", "below_min_entry_price", 3)
        _sig(db, "0xb", "below_min_entry_price", 48)
    lines, flags = _build_copy_skips(db, NOW)
    assert flags["n_structural"] == 0
    assert "OK" in "\n".join(_build_verdict([("copy_skips", lines, flags)]))


def test_section_query_error_degrades():
    class Bad:
        pass
    lines, flags = _build_copy_skips(Bad(), NOW)
    assert "unavailable" in "\n".join(lines)
