"""Unit tests for src/data/wallet_reconciliation.py (issue #1345).

Fixture shapes for ``get_wallet_activity``/``fetch_pusd_transfers`` mirror
real response shapes verified live against ``POLYMARKET_DEPOSIT_WALLET`` on
2026-10-08 (see this module's own docstring and the issue #1345 correction
comment for the full verification) -- not hypothetical schemas.
"""
from unittest.mock import patch

from src.data.onchain_transfers import TransferFetchResult
from src.data.polymarket_traders import TradeList
from src.data.wallet_reconciliation import (
    ClassificationResult,
    classify_onchain_transfers,
    compute_wallet_reconciliation,
    fetch_captured_activity,
)

WALLET = "0xb5f4c28c2F38a4f95c67054B013523994ba3a8B8"
WALLET_LOWER = WALLET.lower()

MATCH_ORDERS_METHOD_ID = "0x3c2b4399"
PERMIT2_METHOD_ID = "0x0a2b8f36"


def _activity_trade(tx_hash, usdc_size, side="BUY"):
    return {
        "proxyWallet": WALLET_LOWER, "type": "TRADE", "side": side,
        "usdcSize": usdc_size, "transactionHash": tx_hash, "timestamp": 1,
        "conditionId": "0xmarket", "size": usdc_size / 0.5, "price": 0.5,
        "outcomeIndex": 0,
    }


def _activity_redeem(tx_hash, usdc_size):
    return {
        "proxyWallet": WALLET_LOWER, "type": "REDEEM", "usdcSize": usdc_size,
        "transactionHash": tx_hash, "timestamp": 1, "conditionId": "0xmarket",
        "size": usdc_size, "price": 0,
    }


def _transfer(tx_hash, value_usd, *, to=WALLET_LOWER, frm="0xcounterparty",
              method_id=MATCH_ORDERS_METHOD_ID, function_name="matchOrders(...)"):
    return {
        "hash": tx_hash, "value": str(int(round(value_usd * 1e6))),
        "tokenDecimal": "6", "to": to, "from": frm,
        "methodId": method_id, "functionName": function_name, "timeStamp": "1",
    }


class TestFetchCapturedActivity:
    def test_sums_buy_as_cash_out_sell_and_redeem_as_cash_in(self):
        records = TradeList([
            _activity_trade("0xbuy", 10.0, side="BUY"),
            _activity_trade("0xsell", 4.0, side="SELL"),
            _activity_redeem("0xredeem", 7.5),
        ], truncated=False)
        with patch("src.data.wallet_reconciliation.get_wallet_activity", return_value=records):
            result = fetch_captured_activity(WALLET)

        assert result.available is True
        assert result.cash_flow_usd == -10.0 + 4.0 + 7.5
        assert result.tx_hashes == {"0xbuy", "0xsell", "0xredeem"}
        assert result.n_trades == 2
        assert result.n_redeems == 1

    def test_empty_genuine_activity_is_available_with_zero_cash_flow(self):
        with patch("src.data.wallet_reconciliation.get_wallet_activity", return_value=TradeList([], truncated=False)):
            result = fetch_captured_activity(WALLET)

        assert result.available is True
        assert result.cash_flow_usd == 0.0
        assert result.tx_hashes == set()

    def test_first_page_failure_is_unavailable_not_zero_activity(self):
        with patch("src.data.wallet_reconciliation.get_wallet_activity", return_value=TradeList([], truncated=True)):
            result = fetch_captured_activity(WALLET)

        assert result.available is False

    def test_unrecognized_trade_side_excluded_from_cash_flow_but_hash_kept(self):
        records = TradeList([
            {**_activity_trade("0xweird", 5.0, side="BUY"), "side": "UNKNOWN"},
        ], truncated=False)
        with patch("src.data.wallet_reconciliation.get_wallet_activity", return_value=records):
            result = fetch_captured_activity(WALLET)

        assert result.cash_flow_usd == 0.0
        assert "0xweird" in result.tx_hashes


