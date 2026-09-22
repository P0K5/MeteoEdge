"""Unit tests for src/scripts/copy_live_settle.py (epic I #1160, issue
#1174). Uses a real in-memory Database (seeded rows), mirroring
test_copy_settle.py's `Database(":memory:")` pattern, with
`fetch_market_resolution` and `LiveTrader` patched -- no real network/CLOB
calls.

Per the issue's own instruction ("write the drift-detection test first"),
TestWalletBalanceDriftDetection is the first test class in this file.
"""
from unittest.mock import MagicMock, patch

from src.data.db import Database
from src.scripts import copy_live_settle as cls

ADDRESS = "0xwallet1"
NOW_ISO = "2026-09-20T00:00:00+00:00"


def _seed_signal(db: Database, *, market: str, address: str = ADDRESS, **overrides) -> int:
    kwargs = dict(
        address=address, market=market, source_price=0.40, detected_at=NOW_ISO,
        outcome_index=0,
    )
    kwargs.update(overrides)
    return db.insert_copy_signal(**kwargs)


def _seed_live_position(
    db: Database, *, market: str, status: str, address: str = ADDRESS,
    outcome_index: int = 0, stake_usd: float = 10.0, fill_price: "float | None" = 0.40,
    filled_stake_usd: "float | None" = None, order_id: "str | None" = "0xorder1",
    rejected_reason: "str | None" = None,
) -> int:
    """Insert one copy_live_positions row (via its required copy_signals FK
    parent first) already transitioned to *status*, and return its id."""
    signal_id = _seed_signal(db, market=market, address=address, outcome_index=outcome_index)
    position_id = db.insert_copy_live_position(
        signal_id=signal_id, address=address, market=market,
        outcome_index=outcome_index, stake_usd=stake_usd, entry_ts=NOW_ISO,
    )
    if status != "pending":
        db.update_copy_live_position_status(
            position_id, status, order_id=order_id, fill_price=fill_price,
            rejected_reason=rejected_reason, filled_stake_usd=filled_stake_usd,
        )
    return position_id


class TestWalletBalanceDriftDetection:
    """The actual "reconciliation" this epic is named for -- written first,
    per the issue's own instruction."""

    def _factory(self, balance: float):
        trader = MagicMock()
        trader.get_usdc_balance.return_value = balance
        factory = MagicMock(return_value=MagicMock())
        return factory, trader

    def test_drift_within_tolerance_is_not_flagged_and_not_critical(self, caplog):
        db = Database(":memory:")
        # committed: one 'filled' (no filled_stake_usd -> falls back to
        # stake_usd=20) + one 'partial' with filled_stake_usd=4 -> 24 total.
        _seed_live_position(db, market="0xm1", status="filled", stake_usd=20.0)
        _seed_live_position(db, market="0xm2", status="partial", stake_usd=10.0, filled_stake_usd=4.0)
        _seed_live_position(db, market="0xm3", status="pending", stake_usd=999.0)  # never counted
        # realized: one settled position, pnl=5.0
        settled_id = _seed_live_position(db, market="0xm4", status="filled", stake_usd=5.0)
        db.settle_copy_live_position(settled_id, 5.0, NOW_ISO)

        # expected = 100 (capital) - 24 (committed) + 5 (realized) = 81
        factory, trader = self._factory(balance=81.0)
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             caplog.at_level("CRITICAL"):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result == {
            "expected_balance_usd": 81.0,
            "actual_balance_usd": 81.0,
            "drift_usd": 0.0,
            "within_tolerance": True,
        }
        assert not any("CRITICAL" in rec.message for rec in caplog.records)

    def test_drift_beyond_tolerance_is_flagged_loudly(self, caplog):
        db = Database(":memory:")
        _seed_live_position(db, market="0xm1", status="filled", stake_usd=20.0)

        # expected = 100 - 20 + 0 = 80; actual = 100 -> drift = 20, way beyond
        # the default $2.00 tolerance.
        factory, trader = self._factory(balance=100.0)
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             caplog.at_level("CRITICAL"):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result["within_tolerance"] is False
        assert result["drift_usd"] == 20.0
        assert any(
            "CRITICAL" in rec.message and "WALLET BALANCE DRIFT" in rec.message
            for rec in caplog.records
        )

    def test_drift_exactly_at_tolerance_boundary_is_not_flagged(self):
        db = Database(":memory:")
        db.set_config("COPY_LIVE_BALANCE_DRIFT_TOLERANCE_USD", "1.0")

        factory, trader = self._factory(balance=99.0)  # expected=100, drift=-1.0
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result["within_tolerance"] is True

    def test_skips_entirely_when_no_real_capital_allocated(self):
        """COPY_LIVE_CAPITAL_USD<=0 (the safety default) -- must skip
        without ever calling the CLOB, mirroring COPY_LIVE_TRADING_ENABLED's
        own inert-by-default precedent."""
        db = Database(":memory:")
        factory = MagicMock()
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 0.0):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result is None
        factory.assert_not_called()

    def test_clob_failure_returns_none_without_raising(self, caplog):
        """A network/auth blip fetching the real balance must never crash
        the whole hourly run -- this is one of three independent things
        run_once does."""
        db = Database(":memory:")
        factory = MagicMock(return_value=MagicMock())
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", side_effect=RuntimeError("CLOB unreachable")):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result is None

    def test_partial_fill_uses_filled_stake_usd_not_full_stake(self):
        """A 'partial' row's committed exposure must use filled_stake_usd,
        never the full originally-intended stake_usd -- the same #1171
        item 3 precision this epic's settlement also depends on."""
        db = Database(":memory:")
        _seed_live_position(
            db, market="0xm1", status="partial", stake_usd=50.0, filled_stake_usd=3.0,
        )

        # If this wrongly used the full stake_usd=50, expected would be 50;
        # using filled_stake_usd=3.0 it's 97.
        factory, trader = self._factory(balance=97.0)
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result["expected_balance_usd"] == 97.0
        assert result["within_tolerance"] is True


