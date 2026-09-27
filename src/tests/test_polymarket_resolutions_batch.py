"""Unit tests for fetch_market_resolutions_batch() in src/data/polymarket.py
(issue #1227).

Covers:
- Batch of 3 ids -> one request, three repeated `condition_ids` keys, all
  three resolutions returned correctly.
- Response returning markets in a different order than requested -> each
  still matched to the right id via its own `conditionId`.
- Response missing one requested id -> that id is None/unresolved.
- Response containing a market whose `conditionId` was not requested ->
  ignored, never trusted (issue #867's guard, batched form).
- A batch request raising/timing out -> falls back to per-market resolution.
- Regression: URL uses repeated `condition_ids` keys, never a comma-joined
  list (the comma form silently returns an empty result -- see #1221's
  investigation, restated in #1227).
- Resolutions identical between the batched and unbatched paths for the
  same fixture markets.
- Batch size constant chunks large ticker lists into multiple requests.
"""
from unittest.mock import MagicMock, patch

from src.data.polymarket import (
    DEFAULT_RESOLUTION_BATCH_SIZE,
    fetch_market_final_price,
    fetch_market_resolution,
    fetch_market_resolutions_batch,
)


def _mock_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else []
    resp.raise_for_status.return_value = None
    return resp


def _market(condition_id, yes_price):
    return {"conditionId": condition_id, "outcomePrices": f'["{yes_price}", "{1 - yes_price}"]'}


class TestFetchMarketResolutionsBatch:
    def test_batch_of_three_resolved_in_one_request(self):
        market_data = [
            _market("0xaaa", 0.97),  # YES
            _market("0xbbb", 0.02),  # NO
            _market("0xccc", 0.99),  # YES
        ]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp) as mock_fetch:
            result = fetch_market_resolutions_batch(["0xaaa", "0xbbb", "0xccc"])
        assert mock_fetch.call_count == 1
        assert result == {"0xaaa": True, "0xbbb": False, "0xccc": True}

    def test_regression_uses_repeated_keys_not_comma_joined(self):
        # The Gamma API's plural condition_ids param only batches via
        # REPEATED query keys (condition_ids=A&condition_ids=B). A
        # comma-joined list (condition_ids=A,B) silently returns an empty
        # result -- the same shape as "market not found" -- so this is a
        # deliberate regression guard, not a style preference: anyone
        # re-deriving this from first principles would likely try the
        # comma form, see [], and wrongly conclude batching isn't
        # supported (see #1221's investigation / #1227's issue body).
        mock_resp = _mock_response(200, [_market("0xaaa", 0.97), _market("0xbbb", 0.02)])
        with patch("src.data.polymarket.fetch", return_value=mock_resp) as mock_fetch:
            fetch_market_resolutions_batch(["0xaaa", "0xbbb"])
        called_url = mock_fetch.call_args[0][0]
        assert "condition_ids=0xaaa" in called_url
        assert "condition_ids=0xbbb" in called_url
        assert "0xaaa,0xbbb" not in called_url
        assert "0xaaa%2C0xbbb" not in called_url
        # Two independent keys, not one comma-joined value.
        assert called_url.count("condition_ids=") == 2

    def test_response_order_does_not_matter(self):
        # Response comes back in the OPPOSITE order from the request.
        market_data = [_market("0xbbb", 0.02), _market("0xaaa", 0.97)]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_resolutions_batch(["0xaaa", "0xbbb"])
        assert result == {"0xaaa": True, "0xbbb": False}

    def test_missing_requested_id_is_unresolved(self):
        # Only 0xaaa comes back; 0xbbb was requested but absent.
        mock_resp = _mock_response(200, [_market("0xaaa", 0.97)])
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_resolutions_batch(["0xaaa", "0xbbb"])
        assert result == {"0xaaa": True, "0xbbb": None}

    def test_foreign_conditionid_in_response_is_ignored(self):
        # issue #867's guard, batched form: a market in the response whose
        # conditionId nobody asked for is never read for any requested id.
        market_data = [_market("0xforeign", 0.97)]
        mock_resp = _mock_response(200, market_data)
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_resolutions_batch(["0xaaa"])
        assert result == {"0xaaa": None}

    def test_batch_request_exception_falls_back_to_per_market(self):
        with patch("src.data.polymarket.fetch", side_effect=TimeoutError("boom")), patch(
            "src.data.polymarket.fetch_market_resolution",
        ) as mock_single:
            mock_single.side_effect = lambda t: {"0xaaa": True, "0xbbb": False}[t]
            result = fetch_market_resolutions_batch(["0xaaa", "0xbbb"])
        assert mock_single.call_count == 2
        assert result == {"0xaaa": True, "0xbbb": False}

    def test_batch_size_chunks_large_ticker_lists(self):
        tickers = [f"0x{i:03d}" for i in range(DEFAULT_RESOLUTION_BATCH_SIZE + 5)]
        mock_resp = _mock_response(200, [])
        with patch("src.data.polymarket.fetch", return_value=mock_resp) as mock_fetch:
            fetch_market_resolutions_batch(tickers)
        # DEFAULT_RESOLUTION_BATCH_SIZE + 5 tickers -> 2 chunks.
        assert mock_fetch.call_count == 2

    def test_duplicate_tickers_deduplicated(self):
        mock_resp = _mock_response(200, [_market("0xaaa", 0.97)])
        with patch("src.data.polymarket.fetch", return_value=mock_resp) as mock_fetch:
            result = fetch_market_resolutions_batch(["0xaaa", "0xaaa"])
        called_url = mock_fetch.call_args[0][0]
        assert called_url.count("condition_ids=") == 1
        assert result == {"0xaaa": True}

    def test_ambiguous_price_treated_as_unresolved(self):
        # Closed-but-not-UMA-resolved market: intermediate price, neither
        # threshold met -- same "treat as unresolved" rule as the
        # single-market path (issue #644).
        mock_resp = _mock_response(200, [_market("0xaaa", 0.50)])
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_resolutions_batch(["0xaaa"])
        assert result == {"0xaaa": None}

    def test_resolutions_identical_between_batched_and_unbatched_paths(self):
        fixture_markets = {
            "0xaaa": _market("0xaaa", 0.97),
            "0xbbb": _market("0xbbb", 0.02),
        }

        def _fetch_side_effect(url, **kwargs):
            # Single-market path hits .../markets?condition_ids=<id>&closed=true;
            # return that one market's fixture record regardless of batching.
            for ticker, market in fixture_markets.items():
                if f"condition_ids={ticker}" in url and "&condition_ids=" not in url:
                    return _mock_response(200, [market])
            return _mock_response(200, list(fixture_markets.values()))

        with patch("src.data.polymarket.fetch", side_effect=_fetch_side_effect):
            unbatched = {
                t: fetch_market_resolution(t) for t in fixture_markets
            }
            batched = fetch_market_resolutions_batch(list(fixture_markets.keys()))

        assert batched == unbatched


class TestParseOutcomeYesPriceSharedWithFinalPrice:
    """fetch_market_final_price still works after its parsing logic was
    pulled out into _parse_outcome_yes_price (issue #1227 refactor,
    shared with the batched path) -- guards against a refactor regression.
    """

    def test_fetch_market_final_price_unaffected_by_refactor(self):
        mock_resp = _mock_response(200, [_market("0xabc123def456", 0.97)])
        with patch("src.data.polymarket.fetch", return_value=mock_resp):
            result = fetch_market_final_price("0xabc123def456")
        assert result == 97
