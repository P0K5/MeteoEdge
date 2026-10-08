"""Unit tests for src/data/polymarket_traders.py (copy-trading hypothesis
spike). All HTTP is mocked -- no network calls, matching
test_polymarket_final_price.py's pattern of patching
``src.data.polymarket.fetch``.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.data.polymarket_traders import (
    ACTIVITY_CASH_FLOW_TYPES,
    get_leaderboard,
    get_wallet_activity,
    get_wallet_trades,
    get_wallet_trades_since,
    normalize_trade,
    wallet_address,
)

ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"


def _mock_response(json_data):
    resp = MagicMock()
    resp.json.return_value = json_data
    return resp


class TestGetLeaderboard:
    def test_list_response_returned_verbatim(self):
        entries = [{"proxyWallet": ADDRESS, "profit": 100}]
        with patch(
            "src.data.polymarket_traders.fetch", return_value=_mock_response(entries)
        ) as mock_fetch:
            result = get_leaderboard(window="month", limit=10)
        assert result == entries
        called_url = mock_fetch.call_args[0][0]
        assert "/v1/leaderboard" in called_url
        assert "window=month" in called_url
        assert "limit=10" in called_url

    def test_dict_response_unwrapped(self):
        entries = [{"proxyWallet": ADDRESS}]
        with patch(
            "src.data.polymarket_traders.fetch",
            return_value=_mock_response({"leaderboard": entries}),
        ):
            result = get_leaderboard()
        assert result == entries

    def test_unrecognized_shape_returns_empty(self):
        with patch(
            "src.data.polymarket_traders.fetch",
            return_value=_mock_response({"unexpected": "shape"}),
        ):
            result = get_leaderboard()
        assert result == []

    def test_network_error_returns_empty_not_raises(self):
        with patch("src.data.polymarket_traders.fetch", side_effect=ConnectionError("boom")):
            result = get_leaderboard()
        assert result == []

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            get_leaderboard(window="decade")


class TestWalletAddress:
    @pytest.mark.parametrize(
        "key", ["proxyWallet", "proxy_wallet", "wallet", "address", "user"]
    )
    def test_recognizes_all_aliases(self, key):
        assert wallet_address({key: ADDRESS}) == ADDRESS

    def test_missing_field_returns_none(self):
        assert wallet_address({"profit": 100}) is None


class TestGetWalletTrades:
    def test_single_page_under_page_size_stops_pagination(self):
        page = [{"price": "0.5"}] * 3
        with patch(
            "src.data.polymarket_traders.fetch", return_value=_mock_response(page)
        ) as mock_fetch:
            result = get_wallet_trades(ADDRESS, page_size=500)
        assert result == page
        assert mock_fetch.call_count == 1

    def test_short_final_page_is_not_truncated(self):
        # Issue #1233's discriminating test: a short final page is genuine
        # exhaustion (the server confirming "that's everything"), and must
        # NOT be flagged truncated -- the exact opposite mistake from the
        # inert always-True/always-False gate this replaces.
        full_page = [{"i": i} for i in range(500)]
        short_page = [{"i": i} for i in range(500, 600)]
        with patch(
            "src.data.polymarket_traders.fetch",
            side_effect=[_mock_response(full_page), _mock_response(short_page)],
        ) as mock_fetch:
            result = get_wallet_trades(ADDRESS, page_size=500)
        assert len(result) == 600
        assert mock_fetch.call_count == 2
        second_url = mock_fetch.call_args_list[1][0][0]
        assert "offset=500" in second_url
        assert result.truncated is False

    def test_empty_page_stops_immediately_and_is_not_truncated(self):
        # An empty page is also genuine exhaustion (a wallet with zero
        # trades, or an offset already past the end) -- same discriminator
        # as a short page, not truncation.
        with patch("src.data.polymarket_traders.fetch", return_value=_mock_response([])):
            result = get_wallet_trades(ADDRESS)
        assert result == []
        assert result.truncated is False

    def test_page_failure_is_flagged_truncated_and_keeps_partial_results(self):
        # Issue #1233 test requirement: paging stopped by a 400 (or any
        # other failed request) -> flagged truncated, but the trades
        # collected on earlier pages are still returned, not discarded.
        full_page = [{"i": i} for i in range(500)]
        with patch(
            "src.data.polymarket_traders.fetch",
            side_effect=[_mock_response(full_page), ConnectionError("400 Bad Request")],
        ):
            result = get_wallet_trades(ADDRESS, page_size=500)
        assert len(result) == 500
        assert result.truncated is True

    def test_respects_max_pages_hard_cap_and_is_flagged_truncated(self):
        # Issue #1233 test requirement: paging stopped by reaching
        # max_pages (every page came back full, so the loop never saw its
        # own natural-exhaustion signal) -> flagged truncated.
        full_page = [{"i": i} for i in range(10)]
        with patch(
            "src.data.polymarket_traders.fetch", return_value=_mock_response(full_page)
        ) as mock_fetch:
            result = get_wallet_trades(ADDRESS, page_size=10, max_pages=3)
        assert mock_fetch.call_count == 3


class TestGetWalletActivity:
    """Issue #1345: /activity returns both TRADE and REDEEM records, each
    with its own transactionHash -- the field src.data.wallet_reconciliation
    needs to tell on-chain settlement transfers apart from genuine external
    deposits/withdrawals."""

    def test_hits_the_activity_endpoint_not_trades(self):
        with patch(
            "src.data.polymarket_traders.fetch", return_value=_mock_response([])
        ) as mock_fetch:
            get_wallet_activity(ADDRESS)
        called_url = mock_fetch.call_args[0][0]
        assert "/activity" in called_url
        assert "/trades" not in called_url

    def test_trade_and_redeem_records_both_returned_verbatim(self):
        records = [
            {"type": "TRADE", "transactionHash": "0xa", "usdcSize": 1.5, "side": "BUY"},
            {"type": "REDEEM", "transactionHash": "0xb", "usdcSize": 2.5},
        ]
        with patch("src.data.polymarket_traders.fetch", return_value=_mock_response(records)):
            result = get_wallet_activity(ADDRESS)
        assert result == records
        assert {r["type"] for r in result} <= ACTIVITY_CASH_FLOW_TYPES

    def test_other_activity_types_pass_through_unfiltered(self):
        """SPLIT/MERGE/REWARD/CONVERSION are real activity types seen live
        -- get_wallet_activity() itself doesn't filter them (callers that
        only want cash-flow-relevant records filter on
        ACTIVITY_CASH_FLOW_TYPES themselves, per this function's docstring)."""
        records = [{"type": "SPLIT", "transactionHash": "0xc"}]
        with patch("src.data.polymarket_traders.fetch", return_value=_mock_response(records)):
            result = get_wallet_activity(ADDRESS)
        assert result == records

    def test_short_final_page_is_not_truncated(self):
        full_page = [{"transactionHash": f"0x{i}", "type": "TRADE"} for i in range(500)]
        short_page = [{"transactionHash": "0xlast", "type": "REDEEM"}]
        with patch(
            "src.data.polymarket_traders.fetch",
            side_effect=[_mock_response(full_page), _mock_response(short_page)],
        ):
            result = get_wallet_activity(ADDRESS, page_size=500)
        assert len(result) == 501
        assert result.truncated is False

    def test_page_failure_is_flagged_truncated_and_keeps_partial_results(self):
        full_page = [{"transactionHash": f"0x{i}", "type": "TRADE"} for i in range(500)]
        with patch(
            "src.data.polymarket_traders.fetch",
            side_effect=[_mock_response(full_page), ConnectionError("boom")],
        ):
            result = get_wallet_activity(ADDRESS, page_size=500)
        assert len(result) == 500
        assert result.truncated is True
        assert result.truncated is True

    def test_regression_10500_trades_then_400_is_truncated(self):
        # Regression fixture for the live 0x5268527977 case (issue #1233):
        # 21 full pages of 500 (=10,500 trades), then the data-api's own
        # undocumented offset ceiling 400s on page 22 (offset=10,500).
        # Both previous fixes (#1209/#1211's wrong-units constant, and the
        # original inert QUALITY_MAX_TOTAL_TRADES=20,000 comparison) missed
        # exactly this case -- 10,500 sits comfortably under both those
        # constants, so neither ever flagged it.
        full_page = [{"i": i} for i in range(500)]
        responses = [_mock_response(full_page)] * 21 + [ConnectionError("400 Bad Request")]
        with patch("src.data.polymarket_traders.fetch", side_effect=responses):
            result = get_wallet_trades(ADDRESS, page_size=500)
        assert len(result) == 10500
        assert result.truncated is True