class TestSettleLivePositions:
    """Mirrors test_copy_settle.py's own settlement tests, one layer up."""

    def test_no_unsettled_positions_returns_zero_summary(self):
        db = Database(":memory:")
        with patch.object(cls, "fetch_market_resolution") as mock_resolve:
            summary = cls._settle_live_positions(db)
        assert summary == {"settled": 0, "pending": 0, "errors": 0}
        mock_resolve.assert_not_called()

    def test_yes_no_and_unresolved_positions(self):
        db = Database(":memory:")
        yes_id = _seed_live_position(db, market="0xyesmarket", status="filled", stake_usd=10.0, fill_price=0.40)
        no_id = _seed_live_position(db, market="0xnomarket", status="filled", stake_usd=10.0, fill_price=0.40)
        pending_market_id = _seed_live_position(
            db, market="0xpendingmarket", status="filled", stake_usd=10.0, fill_price=0.40,
        )

        def fake_resolve(market):
            return {"0xyesmarket": True, "0xnomarket": False, "0xpendingmarket": None}[market]

        with patch.object(cls, "fetch_market_resolution", side_effect=fake_resolve):
            summary = cls._settle_live_positions(db)

        assert summary == {"settled": 2, "pending": 1, "errors": 0}

        rows = {r["id"]: r for r in db._conn.execute("SELECT * FROM copy_live_positions").fetchall()}
        assert rows[yes_id]["status"] == "settled"
        assert rows[yes_id]["settled_pnl_usd"] == 15.0  # 10*(1-0.4)/0.4
        assert rows[no_id]["status"] == "settled"
        assert rows[no_id]["settled_pnl_usd"] == -10.0
        assert rows[pending_market_id]["status"] == "filled"  # unresolved -> stays as-is
        assert rows[pending_market_id]["settled_pnl_usd"] is None

    def test_pending_and_rejected_rows_are_never_settled(self):
        """get_unsettled_copy_live_positions already excludes these, but
        this asserts the end-to-end behavior stays correct."""
        db = Database(":memory:")
        pending_id = _seed_live_position(db, market="0xm1", status="pending", stake_usd=10.0)
        rejected_id = _seed_live_position(
            db, market="0xm1", status="rejected", rejected_reason="timeout",
        )

        with patch.object(cls, "fetch_market_resolution", return_value=True):
            summary = cls._settle_live_positions(db)

        assert summary == {"settled": 0, "pending": 0, "errors": 0}
        pending_row = db._conn.execute(
            "SELECT status FROM copy_live_positions WHERE id=?", (pending_id,)
        ).fetchone()
        assert pending_row["status"] == "pending"
        rejected_row = db._conn.execute(
            "SELECT status FROM copy_live_positions WHERE id=?", (rejected_id,)
        ).fetchone()
        assert rejected_row["status"] == "rejected"

    def test_partial_fill_pnl_uses_filled_stake_usd_not_full_stake(self):
        """Issue #1171 item 3: a partial fill's P&L must be computed from
        what actually filled, not the full intended stake."""
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="partial", stake_usd=50.0,
            filled_stake_usd=4.0, fill_price=0.40,
        )

        with patch.object(cls, "fetch_market_resolution", return_value=True):  # wins
            summary = cls._settle_live_positions(db)

        assert summary == {"settled": 1, "pending": 0, "errors": 0}
        row = db._conn.execute(
            "SELECT settled_pnl_usd FROM copy_live_positions WHERE id=?", (position_id,)
        ).fetchone()
        # If this wrongly used stake_usd=50: pnl = 50*(1-0.4)/0.4 = 75.
        # Using filled_stake_usd=4.0: pnl = 4*(1-0.4)/0.4 = 6.0.
        assert row["settled_pnl_usd"] == 6.0

    def test_filled_row_without_filled_stake_usd_falls_back_to_stake_usd(self):
        """A 'filled' row (never partial) has no filled_stake_usd -- P&L
        must fall back to the full stake_usd, not silently compute pnl=0
        or raise."""
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="filled", stake_usd=10.0, fill_price=0.40,
        )
        with patch.object(cls, "fetch_market_resolution", return_value=True):
            cls._settle_live_positions(db)

        row = db._conn.execute(
            "SELECT settled_pnl_usd FROM copy_live_positions WHERE id=?", (position_id,)
        ).fetchone()
        assert row["settled_pnl_usd"] == 15.0  # 10*(1-0.4)/0.4

    def test_two_positions_same_market_resolve_once(self):
        db = Database(":memory:")
        _seed_live_position(db, market="0xshared", status="filled", stake_usd=10.0)
        _seed_live_position(db, market="0xshared", status="filled", stake_usd=10.0, address="0xwallet2")

        with patch.object(cls, "fetch_market_resolution", return_value=True) as mock_resolve:
            summary = cls._settle_live_positions(db)

        mock_resolve.assert_called_once_with("0xshared")
        assert summary["settled"] == 2

    def test_one_bad_row_does_not_prevent_others_from_settling(self):
        db = Database(":memory:")
        bad_id = _seed_live_position(db, market="0xbad", status="filled", stake_usd=10.0)
        good_id = _seed_live_position(db, market="0xgood", status="filled", stake_usd=10.0)

        real_settle = db.settle_copy_live_position

        def flaky_settle(position_id, settled_pnl_usd, settled_at):
            if position_id == bad_id:
                raise RuntimeError("simulated DB error")
            return real_settle(position_id, settled_pnl_usd, settled_at)

        db.settle_copy_live_position = flaky_settle

        with patch.object(cls, "fetch_market_resolution", return_value=True):
            summary = cls._settle_live_positions(db)

        assert summary == {"settled": 1, "pending": 0, "errors": 1}
        bad_row = db._conn.execute(
            "SELECT status FROM copy_live_positions WHERE id=?", (bad_id,)
        ).fetchone()
        assert bad_row["status"] == "filled"
        good_row = db._conn.execute(
            "SELECT status FROM copy_live_positions WHERE id=?", (good_id,)
        ).fetchone()
        assert good_row["status"] == "settled"


