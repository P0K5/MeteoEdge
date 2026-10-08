"""Unit tests for src/scripts/copy_live_settle.py (epic I #1160, issue
#1174). Uses a real in-memory Database (seeded rows), mirroring
test_copy_settle.py's `Database(":memory:")` pattern, with
`fetch_market_resolution` and `LiveTrader` patched -- no real network/CLOB
calls.

Per the issue's own instruction ("write the drift-detection test first"),
TestWalletBalanceDriftDetection is the first test class in this file.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.data.db import Database
from src.data.wallet_reconciliation import ReconciliationResult
from src.scripts import copy_live_settle as cls

ADDRESS = "0xwallet1"
NOW_ISO = "2026-09-20T00:00:00+00:00"
DEPOSIT_WALLET = "0xdeposit00000000000000000000000000000001"


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
    """The actual "reconciliation" this epic is named for.

    Rebuilt for issue #1345: ``expected_balance`` now comes from
    ``src.data.wallet_reconciliation.compute_wallet_reconciliation`` (full
    on-chain + Data API cash-flow reconciliation of the deposit wallet's
    entire history), not local ``copy_live_positions`` bookkeeping -- so
    these tests patch that function directly (its own module has its own
    dedicated test file, ``test_wallet_reconciliation.py``) rather than
    seeding ``copy_live_positions`` rows to control the expected-balance
    math.
    """

    def _factory(self, balance: float):
        trader = MagicMock()
        trader.get_usdc_balance.return_value = balance
        factory = MagicMock(return_value=MagicMock())
        return factory, trader

    def _recon(self, expected_balance, **overrides) -> ReconciliationResult:
        kwargs = dict(
            activity_available=True, expected_balance_usd=expected_balance,
            activity_cash_flow_usd=expected_balance, transfers_unverified=False,
        )
        kwargs.update(overrides)
        return ReconciliationResult(**kwargs)

    def test_drift_within_tolerance_is_not_flagged_and_not_critical(self, caplog, monkeypatch):
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        factory, trader = self._factory(balance=81.0)
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=self._recon(81.0)) as mock_recon, \
             caplog.at_level("CRITICAL"):
            result = cls.check_wallet_balance_drift(db, factory)

        mock_recon.assert_called_once_with(DEPOSIT_WALLET)
        assert result == {
            "expected_balance_usd": 81.0,
            "actual_balance_usd": 81.0,
            "drift_usd": 0.0,
            "within_tolerance": True,
            "external_deposits_usd": 0.0,
            "external_withdrawals_usd": 0.0,
            "transfers_unverified": False,
            "transfers_unverified_reason": None,
            "unresolved_usd": 0.0,
            "unresolved_count": 0,
        }
        assert not any("CRITICAL" in rec.message for rec in caplog.records)

    def test_drift_beyond_tolerance_is_flagged_loudly(self, caplog, monkeypatch):
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        factory, trader = self._factory(balance=100.0)  # expected=80 -> drift=20
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=self._recon(80.0)), \
             caplog.at_level("CRITICAL"):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result["within_tolerance"] is False
        assert result["drift_usd"] == 20.0
        assert any(
            "CRITICAL" in rec.message and "WALLET BALANCE DRIFT" in rec.message
            for rec in caplog.records
        )

    def test_drift_exactly_at_tolerance_boundary_is_not_flagged(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        db.set_config("COPY_LIVE_BALANCE_DRIFT_TOLERANCE_USD", "1.0")

        factory, trader = self._factory(balance=99.0)  # expected=100, drift=-1.0
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=self._recon(100.0)):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result["within_tolerance"] is True

    def test_skips_entirely_when_no_real_capital_allocated(self, monkeypatch):
        """COPY_LIVE_CAPITAL_USD<=0 (the safety default) -- must skip
        without ever calling the CLOB, mirroring COPY_LIVE_TRADING_ENABLED's
        own inert-by-default precedent. Carried over unchanged from the
        pre-#1345 model -- see check_wallet_balance_drift()'s own docstring
        for why this gate stays even though the formula it used to feed no
        longer exists."""
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        factory = MagicMock()
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 0.0):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result is None
        factory.assert_not_called()

    def test_skips_entirely_when_deposit_wallet_not_set(self, monkeypatch):
        """Issue #1345's new gate: with no wallet address there is nothing
        to reconcile against -- must skip, never crash, never call the
        CLOB or the reconciliation."""
        monkeypatch.delenv("POLYMARKET_DEPOSIT_WALLET", raising=False)
        db = Database(":memory:")
        factory = MagicMock()
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "compute_wallet_reconciliation") as mock_recon:
            result = cls.check_wallet_balance_drift(db, factory)

        assert result is None
        factory.assert_not_called()
        mock_recon.assert_not_called()

    def test_activity_unavailable_returns_none_without_raising(self, monkeypatch):
        """The Data API activity feed being unreachable leaves no cash-flow
        signal at all to build an expected balance from -- must skip
        cleanly, mirroring the CLOB-unreachable precedent below."""
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        factory = MagicMock()
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(
                 cls, "compute_wallet_reconciliation",
                 return_value=ReconciliationResult(activity_available=False, expected_balance_usd=None),
             ):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result is None
        factory.assert_not_called()

    def test_clob_failure_returns_none_without_raising(self, caplog, monkeypatch):
        """A network/auth blip fetching the real balance must never crash
        the whole hourly run -- this is one of three independent things
        run_once does."""
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        factory = MagicMock(return_value=MagicMock())
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", side_effect=RuntimeError("CLOB unreachable")), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=self._recon(80.0)):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result is None

    def test_detected_external_deposit_is_added_to_expected_balance(self, monkeypatch):
        """The ground-truth scenario this issue is named for: a real $20
        external top-up must be reflected in expected_balance, not treated
        as unexplained drift."""
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        # Trades/redeems cash flow alone would say 80; the $20 deposit
        # brings expected_balance to 100, matching the real exchange
        # balance -- zero drift, not a $20 "mystery".
        factory, trader = self._factory(balance=100.0)
        recon = self._recon(100.0, activity_cash_flow_usd=80.0, external_deposits_usd=20.0)
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=recon):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result["expected_balance_usd"] == 100.0
        assert result["external_deposits_usd"] == 20.0
        assert result["within_tolerance"] is True

    def test_detected_external_withdrawal_reduces_expected_balance(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        factory, trader = self._factory(balance=70.0)
        recon = self._recon(70.0, activity_cash_flow_usd=80.0, external_withdrawals_usd=10.0)
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=recon):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result["expected_balance_usd"] == 70.0
        assert result["external_withdrawals_usd"] == 10.0
        assert result["within_tolerance"] is True

    def test_missing_etherscan_key_reports_unverified_not_silent_zero(self, caplog, monkeypatch):
        """Issue #1345 acceptance criteria: a missing ETHERSCAN_API_KEY must
        never be silently treated as "confirmed zero external transfers" --
        the check still produces a verdict (from Data API activity alone)
        but flags transfers_unverified=True."""
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        factory, trader = self._factory(balance=80.0)
        recon = self._recon(
            80.0, transfers_unverified=True, transfers_unverified_reason="no_api_key",
        )
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=recon), \
             caplog.at_level("WARNING"):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result["transfers_unverified"] is True
        assert result["transfers_unverified_reason"] == "no_api_key"
        assert result["within_tolerance"] is True  # no false-positive drift flag
        assert any("UNVERIFIED" in rec.message for rec in caplog.records)

    def test_unresolved_transfer_is_reported_not_folded_into_drift(self, monkeypatch):
        """A transfer that looks like trade settlement but is absent from
        the Data API (the known #1342 gap) must be surfaced distinctly,
        never silently absorbed into drift_usd or guessed as a deposit."""
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        factory, trader = self._factory(balance=80.0)
        recon = self._recon(
            80.0, unresolved=[{"transaction_hash": "0xabc", "amount_usd": 3.5,
                                "function_name": "matchOrders", "timestamp": "123",
                                "reason": "possible_trade_missing_from_data_api"}],
            unresolved_usd=3.5,
        )
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=recon):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result["unresolved_usd"] == 3.5
        assert result["unresolved_count"] == 1
        # The $3.50 unresolved amount is reported on its own, not added
        # into expected_balance (which came straight from the mocked
        # recon's expected_balance_usd=80.0) or hidden inside drift_usd.
        assert result["expected_balance_usd"] == 80.0


class TestWalletBalanceDriftPersistence:
    """Issue #1189: check_wallet_balance_drift()'s result must be queryable
    by the dashboard API after the hourly job exits, via
    get_wallet_balance_drift_status(). The persisted/read contract's
    original five keys are unchanged by issue #1345's computation rebuild
    -- CopyLiveBalanceDriftOut (src/dashboard/api.py) only ever reads
    those five, so the extra #1345 keys on the persisted row are additive
    and inert to it."""

    def _factory(self, balance: float):
        trader = MagicMock()
        trader.get_usdc_balance.return_value = balance
        factory = MagicMock(return_value=MagicMock())
        return factory, trader

    def _recon(self, expected_balance, **overrides) -> ReconciliationResult:
        kwargs = dict(
            activity_available=True, expected_balance_usd=expected_balance,
            activity_cash_flow_usd=expected_balance, transfers_unverified=False,
        )
        kwargs.update(overrides)
        return ReconciliationResult(**kwargs)

    def test_never_checked_returns_all_none(self):
        db = Database(":memory:")
        status = cls.get_wallet_balance_drift_status(db)
        assert status == {
            "checked_at": None,
            "within_tolerance": None,
            "drift_usd": None,
            "expected_balance_usd": None,
            "actual_balance_usd": None,
        }

    def test_drift_beyond_tolerance_persists_and_is_readable(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        factory, trader = self._factory(balance=100.0)  # expected=80, drift=20

        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=self._recon(80.0)):
            result = cls.check_wallet_balance_drift(
                db, factory, now=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
            )

        status = cls.get_wallet_balance_drift_status(db)
        assert status["checked_at"] == "2026-09-20T12:00:00+00:00"
        assert status["within_tolerance"] is False
        assert status["drift_usd"] == result["drift_usd"] == 20.0
        assert status["expected_balance_usd"] == result["expected_balance_usd"]
        assert status["actual_balance_usd"] == result["actual_balance_usd"]

    def test_within_tolerance_result_clears_a_prior_warning(self, monkeypatch):
        """A dashboard must never keep showing a stale drift warning after
        it's resolved -- a later clean run overwrites the persisted row."""
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")

        bad_factory, bad_trader = self._factory(balance=100.0)  # drift=20, flagged
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=bad_trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=self._recon(80.0)):
            cls.check_wallet_balance_drift(
                db, bad_factory, now=datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc),
            )
        assert cls.get_wallet_balance_drift_status(db)["within_tolerance"] is False

        good_factory, good_trader = self._factory(balance=80.0)  # drift=0, clean
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=good_trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=self._recon(80.0)):
            cls.check_wallet_balance_drift(
                db, good_factory, now=datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc),
            )

        status = cls.get_wallet_balance_drift_status(db)
        assert status["within_tolerance"] is True
        assert status["drift_usd"] == 0.0
        assert status["checked_at"] == "2026-09-20T02:00:00+00:00"

    def test_skip_no_capital_never_persists_or_clears(self, monkeypatch):
        """A None-return (no capital allocated) must not overwrite a
        previously-persisted verdict -- see the function's own docstring on
        why a stale timestamp, not silence, is the intended signal."""
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        factory, trader = self._factory(balance=100.0)
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=self._recon(80.0)):
            cls.check_wallet_balance_drift(
                db, factory, now=datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc),
            )
        before = cls.get_wallet_balance_drift_status(db)

        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 0.0):
            result = cls.check_wallet_balance_drift(db, MagicMock())

        assert result is None
        assert cls.get_wallet_balance_drift_status(db) == before

    def test_clob_failure_never_persists_or_clears(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        factory, trader = self._factory(balance=100.0)
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=self._recon(80.0)):
            cls.check_wallet_balance_drift(
                db, factory, now=datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc),
            )
        before = cls.get_wallet_balance_drift_status(db)

        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", side_effect=RuntimeError("CLOB unreachable")), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=self._recon(80.0)):
            result = cls.check_wallet_balance_drift(db, factory)

        assert result is None
        assert cls.get_wallet_balance_drift_status(db) == before

    def test_no_db_returns_all_none(self):
        assert cls.get_wallet_balance_drift_status(None) == {
            "checked_at": None,
            "within_tolerance": None,
            "drift_usd": None,
            "expected_balance_usd": None,
            "actual_balance_usd": None,
        }

    def test_corrupt_persisted_row_degrades_to_all_none(self):
        db = Database(":memory:")
        db.set_config(cls._BALANCE_DRIFT_STATUS_KEY, "not valid json")
        assert cls.get_wallet_balance_drift_status(db) == {
            "checked_at": None,
            "within_tolerance": None,
            "drift_usd": None,
            "expected_balance_usd": None,
            "actual_balance_usd": None,
        }


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
        trader.get_order_fill_cost_usd.return_value = 0.0  # default: fall back to shares*price approximation
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

    def test_recovered_fill_prefers_actual_cost_over_approximation(self):
        """Issue #1341: recover_ghost_orders must not repeat the #1336
        shares*placed-price bug -- the order's own confirmed-trade cost
        wins when available."""
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="rejected", stake_usd=10.0, fill_price=0.40,
            order_id="oid-ghost", rejected_reason="cancel_failed_ghost",
        )
        trader = MagicMock()
        trader.check_fill.return_value = "filled"
        trader.get_order_fill_size.return_value = 25.0  # 10/0.40 = 25 intended shares
        trader.get_order_fill_cost_usd.return_value = 5.5  # real cost, far below 25*0.40
        factory = MagicMock(return_value=MagicMock())

        with patch.object(cls, "LiveTrader", return_value=trader):
            summary = cls.recover_ghost_orders(db, factory)

        assert summary == {"recovered": 1, "confirmed_dead": 0, "still_ambiguous": 0}
        row = db._conn.execute(
            "SELECT filled_stake_usd FROM copy_live_positions WHERE id=?", (position_id,)
        ).fetchone()
        assert row["filled_stake_usd"] == 5.5  # NOT 25.0 * 0.40 == 10.0

    def test_confirmed_smaller_fill_recovers_to_partial(self):
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="rejected", stake_usd=10.0, fill_price=0.40,
            order_id="oid-ghost", rejected_reason="cancel_failed_ghost",
        )
        trader = MagicMock()
        trader.get_order_fill_cost_usd.return_value = 0.0  # default: fall back to shares*price approximation
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
        trader.get_order_fill_cost_usd.return_value = 0.0  # default: fall back to shares*price approximation
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
        trader.get_order_fill_cost_usd.return_value = 0.0  # default: fall back to shares*price approximation
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
        trader.get_order_fill_cost_usd.return_value = 0.0  # default: fall back to shares*price approximation
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

    def test_flipping_live_enabled_off_does_not_block_settlement(self):
        """Issue #1253: this script settles from copy_live_positions rows
        and has no coupling to copy_wallets_followed at all -- turning a
        wallet's per-wallet live_enabled flag off (e.g. an operator pulling
        it from the live roster mid-flight) must never interfere with an
        already-open live position for that wallet settling normally."""
        db = Database(":memory:")
        db.insert_followed_wallet(address=ADDRESS, stake_per_trade=10.0, added_at=NOW_ISO)
        db.set_followed_wallet_live_enabled(ADDRESS, True)
        position_id = _seed_live_position(
            db, market="0xstillsettles", status="filled", stake_usd=10.0, fill_price=0.40,
        )

        # Opt the wallet back out of live -- the open position above was
        # already placed before this flip.
        db.set_followed_wallet_live_enabled(ADDRESS, False)

        with patch.object(cls, "fetch_market_resolution", return_value=True):
            summary = cls._settle_live_positions(db)

        assert summary == {"settled": 1, "pending": 0, "errors": 0}
        row = db._conn.execute(
            "SELECT status, settled_pnl_usd FROM copy_live_positions WHERE id=?",
            (position_id,),
        ).fetchone()
        assert row["status"] == "settled"
        assert row["settled_pnl_usd"] == 15.0  # 10*(1-0.4)/0.4

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

    def test_explicit_clob_client_factory_is_used_for_both_ghost_and_balance(self, monkeypatch):
        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", DEPOSIT_WALLET)
        db = Database(":memory:")
        _seed_live_position(
            db, market="0xm1", status="rejected", stake_usd=10.0, fill_price=0.40,
            order_id="oid-ghost", rejected_reason="cancel_failed_ghost",
        )
        trader = MagicMock()
        trader.get_order_fill_cost_usd.return_value = 0.0  # default: fall back to shares*price approximation
        trader.check_fill.return_value = "open"
        trader.get_order_fill_size.return_value = 0.0
        trader.get_usdc_balance.return_value = 100.0
        factory = MagicMock(return_value=MagicMock())

        recon = ReconciliationResult(
            activity_available=True, expected_balance_usd=100.0,
            activity_cash_flow_usd=100.0, transfers_unverified=False,
        )
        with patch.object(cls, "COPY_LIVE_CAPITAL_USD", 100.0), \
             patch.object(cls, "LiveTrader", return_value=trader), \
             patch.object(cls, "compute_wallet_reconciliation", return_value=recon), \
             patch.object(cls, "fetch_market_resolution", return_value=None):
            result = cls.run_once(db=db, clob_client_factory=factory)

        assert result["ghost_recovery"] == {"recovered": 0, "confirmed_dead": 0, "still_ambiguous": 1}
        assert result["balance_check"]["actual_balance_usd"] == 100.0


