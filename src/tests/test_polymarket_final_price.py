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

from src.data.polymarket import fetch_market_final_price, fetch_market_resolution


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

    # -- issue #867: reject/select on conditionId instead of blind result[0] --

    def test_mismatched_condition_id_returns_none(self):
        """A single-element response whose conditionId != the requested ticker
        must never be read -- that is exactly the wrong-market-read defect
        #867 flagged (a spuriously YES bracket 3.6-21.6F away from the true
        one). Regardless of how plausible-looking its outcomePrices are, the
        function must return None (falls back to METAR) rather than trust it.
        """
        market_data = [{"conditionId": "0xSOMEOTHERMARKETENTIRELY", "outcomePrices": '["0.97", "0.03"]'}]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price(TICKER)
        assert result is None

    def test_mismatched_condition_id_snake_case_field_returns_none(self):
        """Same guard, but the API used the snake_case field name."""
        market_data = [{"condition_id": "0xSOMEOTHERMARKETENTIRELY", "outcomePrices": '["0.97", "0.03"]'}]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price(TICKER)
        assert result is None

    def test_multi_element_response_selects_matching_entry_not_first(self):
        """A multi-element response must have its matching entry selected --
        never blindly `result[0]`. Here the first element is a foreign market
        that would (before the fix) have been misread as this ticker's price;
        the correct entry, carrying the requested conditionId, is second.
        """
        market_data = [
            {"conditionId": "0xWRONGMARKETFIRSTINLIST", "outcomePrices": '["0.02", "0.98"]'},
            {"conditionId": TICKER, "outcomePrices": '["0.97", "0.03"]'},
        ]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price(TICKER)
        assert result == 97

    def test_multi_element_response_matching_entry_first_still_selected(self):
        """Matching entry already at index 0 among several -- still correct
        (guards against an off-by-one in the selection logic)."""
        market_data = [
            {"conditionId": TICKER, "outcomePrices": '["0.97", "0.03"]'},
            {"conditionId": "0xANOTHERFOREIGNMARKET", "outcomePrices": '["0.02", "0.98"]'},
        ]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price(TICKER)
        assert result == 97

    def test_matching_condition_id_is_case_insensitive(self):
        """0x hex condition IDs must match regardless of case."""
        market_data = [{"conditionId": TICKER.upper(), "outcomePrices": '["0.97", "0.03"]'}]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price(TICKER)
        assert result == 97

    def test_no_identifiable_condition_id_falls_back_to_first(self):
        """When NO entry carries a conditionId/condition_id at all, the match
        cannot be verified either way -- falls back to the legacy result[0]
        behaviour (this is also what every pre-#867 test stub above relies
        on: their mocked market dicts omit conditionId entirely)."""
        market_data = [{"outcomePrices": '["0.97", "0.03"]'}]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price(TICKER)
        assert result == 97


class TestFetchMarketResolution:
    """fetch_market_resolution() only accepts definitive extreme prices (#644)."""

    @pytest.mark.parametrize("price,expected", [
        (100, True), (97, True), (95, True),   # YES resolved
        (0, False), (3, False), (5, False),    # NO resolved
        (94, None), (50, None), (6, None),     # ambiguous -> unresolved
        (None, None),                          # not found / not closed / error
    ])
    def test_thresholds(self, price, expected):
        with patch("src.data.polymarket.fetch_market_final_price", return_value=price):
            assert fetch_market_resolution(TICKER) is expected