class TestClassifyOnchainTransfers:
    def test_already_captured_tx_hash_is_excluded(self):
        transfers = [_transfer("0xTRADE1", 5.0)]
        result = classify_onchain_transfers(transfers, {"0xtrade1"}, WALLET)

        assert result.matched_count == 1
        assert result.external_deposits == []
        assert result.external_withdrawals == []
        assert result.unresolved == []

    def test_unmatched_inbound_transfer_is_external_deposit(self):
        """The $20 ground-truth scenario, via a permit2TransferAndMulticall
        -shaped (non-trade-settlement) method selector."""
        transfers = [_transfer("0xdeposit1", 20.0, to=WALLET_LOWER, frm="0xrelay",
                                method_id=PERMIT2_METHOD_ID, function_name="permit2TransferAndMulticall(...)")]
        result = classify_onchain_transfers(transfers, set(), WALLET)

        assert result.external_deposits_usd == 20.0
        assert len(result.external_deposits) == 1
        assert result.external_deposits[0]["amount_usd"] == 20.0
        assert result.external_withdrawals_usd == 0.0
        assert result.unresolved == []

    def test_unmatched_outbound_transfer_is_external_withdrawal(self):
        transfers = [_transfer("0xwithdraw1", 15.0, to="0xsomeoneelse", frm=WALLET_LOWER,
                                method_id=PERMIT2_METHOD_ID, function_name="permit2TransferAndMulticall(...)")]
        result = classify_onchain_transfers(transfers, set(), WALLET)

        assert result.external_withdrawals_usd == 15.0
        assert result.external_deposits_usd == 0.0

    def test_unmatched_trade_settlement_looking_transfer_is_unresolved_not_deposit(self):
        """The known #1342 Data-API-gap precedent: a transfer whose method
        selector looks like matchOrders but is absent from the Data API
        activity feed must stay unresolved, never guessed as a deposit."""
        transfers = [_transfer("0xgap1", 3.5, method_id=MATCH_ORDERS_METHOD_ID)]
        result = classify_onchain_transfers(transfers, set(), WALLET)

        assert result.unresolved == [{
            "transaction_hash": "0xgap1", "amount_usd": 3.5,
            "function_name": "matchOrders(...)", "timestamp": "1",
            "reason": "possible_trade_missing_from_data_api",
        }]
        assert result.external_deposits_usd == 0.0
        assert result.external_withdrawals_usd == 0.0

    def test_zero_net_multi_leg_transaction_is_ignored(self):
        """Mirrors the real 'proxy(...)' calls observed live: multiple pUSD
        legs in one tx that net to exactly zero for this wallet -- no cash
        impact, must not be reported as deposit/withdrawal/unresolved."""
        transfers = [
            _transfer("0xzero1", 5.0, to=WALLET_LOWER, frm="0xa", method_id="0x0a3c4405"),
            _transfer("0xzero1", 5.0, to="0xa", frm=WALLET_LOWER, method_id="0x0a3c4405"),
        ]
        result = classify_onchain_transfers(transfers, set(), WALLET)

        assert result.ignored_zero_net_count == 1
        assert result.external_deposits == []
        assert result.external_withdrawals == []
        assert result.unresolved == []

    def test_multiple_legs_in_one_tx_net_correctly(self):
        """A notional leg + a separate fee leg in the same unmatched
        transaction must net to the wallet's true cash impact, not be
        reported as two separate deposits."""
        transfers = [
            _transfer("0xmulti1", 5.0024, to="0xcounterparty", frm=WALLET_LOWER,
                       method_id=PERMIT2_METHOD_ID, function_name="permit2TransferAndMulticall(...)"),
            _transfer("0xmulti1", 0.0653, to="0xfeesink", frm=WALLET_LOWER,
                       method_id=PERMIT2_METHOD_ID, function_name="permit2TransferAndMulticall(...)"),
        ]
        result = classify_onchain_transfers(transfers, set(), WALLET)

        assert len(result.external_withdrawals) == 1
        assert round(result.external_withdrawals_usd, 4) == round(5.0024 + 0.0653, 4)