class TestFillCostCoalesceIssue1336:
    """Issue #1336: realized P&L and committed exposure must use
    COALESCE(filled_stake_usd, stake_usd) for FULL fills too, not only
    partials. A 'filled' row with a recorded (short) cost must not be booked
    at its intended stake."""

    def test_short_full_status_fill_pnl_uses_recorded_cost_loss(self):
        """Intended $10 at 0.40, but only $3.00 actually matched. A losing
        position must lose $3.00, not $10.00."""
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="filled", stake_usd=10.0,
            filled_stake_usd=3.0, fill_price=0.40,
        )
        with patch.object(cls, "fetch_market_resolution", return_value=False):  # NO wins, YES position loses
            summary = cls._settle_live_positions(db)

        assert summary == {"settled": 1, "pending": 0, "errors": 0}
        row = db._conn.execute(
            "SELECT settled_pnl_usd FROM copy_live_positions WHERE id=?", (position_id,)
        ).fetchone()
        assert row["settled_pnl_usd"] == -3.0

    def test_short_full_status_fill_pnl_uses_recorded_cost_win(self):
        """$3.00 matched at 0.40 wins: shares = 7.5, payout 7.5 -> pnl 4.5."""
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="filled", stake_usd=10.0,
            filled_stake_usd=3.0, fill_price=0.40,
        )
        with patch.object(cls, "fetch_market_resolution", return_value=True):
            cls._settle_live_positions(db)

        row = db._conn.execute(
            "SELECT settled_pnl_usd FROM copy_live_positions WHERE id=?", (position_id,)
        ).fetchone()
        assert row["settled_pnl_usd"] == 4.5  # 3*(1-0.4)/0.4

    def test_null_filled_stake_falls_back_to_stake_usd_on_loss(self):
        """Legacy 'filled' row with no recorded cost: fallback to stake_usd."""
        db = Database(":memory:")
        position_id = _seed_live_position(
            db, market="0xm1", status="filled", stake_usd=10.0, fill_price=0.40,
        )
        with patch.object(cls, "fetch_market_resolution", return_value=False):
            cls._settle_live_positions(db)

        row = db._conn.execute(
            "SELECT settled_pnl_usd FROM copy_live_positions WHERE id=?", (position_id,)
        ).fetchone()
        assert row["settled_pnl_usd"] == -10.0

    # NOTE: the pre-#1345 "committed exposure" test that lived here
    # (`test_drift_committed_exposure_uses_recorded_full_fill_cost`) tested
    # the old COPY_LIVE_CAPITAL_USD-minus-committed-plus-realized formula's
    # own COALESCE(filled_stake_usd, stake_usd) usage. That formula (and the
    # "committed exposure" concept itself) no longer exists after #1345's
    # rebuild -- expected_balance now comes entirely from
    # wallet_reconciliation.compute_wallet_reconciliation, which has no
    # local-ledger "committed" term to get right or wrong. Removed, not
    # replaced: TestWalletBalanceDriftDetection above covers the current
    # formula's own correctness.
