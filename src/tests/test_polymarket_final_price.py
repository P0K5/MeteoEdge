"""Unit tests for fetch_market_final_price() in src/data/polymarket.py.

Covers:
- Old path-form (GET /markets/<ticker>) returns 422 → function returns None gracefully
- New query-param form (GET /markets?condition_ids=<ticker>&closed=true) returns market list
- Parses outcomePrices from response[0] correctly
- Returns None for empty response
- Returns None for network errors
"""
from unittest.mock import MagicMock, patch

import pytest

from src.data.polymarket import fetch_market_final_price


def _mock_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else []
    if status_code >= 400:
        from requests import HTTPError
        resp.raise_for_status.side_effect = HTTPError(f"HTTP {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


TICKER = "0xabc123def456"


class TestFetchMarketFinalPrice:
    def test_query_param_form_yes_won(self):
        """Successful query-param call: outcomePrices ~100 → returns ~100."""
        market_data = [{"outcomePrices": '["0.97", "0.03"]'}]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp) as mock_fetch:
            result = fetch_market_final_price(TICKER)
        # Verify new query-param URL was used
        called_url = mock_fetch.call_args[0][0]
        assert "condition_ids=" in called_url
        assert f"condition_ids={TICKER}" in called_url
        assert f"/markets/{TICKER}" not in called_url
        assert result == 97

    def test_query_param_form_no_won(self):
        """outcomePrices ~0 → returns ~0."""
        market_data = [{"outcomePrices": '["0.02", "0.98"]'}]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price(TICKER)
        assert result == 2

    def test_old_path_form_422_returns_none(self):
        """If server returns 422 (as old path form did), function returns None gracefully."""
        mock_resp = _mock_response(422)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price(TICKER)
        assert result is None

    def test_empty_response_returns_none(self):
        """Empty list from API → return None."""
        mock_resp = _mock_response(200, [])
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price(TICKER)
        assert result is None

    def test_network_error_returns_none(self):
        """Network failure → return None without raising."""
        with patch("src.data.polymarket.fetch", side_effect=ConnectionError("timeout")):
            result = fetch_market_final_price(TICKER)
        assert result is None

    def test_missing_outcome_prices_returns_none(self):
        """Market entry with no outcomePrices key → return None."""
        market_data = [{"conditionId": TICKER}]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price(TICKER)
        assert result is None

    def test_outcome_prices_as_list(self):
        """outcomePrices already a list (not string) is handled."""
        market_data = [{"outcomePrices": [0.99, 0.01]}]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price(TICKER)
        assert result == 99

    def test_url_contains_closed_true(self):
        """URL must include closed=true filter."""
        market_data = [{"outcomePrices": '["0.50", "0.50"]'}]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp) as mock_fetch:
            fetch_market_final_price(TICKER)
        called_url = mock_fetch.call_args[0][0]
        assert "closed=true" in called_url