class TestGhostOrderRecovery:
    """Issue #1171 item 1 / #1174."""

    def test_no_ghost_rows_is_a_cheap_no_op(self):
        db = Database(":memory:")
        factory = MagicMock()
        summary = cls.recover_ghost_orders(db, factory)
        assert summary == {"recovered": 0, "confirmed_dead": 0, "still_ambiguous": 0}
        factory.assert_not_called()

    def test_confirmed_full_fill_recovers_to_filled(self, caplog):
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="rejected", stake_usd=10.0, fill_price=0.40,
            order_id="oid-ghost", rejected_reason="cancel_failed_ghost",
        )
        trader = MagicMock()
        trader.check_fill.return_value = "filled"
        trader.get_order_fill_size.return_value = 25.0  # 10/0.40 = 25 intended shares
        factory = MagicMock(return_value=MagicMock())

        with patch.object(cls, "LiveTrader", return_value=trader), caplog.at_level("CRITICAL"):
            summary = cls.recover_ghost_orders(db, factory)

        assert summary == {"recovered": 1, "confirmed_dead": 0, "still_ambiguous": 0}
        row = db._conn.execute(
            "SELECT * FROM copy_live_positions WHERE id=?", (position_id,)
        ).fetchone()
        assert row["status"] == "filled"
        assert row["filled_stake_usd"] == 25.0 * 0.40
        assert any("CRITICAL" in rec.message and "recovered" in rec.message for rec in caplog.records)

    def test_confirmed_smaller_fill_recovers_to_partial(self):
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="rejected", stake_usd=10.0, fill_price=0.40,
            order_id="oid-ghost", rejected_reason="cancel_failed_ghost",
        )
        trader = MagicMock()
        trader.check_fill.return_value = "open"
        trader.get_order_fill_size.return_value = 5.0  # < 25 intended shares
        factory = MagicMock(return_value=MagicMock())

        with patch.object(cls, "LiveTrader", return_value=trader):
            summary = cls.recover_ghost_orders(db, factory)

        assert summary == {"recovered": 1, "confirmed_dead": 0, "still_ambiguous": 0}
        row = db._conn.execute(
            "SELECT status, filled_stake_usd FROM copy_live_positions WHERE id=?", (position_id,)
        ).fetchone()
        assert row["status"] == "partial"
        assert row["filled_stake_usd"] == 2.0  # 5.0 * 0.40

    def test_confirmed_zero_fill_cancellation_is_marked_dead_not_re_queried(self):
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="rejected", stake_usd=10.0, fill_price=0.40,
            order_id="oid-ghost", rejected_reason="cancel_failed_ghost",
        )
        trader = MagicMock()
        trader.check_fill.return_value = "cancelled"
        trader.get_order_fill_size.return_value = 0.0
        factory = MagicMock(return_value=MagicMock())

        with patch.object(cls, "LiveTrader", return_value=trader):
            summary = cls.recover_ghost_orders(db, factory)

        assert summary == {"recovered": 0, "confirmed_dead": 1, "still_ambiguous": 0}
        row = db._conn.execute(
            "SELECT status, rejected_reason FROM copy_live_positions WHERE id=?", (position_id,)
        ).fetchone()
        assert row["status"] == "rejected"
        assert row["rejected_reason"] == "cancel_confirmed_zero_fill"

        # A second run must never re-flag this row as still-ambiguous --
        # get_ghost_order_positions only matches 'cancel_failed_ghost'.
        assert db.get_ghost_order_positions() == []

    def test_still_open_order_is_left_ambiguous_for_next_run(self):
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="rejected", stake_usd=10.0, fill_price=0.40,
            order_id="oid-ghost", rejected_reason="cancel_failed_ghost",
        )
        trader = MagicMock()
        trader.check_fill.return_value = "open"
        trader.get_order_fill_size.return_value = 0.0
        factory = MagicMock(return_value=MagicMock())

        with patch.object(cls, "LiveTrader", return_value=trader):
            summary = cls.recover_ghost_orders(db, factory)

        assert summary == {"recovered": 0, "confirmed_dead": 0, "still_ambiguous": 1}
        row = db._conn.execute(
            "SELECT status, rejected_reason FROM copy_live_positions WHERE id=?", (position_id,)
        ).fetchone()
        assert row["status"] == "rejected"
        assert row["rejected_reason"] == "cancel_failed_ghost"  # unchanged -- retried next run

    def test_recheck_exception_leaves_row_ambiguous_without_raising(self):
        db = Database(":memory:")
        _seed_live_position(
            db, market="0xm1", status="rejected", stake_usd=10.0, fill_price=0.40,
            order_id="oid-ghost", rejected_reason="cancel_failed_ghost",
        )
        trader = MagicMock()
        trader.check_fill.side_effect = RuntimeError("CLOB unreachable")
        factory = MagicMock(return_value=MagicMock())

        with patch.object(cls, "LiveTrader", return_value=trader):
            summary = cls.recover_ghost_orders(db, factory)

        assert summary == {"recovered": 0, "confirmed_dead": 0, "still_ambiguous": 1}

    def test_plain_rejected_rows_are_never_touched(self):
        """A confirmed, unambiguous rejection (not a ghost) must never be
        re-checked or re-written."""
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="rejected", stake_usd=10.0,
            order_id="oid-plain", rejected_reason="timeout",
        )
        factory = MagicMock()

        summary = cls.recover_ghost_orders(db, factory)

        assert summary == {"recovered": 0, "confirmed_dead": 0, "still_ambiguous": 0}
        factory.assert_not_called()
        row = db._conn.execute(
            "SELECT rejected_reason FROM copy_live_positions WHERE id=?", (position_id,)
        ).fetchone()
        assert row["rejected_reason"] == "timeout"


