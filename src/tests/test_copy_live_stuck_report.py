"""Issue #1393: live stuck-position health-report section (read-only)."""
from datetime import datetime, timedelta, timezone

from src.data.db import Database
from src.scripts.daily_health_report import (
    LIVE_PENDING_STALE_HOURS,
    _build_copy_live_stuck,
    _build_verdict,
)

W = "0xlive1"


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _seed(db, *, status="filled", hours_ago=60.0, stake=5.0, level=0, cat=None,
          market="0xm", reason=None, order_id=None):
    sid = db.insert_copy_signal(address=W, market=market, source_price=0.4,
                                detected_at=_iso(hours_ago), outcome_index=0)
    pid = db.insert_copy_live_position(signal_id=sid, address=W, market=market,
                                       outcome_index=0, stake_usd=stake,
                                       entry_ts=_iso(hours_ago), order_id=order_id)
    with db._lock:
        db._conn.execute(
            "UPDATE copy_live_positions SET status=?, rejected_reason=?, stuck_level=?, "
            "stuck_category=?, stuck_first_seen_at=? WHERE id=?",
            (status, reason, level, cat, _iso(1) if level else None, pid),
        )
        db._conn.commit()
    return pid


def _run(db):
    return _build_copy_live_stuck(db, datetime.now(timezone.utc))


def test_empty_table_is_clean_and_shows_exposure_vs_cap():
    lines, flags = _run(Database(":memory:"))
    text = "\n".join(lines)
    assert flags == {"n_stuck": 0, "n_urgent": 0, "n_pending_stale": 0, "n_ghost": 0}
    assert "Unsettled live exposure counted: $0.00 of cap $250.00" in text
    assert "[WARN]" not in text


def test_lists_stuck_rows_and_counts_stake_and_urgent():
    db = Database(":memory:")
    a = _seed(db, level=1, cat="PAST_END_UNRESOLVED", stake=5.0)
    b = _seed(db, level=2, cat="UNRETRIEVABLE", stake=7.5, status="partial", market="0xn")
    _seed(db, level=0, stake=100.0, market="0xhealthy")
    lines, flags = _run(db)
    text = "\n".join(lines)
    assert flags["n_stuck"] == 2 and flags["n_urgent"] == 1
    assert "stake unresolved: $12.50" in text
    assert f"[PAST_END_UNRESOLVED/AGED_48H] id={a} wallet={W} market=0xm" in text
    assert f"[UNRETRIEVABLE/PATHOLOGICAL_7D] id={b}" in text
    assert "0xhealthy" not in text
    # all three open rows count toward exposure (same query as the order gate)
    assert "$112.50 of cap $250.00" in text


def test_stale_pending_separate_and_fresh_pending_ignored():
    db = Database(":memory:")
    stale = _seed(db, status="pending", hours_ago=LIVE_PENDING_STALE_HOURS + 1)
    _seed(db, status="pending", hours_ago=0.1, market="0xfresh")
    lines, flags = _run(db)
    text = "\n".join(lines)
    assert flags["n_pending_stale"] == 1 and flags["n_stuck"] == 0
    assert f"[STALE_PENDING] id={stale}" in text
    assert "0xfresh" not in text


def test_ghost_listed_and_counted_in_exposure_and_resolved_ghost_gone():
    db = Database(":memory:")
    g = _seed(db, status="rejected", reason="cancel_failed_ghost", order_id="ord1",
              hours_ago=5, stake=9.0)
    _seed(db, status="rejected", reason="cancel_confirmed_zero_fill", order_id="ord2",
          market="0xdead")
    lines, flags = _run(db)
    text = "\n".join(lines)
    assert flags["n_ghost"] == 1
    assert f"[GHOST_UNRESOLVED] id={g}" in text and "0xdead" not in text
    assert "counted: $9.00" in text and "ghosts are NOT counted" not in text
    assert "1 unresolved ghost(s)" in text


def test_exposure_over_cap_warns():
    db = Database(":memory:")
    for i in range(3):
        _seed(db, stake=100.0, market=f"0x{i}")
    text = "\n".join(_run(db)[0])
    assert "$300.00 of cap $250.00" in text and "above COPY_LIVE_MAX_TOTAL_EXPOSURE_USD" in text


def test_query_error_degrades_to_unavailable():
    class Boom:
        def get_open_copy_live_positions(self):
            raise RuntimeError("x")
    lines, _ = _build_copy_live_stuck(Boom(), datetime.now(timezone.utc))
    assert any("unavailable" in line for line in lines)


def test_read_only_no_rows_modified():
    db = Database(":memory:")
    _seed(db, level=2, cat="UNRETRIEVABLE")
    _seed(db, status="pending", hours_ago=10, market="0xp")
    before = [tuple(r) for r in db._conn.execute("SELECT * FROM copy_live_positions ORDER BY id")]
    _run(db)
    after = [tuple(r) for r in db._conn.execute("SELECT * FROM copy_live_positions ORDER BY id")]
    assert before == after


def test_verdict_warns_on_each_category_and_not_when_clean():
    def v(flags):
        return "\n".join(_build_verdict([("copy_live_stuck", ["x"], flags)]))
    assert "[OK]" in v({"n_stuck": 0, "n_pending_stale": 0, "n_ghost": 0})
    assert "1 stuck" in v({"n_stuck": 1})
    assert "2 stale pending" in v({"n_pending_stale": 2})
    assert "1 unresolved ghost" in v({"n_ghost": 1})
