"""Tests for src/scripts/audit_multi_fill_groups.py (issue #993, PR 1 -- audit only).

Covers:
- find_multi_fill_groups(): finds the same multi-fill groups the #993 issue-body
  query targets, and only those (single-fill tickers are excluded).
- classify_group(): exactly-2-rows-within-threshold -> reprice_artifact,
  everything else (3+ rows, or 2 rows spaced further apart) -> bracket_stacking.
- compute_pnl_impact() / compute_settled_stats(): pure aggregation over rows.
- run_report() is read-only: it never issues an INSERT/UPDATE/DELETE against
  the DB it's pointed at.
"""
import sqlite3

import pytest

from src.scripts.audit_multi_fill_groups import (
    REPRICE_MAX_GAP_MINUTES,
    classify_group,
    compute_pnl_impact,
    compute_settled_stats,
    find_multi_fill_groups,
    run_report,
)

_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    station TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    order_id TEXT,
    outcome TEXT,
    pnl REAL,
    actual_price INTEGER,
    size_eur REAL,
    mode TEXT NOT NULL DEFAULT 'live'
);
"""


def _make_db(tmp_path):
    path = tmp_path / "audit_test.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    return conn, path


def _insert(conn, **kw):
    defaults = {
        "ts": "2026-08-11T11:42:29.189644Z", "station": "KORD", "ticker": "0xabc",
        "side": "NO", "order_id": "ord-1", "outcome": "filled", "pnl": None,
        "actual_price": 70, "size_eur": 5.0, "mode": "live",
    }
    defaults.update(kw)
    conn.execute(
        "INSERT INTO trades (ts, station, ticker, side, order_id, outcome, pnl, "
        "actual_price, size_eur, mode) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (defaults["ts"], defaults["station"], defaults["ticker"], defaults["side"],
         defaults["order_id"], defaults["outcome"], defaults["pnl"],
         defaults["actual_price"], defaults["size_eur"], defaults["mode"]),
    )
    conn.commit()


class TestFindMultiFillGroups:
    def test_single_fill_ticker_not_returned(self, tmp_path):
        conn, _ = _make_db(tmp_path)
        _insert(conn, ticker="0xsingle", outcome="filled")
        groups = find_multi_fill_groups(conn)
        assert groups == []

    def test_two_filled_rows_same_day_station_ticker_side_grouped(self, tmp_path):
        conn, _ = _make_db(tmp_path)
        _insert(conn, ticker="0xpair", order_id="ord-a",
                ts="2026-08-11T11:42:29Z", outcome="sold", pnl=0.0, size_eur=0.0)
        _insert(conn, ticker="0xpair", order_id="ord-b",
                ts="2026-08-11T11:47:31Z", outcome="sold", pnl=1.2, size_eur=5.0)
        groups = find_multi_fill_groups(conn)
        assert len(groups) == 1
        assert groups[0]["n"] == 2
        assert len(groups[0]["rows"]) == 2

    def test_timeout_only_rows_not_counted(self, tmp_path):
        """A group where none of the rows are filled/sold must not appear --
        the HAVING clause only counts filled/sold outcomes."""
        conn, _ = _make_db(tmp_path)
        _insert(conn, ticker="0xtimeouts", order_id="ord-t1", outcome="timeout")
        _insert(conn, ticker="0xtimeouts", order_id="ord-t2", outcome="timeout")
        groups = find_multi_fill_groups(conn)
        assert groups == []

    def test_paper_mode_excluded(self, tmp_path):
        conn, _ = _make_db(tmp_path)
        _insert(conn, ticker="0xpaper", order_id="ord-p1", mode="paper", outcome="filled")
        _insert(conn, ticker="0xpaper", order_id="ord-p2", mode="paper", outcome="filled")
        groups = find_multi_fill_groups(conn)
        assert groups == []


class TestClassifyGroup:
    def _group(self, timestamps):
        return {"rows": [{"ts": ts} for ts in timestamps]}

    def test_two_rows_five_minutes_apart_is_reprice_artifact(self):
        g = self._group(["2026-08-11T11:42:29Z", "2026-08-11T11:47:31Z"])
        assert classify_group(g) == "reprice_artifact"

    def test_two_rows_at_exact_threshold_is_reprice_artifact(self):
        g = self._group(["2026-08-11T11:00:00Z", "2026-08-11T11:07:30Z"])  # 7.5 min
        assert classify_group(g, max_gap_minutes=7.5) == "reprice_artifact"

    def test_two_rows_just_over_threshold_is_stacking(self):
        g = self._group(["2026-08-11T11:00:00Z", "2026-08-11T11:07:31Z"])  # 7.517 min
        assert classify_group(g, max_gap_minutes=7.5) == "bracket_stacking"

    def test_two_rows_ten_minutes_apart_is_stacking(self):
        g = self._group(["2026-08-11T11:00:00Z", "2026-08-11T11:10:00Z"])
        assert classify_group(g) == "bracket_stacking"

    def test_three_rows_ten_minutes_apart_each_is_stacking_not_reprice(self):
        """Even though each individual gap resembles nothing like a reprice,
        3+ rows can never be a reprice artifact (#743 does exactly one retry)."""
        g = self._group([
            "2026-06-08T10:00:00Z", "2026-06-08T10:11:00Z", "2026-06-08T10:22:00Z",
        ])
        assert classify_group(g) == "bracket_stacking"

    def test_default_threshold_matches_fill_max_wait_s_derived_value(self):
        assert REPRICE_MAX_GAP_MINUTES == pytest.approx(7.5)


class TestComputePnlImpact:
    def test_sums_pnl_and_size_and_flags_zero_size_rows(self):
        groups = [{
            "rows": [
                {"id": 1696, "pnl": 0.0, "size_eur": 0.0},
                {"id": 1697, "pnl": 1.1033, "size_eur": 4.9973},
            ],
        }]
        impact = compute_pnl_impact(groups)
        assert impact["n_rows"] == 2
        assert impact["n_groups"] == 1
        assert impact["total_pnl"] == pytest.approx(1.1033)
        assert impact["total_size_eur"] == pytest.approx(4.9973)
        assert impact["n_zero_size_rows"] == 1
        assert impact["zero_size_row_ids"] == [1696]

    def test_empty_groups_gives_zeroed_report(self):
        impact = compute_pnl_impact([])
        assert impact["n_rows"] == 0
        assert impact["total_pnl"] == 0.0
        assert impact["zero_size_row_ids"] == []


class TestComputeSettledStats:
    def test_excluding_ids_reduces_trade_count_and_recomputes_win_rate(self, tmp_path):
        conn, _ = _make_db(tmp_path)
        _insert(conn, ticker="0xa", order_id="ord-1", outcome="sold", pnl=0.0)  # phantom leg
        _insert(conn, ticker="0xa", order_id="ord-2", outcome="sold", pnl=1.5)  # real win
        _insert(conn, ticker="0xb", order_id="ord-3", outcome="sold", pnl=-0.5)  # real loss
        ids = [r[0] for r in conn.execute("SELECT id FROM trades ORDER BY id").fetchall()]
        phantom_id = ids[0]

        stats = compute_settled_stats(conn, exclude_ids={phantom_id})
        assert stats["before"]["n_trades"] == 3
        assert stats["before"]["n_wins"] == 1
        assert stats["after"]["n_trades"] == 2
        assert stats["after"]["n_wins"] == 1
        assert stats["after"]["win_rate"] == pytest.approx(0.5)


class TestRunReportIsReadOnly:
    def test_run_report_does_not_mutate_the_db(self, tmp_path, capsys):
        conn, path = _make_db(tmp_path)
        _insert(conn, ticker="0xro", order_id="ord-ro-1",
                ts="2026-08-11T11:42:29Z", outcome="sold", pnl=0.0, size_eur=0.0)
        _insert(conn, ticker="0xro", order_id="ord-ro-2",
                ts="2026-08-11T11:47:31Z", outcome="sold", pnl=1.2, size_eur=5.0)
        conn.close()

        before = path.read_bytes()
        run_report(str(path))
        after = path.read_bytes()

        assert before == after, "run_report() must never write to the audited DB"
        out = capsys.readouterr().out
        assert "Reprice-artifact groups (1)" in out