class TestIsolationFromCopyPositionsAndCopySettle:
    """This script never touches copy_positions/copy_settle.py (issue
    #1100 isolation decision, restated explicitly by #1174's own
    acceptance criteria)."""

    def test_settle_never_touches_copy_positions_table(self):
        db = Database(":memory:")
        signal_id = _seed_signal(db, market="0xpaper")
        paper_id = db.insert_copy_position(
            signal_id=signal_id, address=ADDRESS, market="0xpaper",
            outcome_index=0, entry_price=0.40, stake_usd=10.0, entry_ts=NOW_ISO,
        )
        _seed_live_position(db, market="0xlive", status="filled", stake_usd=10.0)

        with patch.object(cls, "fetch_market_resolution", return_value=True):
            cls._settle_live_positions(db)

        # The paper position must remain untouched -- still open, never
        # settled by this script.
        paper_row = db._conn.execute(
            "SELECT status FROM copy_positions WHERE id=?", (paper_id,)
        ).fetchone()
        assert paper_row["status"] == "open"

    def test_run_once_never_imports_copy_settle_module(self):
        """A cheap static guarantee: copy_live_settle.py's own source never
        imports src.scripts.copy_settle (docstring/comment prose mentioning
        it by name for context is fine -- an actual import statement is
        the thing that would indicate coupling)."""
        import inspect
        for line in inspect.getsource(cls).splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "copy_settle " not in stripped and not stripped.endswith("copy_settle"), (
                    f"unexpected import referencing copy_settle: {line!r}"
                )