class TestGetWalletTradesSince:
    """get_wallet_trades_since() -- exploits the confirmed newest-first
    default (see get_wallet_trades()'s docstring) to stop paginating as
    soon as a trade at or before since_ts is seen."""

    def test_stops_at_first_page_when_all_trades_are_new(self):
        page = [{"timestamp": 300}, {"timestamp": 200}, {"timestamp": 100}]
        with patch(
            "src.data.polymarket_traders.fetch", return_value=_mock_response(page)
        ) as mock_fetch:
            result = get_wallet_trades_since(ADDRESS, since_ts=0, page_size=500)
        assert result == page
        assert mock_fetch.call_count == 1

    def test_stops_early_at_boundary_within_a_page(self):
        # Newest-first page; since_ts=150 should keep only the two trades
        # strictly newer than 150 and never fetch a second page.
        page = [{"timestamp": 300}, {"timestamp": 200}, {"timestamp": 100}]
        with patch(
            "src.data.polymarket_traders.fetch", return_value=_mock_response(page)
        ) as mock_fetch:
            result = get_wallet_trades_since(ADDRESS, since_ts=150, page_size=500)
        assert result == [{"timestamp": 300}, {"timestamp": 200}]
        assert mock_fetch.call_count == 1

    def test_walks_multiple_pages_when_boundary_not_yet_reached(self):
        # Newest-first pages: offset=0 is the most recent 500 trades
        # (1000..501), offset=500 the next 500 (500..1). since_ts=400 sits
        # in the second page, so page0 (all > 400) must be fully consumed
        # and a second fetch issued before the boundary is found.
        page0 = [{"timestamp": t} for t in range(1000, 500, -1)]  # 1000..501
        page1 = [{"timestamp": t} for t in range(500, 0, -1)]  # 500..1
        with patch(
            "src.data.polymarket_traders.fetch",
            side_effect=[_mock_response(page0), _mock_response(page1)],
        ) as mock_fetch:
            result = get_wallet_trades_since(ADDRESS, since_ts=400, page_size=500)
        assert mock_fetch.call_count == 2
        assert all(t["timestamp"] > 400 for t in result)
        assert len(result) == 600  # 1000..401

    def test_since_ts_zero_returns_everything_on_the_page(self):
        page = [{"timestamp": 5}, {"timestamp": 4}, {"timestamp": 3}]
        with patch("src.data.polymarket_traders.fetch", return_value=_mock_response(page)):
            result = get_wallet_trades_since(ADDRESS, since_ts=0, page_size=500)
        assert result == page

    def test_unparseable_timestamp_kept_but_not_used_as_boundary(self):
        page = [{"timestamp": "not-a-number"}, {"timestamp": 100}]
        with patch("src.data.polymarket_traders.fetch", return_value=_mock_response(page)):
            result = get_wallet_trades_since(ADDRESS, since_ts=50, page_size=500)
        assert result == page

    def test_network_error_degrades_to_partial_result(self):
        page0 = [{"timestamp": 500}] * 500
        with patch(
            "src.data.polymarket_traders.fetch",
            side_effect=[_mock_response(page0), ConnectionError("boom")],
        ):
            result = get_wallet_trades_since(ADDRESS, since_ts=0, page_size=500)
        assert result == page0

    def test_respects_max_pages_hard_cap(self):
        full_page = [{"timestamp": 999999 - i} for i in range(10)]
        with patch(
            "src.data.polymarket_traders.fetch", return_value=_mock_response(full_page)
        ) as mock_fetch:
            get_wallet_trades_since(ADDRESS, since_ts=0, page_size=10, max_pages=3)
        assert mock_fetch.call_count == 3