class TestComputeWalletReconciliation:
    def _patch_activity(self, cash_flow_usd, tx_hashes=frozenset(), available=True):
        from src.data.wallet_reconciliation import ActivityFetchResult
        return patch(
            "src.data.wallet_reconciliation.fetch_captured_activity",
            return_value=ActivityFetchResult(
                available=available, tx_hashes=set(tx_hashes), cash_flow_usd=cash_flow_usd,
            ),
        )

    def test_clean_reconciliation_no_external_transfers(self):
        with self._patch_activity(80.0), \
             patch(
                 "src.data.wallet_reconciliation.fetch_pusd_transfers",
                 return_value=TransferFetchResult(available=True, transfers=[]),
             ):
            result = compute_wallet_reconciliation(WALLET, etherscan_api_key="k")

        assert result.activity_available is True
        assert result.expected_balance_usd == 80.0
        assert result.transfers_unverified is False

    def test_detected_external_deposit_included_in_expected_balance(self):
        transfers = [_transfer("0xd1", 20.0, to=WALLET_LOWER, frm="0xrelay",
                                method_id=PERMIT2_METHOD_ID, function_name="permit2TransferAndMulticall(...)")]
        with self._patch_activity(80.0), \
             patch(
                 "src.data.wallet_reconciliation.fetch_pusd_transfers",
                 return_value=TransferFetchResult(available=True, transfers=transfers),
             ):
            result = compute_wallet_reconciliation(WALLET, etherscan_api_key="k")

        assert result.expected_balance_usd == 100.0
        assert result.external_deposits_usd == 20.0

    def test_detected_external_withdrawal_reduces_expected_balance(self):
        transfers = [_transfer("0xw1", 10.0, to="0xsomeoneelse", frm=WALLET_LOWER,
                                method_id=PERMIT2_METHOD_ID, function_name="permit2TransferAndMulticall(...)")]
        with self._patch_activity(80.0), \
             patch(
                 "src.data.wallet_reconciliation.fetch_pusd_transfers",
                 return_value=TransferFetchResult(available=True, transfers=transfers),
             ):
            result = compute_wallet_reconciliation(WALLET, etherscan_api_key="k")

        assert result.expected_balance_usd == 70.0
        assert result.external_withdrawals_usd == 10.0

    def test_polymarket_internal_transfer_excluded_via_tx_hash_match(self):
        """A transfer whose tx hash IS in the Data API activity set must
        contribute nothing extra -- it's already inside
        activity_cash_flow_usd, not a second, separate deposit."""
        transfers = [_transfer("0xTRADE1", 999.0, to=WALLET_LOWER, frm="0xothertrader")]
        with self._patch_activity(80.0, tx_hashes={"0xtrade1"}), \
             patch(
                 "src.data.wallet_reconciliation.fetch_pusd_transfers",
                 return_value=TransferFetchResult(available=True, transfers=transfers),
             ):
            result = compute_wallet_reconciliation(WALLET, etherscan_api_key="k")

        assert result.expected_balance_usd == 80.0
        assert result.external_deposits_usd == 0.0
        assert result.external_withdrawals_usd == 0.0

    def test_missing_api_key_degrades_to_unverified_not_crash_or_silent_zero(self):
        with self._patch_activity(80.0), \
             patch(
                 "src.data.wallet_reconciliation.fetch_pusd_transfers",
                 return_value=TransferFetchResult(available=False, reason="no_api_key"),
             ):
            result = compute_wallet_reconciliation(WALLET, etherscan_api_key=None)

        assert result.activity_available is True
        assert result.transfers_unverified is True
        assert result.transfers_unverified_reason == "no_api_key"
        # Still produces a verdict from Data API activity alone -- never a
        # silent "assume zero external transfers" without flagging it.
        assert result.expected_balance_usd == 80.0

    def test_activity_unavailable_means_no_expected_balance_at_all(self):
        from src.data.wallet_reconciliation import ActivityFetchResult
        with patch(
            "src.data.wallet_reconciliation.fetch_captured_activity",
            return_value=ActivityFetchResult(available=False),
        ) as mock_activity, \
             patch("src.data.wallet_reconciliation.fetch_pusd_transfers") as mock_onchain:
            result = compute_wallet_reconciliation(WALLET, etherscan_api_key="k")

        assert result.activity_available is False
        assert result.expected_balance_usd is None
        mock_activity.assert_called_once()
        mock_onchain.assert_not_called()  # no point fetching on-chain data with nothing to reconcile it against
