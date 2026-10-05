"""Live drawdown breaker measured from a baseline timestamp (issue #1317).

Closing a losing phase with COPY_LIVE_DRAWDOWN_SINCE must stop those losses
counting toward the stop, but a new drawdown past the stop must still block.
"""
from unittest.mock import MagicMock

from src.risk.copy_risk_manager import allow_live_copy_signal

_CFG = {
    "COPY_LIVE_DAILY_LOSS_LIMIT_USD": 25.0,
    "COPY_LIVE_DRAWDOWN_STOP_PCT": 0.20,
    "COPY_LIVE_DRAWDOWN_SINCE": "",
}


def _db(total_pnl: float):
    db = MagicMock()
    db.get_copy_live_realized_pnl_total_for_date.return_value = {"n_settled": 0, "total_pnl_usd": 0.0}
    db.get_copy_live_realized_pnl_total.return_value = {"n_settled": 1, "total_pnl_usd": total_pnl}
    return db


def _cfg(**overrides):
    cfg = dict(_CFG)
    cfg.update(overrides)
    return cfg


def test_no_baseline_counts_all_losses_and_blocks(monkeypatch):
    import src.risk.copy_risk_manager as m
    monkeypatch.setattr(m, "COPY_LIVE_CAPITAL_USD", 40.0)
    db = _db(-20.0)
    ok, reason = allow_live_copy_signal(db, _cfg())
    assert ok is False
    db.get_copy_live_realized_pnl_total.assert_called_with(since=None)


def test_baseline_after_losses_allows_trading(monkeypatch):
    import src.risk.copy_risk_manager as m
    monkeypatch.setattr(m, "COPY_LIVE_CAPITAL_USD", 40.0)
    db = _db(0.0)  # losses before the baseline are excluded by the query
    ok, _ = allow_live_copy_signal(db, _cfg(COPY_LIVE_DRAWDOWN_SINCE="2026-10-05T00:00:00+00:00"))
    assert ok is True
    db.get_copy_live_realized_pnl_total.assert_called_with(since="2026-10-05T00:00:00+00:00")


def test_new_drawdown_after_baseline_still_blocks(monkeypatch):
    import src.risk.copy_risk_manager as m
    monkeypatch.setattr(m, "COPY_LIVE_CAPITAL_USD", 40.0)
    db = _db(-9.0)  # 9/40 = 22.5% >= 20%, all after the baseline
    ok, reason = allow_live_copy_signal(db, _cfg(COPY_LIVE_DRAWDOWN_SINCE="2026-10-05T00:00:00+00:00"))
    assert ok is False
    assert reason


def test_unparseable_baseline_falls_back_to_all_time(monkeypatch):
    import src.risk.copy_risk_manager as m
    monkeypatch.setattr(m, "COPY_LIVE_CAPITAL_USD", 40.0)
    db = _db(-20.0)
    ok, _ = allow_live_copy_signal(db, _cfg(COPY_LIVE_DRAWDOWN_SINCE="not-a-date"))
    assert ok is False
    db.get_copy_live_realized_pnl_total.assert_called_with(since=None)


def _settled_live(db, *, market: str, pnl: float, settled_at: str) -> None:
    from src.tests.test_copy_live_settle import _seed_live_position
    pos_id = _seed_live_position(db, market=market, status="filled", stake_usd=5.0)
    db._conn.execute(
        "UPDATE copy_live_positions SET status='settled', settled_pnl_usd=?, settled_at=? WHERE id=?",
        (pnl, settled_at, pos_id),
    )
    db._conn.commit()


def test_db_query_excludes_settlements_before_baseline():
    from src.data.db import Database
    db = Database(":memory:")
    _settled_live(db, market="0xold", pnl=-20.0, settled_at="2026-10-04T13:00:00+00:00")
    _settled_live(db, market="0xnew", pnl=-3.0, settled_at="2026-10-05T10:00:00+00:00")
    assert db.get_copy_live_realized_pnl_total()["total_pnl_usd"] == -23.0
    assert db.get_copy_live_realized_pnl_total(since="2026-10-05T00:00:00+00:00")["total_pnl_usd"] == -3.0
