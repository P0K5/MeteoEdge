"""Tests for issue #737: shadow NO-side price-semantics writer helper + backfill.

- ``_shadow_bought_side_cost_cents`` (writer): NO stores the NO ask (or the
  100-yes_ask fallback), YES stores the YES ask.
- ``backfill_shadow_no_price_737.backfill``: flips historical NO ``actual_price``
  (100 - stored), recomputes settled pnl from the sign, is idempotent, honors
  the ``--deploy-ts`` guard, leaves YES rows alone, and marks (without flipping)
  quarantined/excluded rows.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.data.db import Database
from src.scripts.run import _shadow_bought_side_cost_cents
from src.scripts.backfill_shadow_no_price_737 import backfill

DEPLOY_TS = "2026-07-19T00:00:00+00:00"
OLD_TS = "2026-07-10T10:00:00+00:00"
NEW_TS = "2026-07-20T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Writer helper (both sides, both fallbacks)
# ---------------------------------------------------------------------------

class TestShadowBoughtSideCost:
    def test_no_uses_no_ask(self):
        assert _shadow_bought_side_cost_cents("NO", 22, 78) == 78

    def test_no_fallback_when_no_ask_missing(self):
        assert _shadow_bought_side_cost_cents("NO", 22, None) == 78

    def test_no_fallback_when_no_ask_zero(self):
        assert _shadow_bought_side_cost_cents("NO", 22, 0) == 78

    def test_no_fallback_when_no_ask_negative(self):
        assert _shadow_bought_side_cost_cents("NO", 22, -5) == 78

    def test_no_fallback_clamped_floor(self):
        # yes_ask=99 → 100-99 = 1 (clamped to the [1, 99] floor)
        assert _shadow_bought_side_cost_cents("NO", 99, None) == 1

    def test_yes_uses_yes_ask(self):
        assert _shadow_bought_side_cost_cents("YES", 72, 30) == 72

    def test_yes_ignores_no_ask(self):
        assert _shadow_bought_side_cost_cents("YES", 72, None) == 72


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def _seed(db, *, station, side, actual_price, ts, bl, bh,
          pnl=None, settled_at=None, capital_after=None, outcome=None):
    return db.insert_trade(
        ts=ts, station=station, ticker=f"T-{station}-{side}",
        bracket_low=bl, bracket_high=bh, side=side,
        predicted_price=int(actual_price * 0.9), actual_price=actual_price,
        predicted_edge=10.0, mode="shadow", capital_before=0.0,
        order_id=None, outcome=outcome, pnl=pnl, capital_after=capital_after,
        settled_at=settled_at,
    )


def _row(db, tid):
    cur = db._conn.execute(
        "SELECT actual_price, pnl, capital_after, price_semantics_fixed "
        "FROM trades WHERE id=?",
        (tid,),
    )
    return cur.fetchone()


@pytest.fixture
def seeded(tmp_path):
    dbp = tmp_path / "t.db"
    db = Database(str(dbp))
    ids = {}
    # old-convention NO settled WIN: stored yes_ask=22, old pnl = (100-22)/100 = +0.78
    ids["no_win"] = _seed(db, station="KORD", side="NO", actual_price=22, ts=OLD_TS,
                          bl=80, bh=81, pnl=0.78, capital_after=0.78,
                          settled_at="2026-07-11T00:00:00+00:00", outcome="filled")
    # old-convention NO settled LOSS: stored 22, old pnl = -22/100 = -0.22
    ids["no_loss"] = _seed(db, station="KMIA", side="NO", actual_price=22, ts=OLD_TS,
                           bl=80, bh=81, pnl=-0.22, capital_after=-0.22,
                           settled_at="2026-07-11T00:00:00+00:00", outcome="filled")
    # old-convention NO unsettled: stored 22, pnl NULL (settled later by #742/settle)
    ids["no_unsettled"] = _seed(db, station="KSEA", side="NO", actual_price=22, ts=OLD_TS,
                                bl=70, bh=71)
    # YES row: must be left untouched by the NO-only backfill
    ids["yes"] = _seed(db, station="KLGA", side="YES", actual_price=72, ts=OLD_TS, bl=60, bh=61)
    # NO post-deploy row already carrying the correct NO cost (distinct value 80):
    # the deploy-ts guard must skip it (else it would wrongly flip to 20)
    ids["no_postdeploy"] = _seed(db, station="KBOS", side="NO", actual_price=80, ts=NEW_TS,
                                 bl=50, bh=51)
    # NO quarantined (settled but pnl NULL, e.g. #610): mark, do NOT flip/resurrect
    ids["no_quarantined"] = _seed(db, station="KDEN", side="NO", actual_price=22, ts=OLD_TS,
                                  bl=40, bh=41, settled_at="2026-07-11T00:00:00+00:00",
                                  outcome="filled")
    db.close()
    return dbp, ids


class TestBackfill737:
    def _run(self, dbp):
        backfill(Path(str(dbp)), DEPLOY_TS, dry_run=False)

    def test_settled_win_flipped_and_recomputed(self, seeded):
        dbp, ids = seeded
        self._run(dbp)
        db = Database(str(dbp))
        r = _row(db, ids["no_win"])
        assert r["actual_price"] == 78
        assert abs(r["pnl"] - 0.22) < 1e-9
        assert abs(r["capital_after"] - 0.22) < 1e-9
        assert r["price_semantics_fixed"] == 1
        db.close()

    def test_settled_loss_flipped_and_recomputed(self, seeded):
        dbp, ids = seeded
        self._run(dbp)
        db = Database(str(dbp))
        r = _row(db, ids["no_loss"])
        assert r["actual_price"] == 78
        assert abs(r["pnl"] - (-0.78)) < 1e-9
        assert abs(r["capital_after"] - (-0.78)) < 1e-9
        db.close()

    def test_unsettled_price_flipped_pnl_untouched(self, seeded):
        dbp, ids = seeded
        self._run(dbp)
        db = Database(str(dbp))
        r = _row(db, ids["no_unsettled"])
        assert r["actual_price"] == 78
        assert r["pnl"] is None
        assert r["price_semantics_fixed"] == 1
        db.close()

    def test_yes_row_untouched(self, seeded):
        dbp, ids = seeded
        self._run(dbp)
        db = Database(str(dbp))
        r = _row(db, ids["yes"])
        assert r["actual_price"] == 72  # unchanged
        assert r["price_semantics_fixed"] == 0
        db.close()

    def test_deploy_ts_guard_skips_post_deploy_rows(self, seeded):
        dbp, ids = seeded
        self._run(dbp)
        db = Database(str(dbp))
        r = _row(db, ids["no_postdeploy"])
        assert r["actual_price"] == 80  # NOT flipped to 20
        assert r["price_semantics_fixed"] == 0
        db.close()

    def test_quarantined_row_marked_not_flipped(self, seeded):
        dbp, ids = seeded
        self._run(dbp)
        db = Database(str(dbp))
        r = _row(db, ids["no_quarantined"])
        assert r["actual_price"] == 22  # price NOT flipped
        assert r["pnl"] is None          # pnl NOT resurrected
        assert r["price_semantics_fixed"] == 1  # but marked done
        db.close()

    def test_idempotent_second_run_is_noop(self, seeded):
        dbp, ids = seeded
        self._run(dbp)
        self._run(dbp)  # second run must not double-flip
        db = Database(str(dbp))
        r = _row(db, ids["no_win"])
        assert r["actual_price"] == 78
        assert abs(r["pnl"] - 0.22) < 1e-9
        db.close()

    def test_dry_run_changes_nothing(self, seeded):
        dbp, ids = seeded
        backfill(Path(str(dbp)), DEPLOY_TS, dry_run=True)
        db = Database(str(dbp))
        r = _row(db, ids["no_win"])
        assert r["actual_price"] == 22  # unchanged in dry-run
        db.close()