class TestRunOnce:
    def test_no_db_returns_empty_summaries(self):
        with patch.object(cls, "_open_db", return_value=None):
            result = cls.run_once()
        assert result == {
            "settle": {"settled": 0, "pending": 0, "errors": 0},
            "ghost_recovery": {"recovered": 0, "confirmed_dead": 0, "still_ambiguous": 0},
            "balance_check": None,
        }

    def test_no_ghost_rows_and_no_capital_skips_clob_entirely(self):
        """Paper-only / not-yet-configured-for-live posture: run_once must
        never import the CLOB auth module or construct a LiveTrader at all."""
        db = Database(":memory:")
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 0.0), \
             patch.object(cls, "fetch_market_resolution", return_value=None), \
             patch("src.execution.auth.get_clob_client") as mock_get_client:
            result = cls.run_once(db=db)

        mock_get_client.assert_not_called()
        assert result["balance_check"] is None
        assert result["ghost_recovery"] == {"recovered": 0, "confirmed_dead": 0, "still_ambiguous": 0}

    def test_explicit_clob_client_factory_is_used_for_both_ghost_and_balance(self):
        db = Database(":memory:")
        _seed_live_position(
            db, market="0xm1", status="rejected", stake_usd=10.0, fill_price=0.40,
            order_id="oid-ghost", rejected_reason="cancel_failed_ghost",
        )
        trader = MagicMock()
        trader.check_fill.return_value = "open"
        trader.get_order_fill_size.return_value = 0.0
        trader.get_usdc_balance.return_value = 100.0
        factory = MagicMock(return_value=MagicMock())

        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "fetch_market_resolution", return_value=None):
            result = cls.run_once(db=db, clob_client_factory=factory)

        assert result["ghost_recovery"] == {"recovered": 0, "confirmed_dead": 0, "still_ambiguous": 1}
        assert result["balance_check"]["actual_balance_usd"] == 100.0
