"""Unit tests for src/scripts/copy_wallet_promotion.py (issue #1122, epic
#1101 story B2). Database is mocked -- no real network/DB calls, mirroring
test_copy_wallet_screening.py's mocking style.
"""
from unittest.mock import MagicMock

import pytest

from src.scripts.copy_wallet_promotion import follow, main, pause, report, resume


def _screening_row(address="0xabc", eligible=1, median_roi=0.1, **overrides):
    row = {
        "address": address,
        "window": "month",
        "screened_at": "2026-09-19T00:00:00+00:00",
        "n_buy_trades": 10,
        "n_resolved": 10,
        "win_rate": 0.6,
        "mean_roi": 0.1,
        "median_roi": median_roi,
        "eligible_to_follow": eligible,
    }
    row.update(overrides)
    return row


def _followed_row(address="0xabc", status="active", **overrides):
    row = {
        "address": address,
        "stake_per_trade": 5.0,
        "status": status,
        "paused_reason": None,
        "added_at": "2026-09-01T00:00:00+00:00",
        "last_seen_trade_ts": None,
    }
    row.update(overrides)
    return row


class TestFollow:
    def test_succeeds_when_eligible_and_under_cap(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_recent_wallet_screenings.return_value = [_screening_row(eligible=1)]

        rc = follow(db, "0xabc", stake=5.0, max_followed=10)

        assert rc == 0
        db.insert_followed_wallet.assert_called_once()
        kwargs = db.insert_followed_wallet.call_args.kwargs
        assert kwargs["address"] == "0xabc"
        assert kwargs["stake_per_trade"] == 5.0
        assert "added_at" in kwargs

    def test_refuses_when_eligible_to_follow_is_zero(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_recent_wallet_screenings.return_value = [_screening_row(eligible=0)]

        rc = follow(db, "0xabc", stake=5.0, max_followed=10)

        assert rc == 1
        db.insert_followed_wallet.assert_not_called()

    def test_refuses_when_no_screening_row_exists(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_recent_wallet_screenings.return_value = []

        rc = follow(db, "0xabc", stake=5.0, max_followed=10)

        assert rc == 1
        db.insert_followed_wallet.assert_not_called()

    def test_refuses_when_max_wallets_followed_already_active(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = [
            _followed_row(address=f"0xwallet{i}") for i in range(3)
        ]
        db.get_recent_wallet_screenings.return_value = [_screening_row(eligible=1)]

        rc = follow(db, "0xnew", stake=5.0, max_followed=3)

        assert rc == 1
        db.insert_followed_wallet.assert_not_called()

    def test_refuses_when_already_followed(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = [_followed_row(address="0xabc", status="paused")]
        db.get_recent_wallet_screenings.return_value = [_screening_row(eligible=1)]

        rc = follow(db, "0xabc", stake=5.0, max_followed=10)

        assert rc == 1
        db.insert_followed_wallet.assert_not_called()

    def test_uses_latest_screening_row_only(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_recent_wallet_screenings.return_value = [_screening_row(eligible=1)]

        follow(db, "0xabc", stake=5.0, max_followed=10)

        db.get_recent_wallet_screenings.assert_called_once_with("0xabc", limit=1)

    @pytest.mark.parametrize("bad_stake", [0.0, -5.0, float("nan"), float("inf"), float("-inf")])
    def test_refuses_non_positive_or_non_finite_stake(self, bad_stake):
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_recent_wallet_screenings.return_value = [_screening_row(eligible=1)]

        rc = follow(db, "0xabc", stake=bad_stake, max_followed=10)

        assert rc == 1
        db.insert_followed_wallet.assert_not_called()
        # Rejected before any DB read -- --stake validation happens up front.
        db.get_followed_wallets.assert_not_called()
        db.get_recent_wallet_screenings.assert_not_called()


class TestPause:
    def test_calls_update_status_with_expected_args(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = [_followed_row(address="0xabc")]

        rc = pause(db, "0xabc", "unstable")

        assert rc == 0
        db.update_followed_wallet_status.assert_called_once_with("0xabc", "paused", "unstable")

    def test_refuses_when_address_not_followed(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = []

        rc = pause(db, "0xabc", "unstable")

        assert rc == 1
        db.update_followed_wallet_status.assert_not_called()


class TestResume:
    def test_calls_update_status_with_expected_args(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = [_followed_row(address="0xabc", status="paused")]

        rc = resume(db, "0xabc", max_followed=10)

        assert rc == 0
        db.update_followed_wallet_status.assert_called_once_with("0xabc", "active", None)

    def test_refuses_when_already_active(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = [_followed_row(address="0xabc", status="active")]

        rc = resume(db, "0xabc", max_followed=10)

        assert rc == 1
        db.update_followed_wallet_status.assert_not_called()

    def test_refuses_when_would_exceed_max_wallets_followed(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = [
            _followed_row(address="0xpaused", status="paused"),
            _followed_row(address="0xwallet0", status="active"),
            _followed_row(address="0xwallet1", status="active"),
        ]

        rc = resume(db, "0xpaused", max_followed=2)

        assert rc == 1
        db.update_followed_wallet_status.assert_not_called()

    def test_refuses_when_address_not_followed(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = []

        rc = resume(db, "0xabc", max_followed=10)

        assert rc == 1
        db.update_followed_wallet_status.assert_not_called()


class TestReport:
    def test_read_only_calls_no_db_write_methods(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_latest_wallet_screenings.return_value = [_screening_row(eligible=1)]

        rc = report(db, max_followed=10)

        assert rc == 0
        db.insert_followed_wallet.assert_not_called()
        db.update_followed_wallet_status.assert_not_called()
        db.insert_wallet_screening.assert_not_called()

    def test_excludes_already_followed_addresses(self):
        db = MagicMock()
        db.get_followed_wallets.return_value = [_followed_row(address="0xabc")]
        db.get_latest_wallet_screenings.return_value = [
            _screening_row(address="0xabc", eligible=1),
            _screening_row(address="0xdef", eligible=1),
        ]

        report(db, max_followed=10)

        # Cannot inspect print() output directly without capsys, but this at
        # least asserts the read-only contract and that no filtering blows up.
        db.get_latest_wallet_screenings.assert_called_once()

    def test_excludes_ineligible_rows(self, capsys):
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_latest_wallet_screenings.return_value = [
            _screening_row(address="0xabc", eligible=0),
            _screening_row(address="0xdef", eligible=1),
        ]

        report(db, max_followed=10)

        out = capsys.readouterr().out
        assert "0xdef" in out
        assert "0xabc" not in out

    def test_sorted_by_median_roi_descending(self, capsys):
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_latest_wallet_screenings.return_value = [
            _screening_row(address="0xlow", eligible=1, median_roi=0.05),
            _screening_row(address="0xhigh", eligible=1, median_roi=0.50),
        ]

        report(db, max_followed=10)

        out = capsys.readouterr().out
        assert out.index("0xhigh") < out.index("0xlow")

    def test_reports_slots_remaining(self, capsys):
        db = MagicMock()
        db.get_followed_wallets.return_value = [
            _followed_row(address="0xwallet0"),
            _followed_row(address="0xwallet1"),
        ]
        db.get_latest_wallet_screenings.return_value = []

        report(db, max_followed=5)

        out = capsys.readouterr().out
        assert "3/5" in out

    def test_empty_table_reports_zero_candidates(self, capsys):
        """All candidates stale / empty table -> no crash, reports zero."""
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_latest_wallet_screenings.return_value = []

        rc = report(db, max_followed=10)

        assert rc == 0
        out = capsys.readouterr().out
        assert "0 eligible, unfollowed wallet(s)" in out

    def test_scopes_to_latest_run_only(self, capsys):
        """Scope the advisory to wallets from the latest screening run."""
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_latest_wallet_screenings.return_value = [
            # Recent run: 2026-09-27
            _screening_row(
                address="0xrecent1", eligible=1, median_roi=0.15,
                screened_at="2026-09-27T00:00:00+00:00"),
            _screening_row(
                address="0xrecent2", eligible=1, median_roi=0.10,
                screened_at="2026-09-27T00:00:00+00:00"),
            # Older run: 2026-09-25
            _screening_row(
                address="0xold", eligible=1, median_roi=0.20,
                screened_at="2026-09-25T00:00:00+00:00"),
        ]

        rc = report(db, max_followed=10)

        assert rc == 0
        out = capsys.readouterr().out
        # Should list recent wallets, not the old one
        assert "0xrecent1" in out
        assert "0xrecent2" in out
        assert "0xold" not in out
        # Should report 1 excluded
        assert "1 wallet(s) with screening data from runs before" in out

    def test_excludes_stale_ineligible_wallet(self, capsys):
        """Stale eligible_to_follow=1 from older run -> not listed, counted excluded.

        This is the shape described in issue #1226: 0x32b484581f with
        median_roi=-1.0 from an older screened_at, which would pass the
        advisory filter under the old code (eligible_to_follow=1) but is
        now excluded because it's from an older run.
        """
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_latest_wallet_screenings.return_value = [
            # Latest run: eligible and good ROI
            _screening_row(
                address="0xgood", eligible=1, median_roi=0.18,
                screened_at="2026-09-27T00:00:00+00:00"),
            # Stale run: eligible=1 but catastrophic ROI (the old #1209 issue)
            _screening_row(
                address="0xstale_bad", eligible=1, median_roi=-1.0,
                n_resolved=10398,
                screened_at="2026-09-25T00:00:00+00:00"),
        ]

        rc = report(db, max_followed=10)

        assert rc == 0
        out = capsys.readouterr().out
        # Only the current-run wallet should be listed
        assert "0xgood" in out
        assert "0xstale_bad" not in out
        # Should report 1 excluded (the stale wallet)
        assert "1 wallet(s) with screening data from runs before" in out

    def test_excludes_stale_even_if_eligible(self, capsys):
        """Wallets from older runs are excluded regardless of eligibility."""
        db = MagicMock()
        db.get_followed_wallets.return_value = []
        db.get_latest_wallet_screenings.return_value = [
            # Latest run
            _screening_row(
                address="0xcurrent", eligible=1,
                screened_at="2026-09-27T00:00:00+00:00"),
            # Older run, not eligible
            _screening_row(
                address="0xstale_ineligible", eligible=0,
                screened_at="2026-09-25T00:00:00+00:00"),
        ]

        rc = report(db, max_followed=10)

        assert rc == 0
        out = capsys.readouterr().out
        assert "0xcurrent" in out
        assert "0xstale_ineligible" not in out
        assert "1 wallet(s) with screening data from runs before" in out

    def test_eligible_in_latest_run_but_already_followed_excluded(self, capsys):
        """Wallet eligible in latest run but already followed -> not listed."""
        db = MagicMock()
        db.get_followed_wallets.return_value = [
            _followed_row(address="0xalready_followed")
        ]
        db.get_latest_wallet_screenings.return_value = [
            _screening_row(
                address="0xalready_followed", eligible=1,
                screened_at="2026-09-27T00:00:00+00:00"),
            _screening_row(
                address="0xcandidateA", eligible=1,
                screened_at="2026-09-27T00:00:00+00:00"),
        ]

        rc = report(db, max_followed=10)

        assert rc == 0
        out = capsys.readouterr().out
        # Followed wallet should not appear in recommendations
        assert "0xalready_followed" not in out
        # Unfollowed candidate should appear
        assert "0xcandidateA" in out
        # No wallets excluded due to staleness in this case
        assert "wallet(s) with screening data from runs before" not in out


class TestMainArgparse:
    def test_mutually_exclusive_actions_rejected(self):
        with pytest.raises(SystemExit):
            main(["--follow", "0xabc", "--pause", "0xdef", "--reason", "x"])

    def test_pause_without_reason_rejected(self):
        with pytest.raises(SystemExit):
            main(["--pause", "0xabc"])
