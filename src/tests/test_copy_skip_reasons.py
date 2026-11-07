"""Issue #1314: skip-reason aggregation and classification.

The health-report section that rendered these counts (``_build_copy_skips``/
``_copy_skip_counts`` in ``daily_health_report.py``) was removed by #1501
(copy-trading wind-down); the aggregation/classification module itself
(``src/data/copy_skip_reasons.py``) is kept per #1501's explicit scope
(data/code removal is #1502's job), so its direct unit tests stay.
"""
from src.data.copy_skip_reasons import (
    BREAKER, LIFECYCLE, ROUTINE, STRUCTURAL, UNKNOWN, SKIP_REASONS,
    classify, trend, accepted_ref, ACCEPTED_STRUCTURAL,
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


def test_accepted_ref():
    assert accepted_ref("0xbca08c1b99", "non_binary_outcome_index") == "#1390"
    assert accepted_ref("0xother", "non_binary_outcome_index") is None
    assert accepted_ref("0xbca08c1b99", "missing_outcome_index") is None
    assert all(classify(r) == STRUCTURAL for _, r in ACCEPTED_STRUCTURAL)
