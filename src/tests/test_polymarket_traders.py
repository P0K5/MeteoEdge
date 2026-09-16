"""Unit tests for src/data/polymarket_traders.py (copy-trading hypothesis
spike). All HTTP is mocked -- no network calls, matching
test_polymarket_final_price.py's pattern of patching
``src.data.polymarket.fetch``.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.data.polymarket_traders import (
    get_leaderboard,
    get_wallet_trades,
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

    def test_paginates_until_short_page(self):
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

    def test_empty_page_stops_immediately(self):
        with patch("src.data.polymarket_traders.fetch", return_value=_mock_response([])):
            result = get_wallet_trades(ADDRESS)
        assert result == []

    def test_partial_results_kept_on_page_failure(self):
        full_page = [{"i": i} for i in range(500)]
        with patch(
            "src.data.polymarket_traders.fetch",
            side_effect=[_mock_response(full_page), ConnectionError("boom")],
        ):
            result = get_wallet_trades(ADDRESS, page_size=500)
        assert len(result) == 500

    def test_respects_max_pages_hard_cap(self):
        full_page = [{"i": i} for i in range(10)]
        with patch(
            "src.data.polymarket_traders.fetch", return_value=_mock_response(full_page)
        ) as mock_fetch:
            get_wallet_trades(ADDRESS, page_size=10, max_pages=3)
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
        }
        result = normalize_trade(raw)
        assert result == {
            "market": "0xabc",
            "side": "BUY",
            "price": 0.65,
            "size": 10.5,
            "timestamp": 1700000000,
            "outcome": "Yes",
            "asset": None,
        }

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