class TestNormalizeTrade:
    def test_valid_buy_trade(self):
        raw = {
            "conditionId": "0xabc",
            "side": "buy",
            "price": "0.65",
            "size": "10.5",
            "timestamp": "1700000000",
            "outcome": "Yes",
            "outcomeIndex": 0,
            "transactionHash": "0xdeadbeef",
        }
        result = normalize_trade(raw)
        assert result == {
            "market": "0xabc",
            "side": "BUY",
            "price": 0.65,
            "size": 10.5,
            "timestamp": 1700000000,
            "outcome": "Yes",
            "outcome_index": 0,
            "asset": None,
            "source_trade_id": "0xdeadbeef",
        }

    def test_missing_transaction_hash_defaults_to_none(self):
        raw = {
            "conditionId": "0xabc", "side": "BUY", "price": "0.65",
            "size": "10.5", "timestamp": "1700000000", "outcome": "Yes",
        }
        assert normalize_trade(raw)["source_trade_id"] is None

    def test_non_yes_no_outcome_label_kept_verbatim_with_index(self):
        # Live data-api.polymarket.com trades label outcomes like "Up"/"Down"
        # or team names, not literally "Yes"/"No" -- outcome_index is what
        # downstream code must use to know which side of the market this is.
        raw = {
            "conditionId": "0xabc", "side": "BUY", "price": "0.65",
            "size": "10.5", "timestamp": "1700000000",
            "outcome": "Up", "outcomeIndex": "0",
        }
        result = normalize_trade(raw)
        assert result["outcome"] == "Up"
        assert result["outcome_index"] == 0

    def test_missing_outcome_index_defaults_to_none(self):
        raw = {
            "conditionId": "0xabc", "side": "BUY", "price": "0.65",
            "size": "10.5", "timestamp": "1700000000", "outcome": "Yes",
        }
        assert normalize_trade(raw)["outcome_index"] is None

    def test_unparseable_outcome_index_defaults_to_none(self):
        raw = {
            "conditionId": "0xabc", "side": "BUY", "price": "0.65",
            "size": "10.5", "timestamp": "1700000000", "outcome": "Yes",
            "outcomeIndex": "not-an-int",
        }
        assert normalize_trade(raw)["outcome_index"] is None

    def test_missing_market_returns_none(self):
        raw = {"side": "BUY", "price": "0.5", "size": "1", "timestamp": "1"}
        assert normalize_trade(raw) is None

    def test_invalid_side_returns_none(self):
        raw = {
            "conditionId": "0xabc", "side": "HOLD", "price": "0.5",
            "size": "1", "timestamp": "1",
        }
        assert normalize_trade(raw) is None

    def test_unparseable_price_returns_none(self):
        raw = {
            "conditionId": "0xabc", "side": "BUY", "price": "not-a-number",
            "size": "1", "timestamp": "1",
        }
        assert normalize_trade(raw) is None

    def test_condition_id_alias_fallback(self):
        raw = {
            "condition_id": "0xdef", "side": "SELL", "price": "0.3",
            "size": "2", "timestamp": "5",
        }
        result = normalize_trade(raw)
        assert result["market"] == "0xdef"
        assert result["side"] == "SELL"
