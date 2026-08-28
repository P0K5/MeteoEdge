"""Tests for batch CLOB orderbook fetching (issue #171).

Covers:
- fetch_orderbooks_batch(): parallel fetch, deduplication, failure isolation
- _enrich_from_clob(): uses shared dict instead of calling get_orderbook directly
- _log_open_position_snapshots(): uses shared dict instead of calling get_orderbook directly
- scan_markets(): threads orderbooks dict through to _enrich_from_clob
"""
from collections import defaultdict
from unittest.mock import MagicMock, patch, call

import pytest

from src.data.polymarket import fetch_orderbooks_batch


# ---------------------------------------------------------------------------
# fetch_orderbooks_batch
# ---------------------------------------------------------------------------

class TestFetchOrderbooksBatch:

    def test_empty_input_returns_empty_dict(self):
        result = fetch_orderbooks_batch([])
        assert result == {}

    def test_single_token_returned(self):
        ob = {"bids": [{"price": "0.45", "size": "10"}], "asks": []}
        with patch("src.data.polymarket.get_orderbook", return_value=ob) as mock_gob:
            result = fetch_orderbooks_batch(["tok-A"])
        assert result == {"tok-A": ob}
        mock_gob.assert_called_once_with("tok-A")

    def test_deduplicates_token_ids(self):
        """Duplicate token IDs must result in exactly one get_orderbook call."""
        ob = {"bids": [], "asks": [{"price": "0.60", "size": "5"}]}
        with patch("src.data.polymarket.get_orderbook", return_value=ob) as mock_gob:
            result = fetch_orderbooks_batch(["tok-X", "tok-X", "tok-X"])
        assert mock_gob.call_count == 1
        assert result == {"tok-X": ob}

    def test_failed_token_returns_empty_dict_not_exception(self):
        """A RuntimeError for one token must not abort the batch; other tokens succeed."""
        good_ob = {"bids": [{"price": "0.55", "size": "20"}], "asks": []}

        def _side_effect(token_id):
            if token_id == "tok-bad":
                raise RuntimeError("CLOB 503")
            return good_ob

        with patch("src.data.polymarket.get_orderbook", side_effect=_side_effect):
            result = fetch_orderbooks_batch(["tok-good", "tok-bad"])

        assert result["tok-good"] == good_ob
        assert result["tok-bad"] == {}

    def test_multiple_tokens_all_fetched(self):
        """All supplied tokens appear in the result."""
        tokens = [f"tok-{i}" for i in range(20)]
        counter = {"n": 0}

        def _side_effect(token_id):
            counter["n"] += 1
            return {"bids": [], "asks": []}

        with patch("src.data.polymarket.get_orderbook", side_effect=_side_effect):
            result = fetch_orderbooks_batch(tokens)

        assert counter["n"] == 20
        assert set(result.keys()) == set(tokens)

    def test_all_failures_returns_all_empty(self):
        """When every fetch fails, all tokens map to empty dict."""
        with patch("src.data.polymarket.get_orderbook", side_effect=RuntimeError("boom")):
            result = fetch_orderbooks_batch(["tok-1", "tok-2"])
        assert result == {"tok-1": {}, "tok-2": {}}


# ---------------------------------------------------------------------------
# _enrich_from_clob with shared orderbooks dict
# ---------------------------------------------------------------------------

class TestEnrichFromClobSharedDict:

    def _make_bracket(self):
        from src.model.envelope import Bracket
        return Bracket(
            ticker="TEST-0x1234",
            low_f=80.0,
            high_f=84.0,
            yes_ask_cents=60,
            yes_ask_size=0,
            no_ask_cents=40,
            no_ask_size=0,
            yes_token_id="tok-YES",
            no_token_id="tok-NO",
        )

    def test_uses_shared_dict_not_get_orderbook(self):
        """When orderbooks is provided, get_orderbook must not be called."""
        from src.strategy.scanner import _enrich_from_clob

        shared = {
            "tok-YES": {"asks": [{"price": "0.55", "size": "10"}], "bids": []},
            "tok-NO":  {"asks": [{"price": "0.45", "size": "8"}], "bids": []},
        }
        bracket = self._make_bracket()

        with patch("src.strategy.scanner.get_orderbook") as mock_gob:
            with patch("src.strategy.scanner.ENABLE_CLOB_ENRICHMENT", True):
                _enrich_from_clob(bracket, orderbooks=shared)

        mock_gob.assert_not_called()
        assert bracket.yes_ask_cents == 55  # 0.55 * 100
        assert bracket.no_ask_cents == 45   # 0.45 * 100

    def test_missing_token_in_shared_dict_is_skipped_gracefully(self):
        """If a token_id is absent from shared dict, asks/bids default to [] and no error."""
        from src.strategy.scanner import _enrich_from_clob

        bracket = self._make_bracket()
        original_yes = bracket.yes_ask_cents
        original_no = bracket.no_ask_cents

        with patch("src.strategy.scanner.get_orderbook") as mock_gob:
            with patch("src.strategy.scanner.ENABLE_CLOB_ENRICHMENT", True):
                _enrich_from_clob(bracket, orderbooks={})  # empty dict — neither token present

        mock_gob.assert_not_called()
        # Prices unchanged when no data available
        assert bracket.yes_ask_cents == original_yes
        assert bracket.no_ask_cents == original_no

    def test_fallback_calls_get_orderbook_when_no_shared_dict(self):
        """When orderbooks=None, the original single-call fallback must execute."""
        from src.strategy.scanner import _enrich_from_clob

        ob = {"asks": [{"price": "0.72", "size": "5"}], "bids": []}
        bracket = self._make_bracket()

        with patch("src.strategy.scanner.get_orderbook", return_value=ob) as mock_gob:
            with patch("src.strategy.scanner.ENABLE_CLOB_ENRICHMENT", True):
                _enrich_from_clob(bracket, orderbooks=None)

        assert mock_gob.call_count == 2  # once per YES, once per NO
        assert bracket.yes_ask_cents == 72


# ---------------------------------------------------------------------------
# _enrich_from_clob: real bid/ask + depth persistence -- issue #1077
# ---------------------------------------------------------------------------

class TestEnrichFromClobBookDepth:

    def _make_bracket(self):
        from src.model.envelope import Bracket
        return Bracket(
            ticker="TEST-0x1234",
            low_f=80.0,
            high_f=84.0,
            yes_ask_cents=60,
            yes_ask_size=0,
            no_ask_cents=40,
            no_ask_size=0,
            yes_token_id="tok-YES",
            no_token_id="tok-NO",
        )

    def test_mocked_book_persists_bid_ask_and_depth_at_full_precision(self):
        """A well-formed book response persists bid/ask + top-3 depth levels
        on both sides at full sub-penny float precision, and marks status ok."""
        from src.strategy.scanner import _enrich_from_clob

        shared = {
            "tok-YES": {
                "bids": [
                    {"price": "0.401", "size": "120"},
                    {"price": "0.400", "size": "50"},
                    {"price": "0.399", "size": "10"},
                    {"price": "0.398", "size": "999"},  # 4th level -- must be dropped
                ],
                "asks": [{"price": "0.403", "size": "30"}],
            },
            "tok-NO": {
                "bids": [{"price": "0.596", "size": "15"}],
                "asks": [{"price": "0.599", "size": "200"}, {"price": "0.601", "size": "5"}],
            },
        }
        bracket = self._make_bracket()

        with patch("src.strategy.scanner.ENABLE_CLOB_ENRICHMENT", True):
            _enrich_from_clob(bracket, orderbooks=shared)

        assert bracket.yes_bid_raw == pytest.approx(0.401)
        assert bracket.no_bid_raw == pytest.approx(0.596)
        assert bracket.yes_price_raw == pytest.approx(0.403)  # top-of-book ask
        assert bracket.no_price_raw == pytest.approx(0.599)

        assert len(bracket.yes_bid_levels) == 3  # capped at top 3
        assert bracket.yes_bid_levels[0] == {"price": pytest.approx(0.401), "size": 120.0}
        # best-first: bids descending
        assert [lv["price"] for lv in bracket.yes_bid_levels] == pytest.approx([0.401, 0.400, 0.399])
        assert bracket.yes_ask_levels == [{"price": pytest.approx(0.403), "size": 30.0}]

        # asks ascending (best first)
        assert [lv["price"] for lv in bracket.no_ask_levels] == pytest.approx([0.599, 0.601])

        assert bracket.yes_book_status == "ok"
        assert bracket.no_book_status == "ok"

    def test_failed_fetch_writes_null_never_a_substituted_price(self):
        """A raised exception during fetch must never fabricate a price --
        every book field stays None and status is fetch_failed."""
        from src.strategy.scanner import _enrich_from_clob

        bracket = self._make_bracket()
        original_yes_ask = bracket.yes_ask_cents

        with patch("src.strategy.scanner.get_orderbook", side_effect=RuntimeError("CLOB 503")):
            with patch("src.strategy.scanner.ENABLE_CLOB_ENRICHMENT", True):
                _enrich_from_clob(bracket, orderbooks=None)

        assert bracket.yes_book_status == "fetch_failed"
        assert bracket.no_book_status == "fetch_failed"
        assert bracket.yes_bid_raw is None
        assert bracket.yes_bid_levels is None
        assert bracket.yes_ask_levels is None
        # Never fabricated -- ask price is untouched, not defaulted to 0.5 or similar.
        assert bracket.yes_ask_cents == original_yes_ask

    def test_empty_book_and_fetch_failed_are_distinguishable(self):
        """A genuinely empty book (fetched, zero levels) must produce a
        different status than a fetch failure -- never collapsed together."""
        from src.strategy.scanner import _enrich_from_clob

        # Batch fetch's own sentinel for a failed token is `{}` (no bids/asks
        # keys at all -- see fetch_orderbooks_batch); a real empty book still
        # carries the keys, just with empty lists.
        shared = {
            "tok-YES": {},                          # simulates a failed batch fetch
            "tok-NO": {"bids": [], "asks": []},      # simulates a genuinely empty book
        }
        bracket = self._make_bracket()

        with patch("src.strategy.scanner.ENABLE_CLOB_ENRICHMENT", True):
            _enrich_from_clob(bracket, orderbooks=shared)

        assert bracket.yes_book_status == "fetch_failed"
        assert bracket.no_book_status == "empty_book"
        assert bracket.yes_book_status != bracket.no_book_status
        assert bracket.no_bid_levels is None
        assert bracket.no_ask_levels is None

    def test_no_token_id_leaves_status_none_not_attempted(self):
        """A side with no token_id is never attempted -- status stays None,
        distinguishable from both fetch_failed and empty_book."""
        from src.model.envelope import Bracket
        from src.strategy.scanner import _enrich_from_clob

        bracket = Bracket(
            ticker="TEST-NOTOK", low_f=80.0, high_f=84.0,
            yes_ask_cents=60, yes_ask_size=0, no_ask_cents=40, no_ask_size=0,
            yes_token_id=None, no_token_id=None,
        )

        with patch("src.strategy.scanner.ENABLE_CLOB_ENRICHMENT", True):
            _enrich_from_clob(bracket, orderbooks={})

        assert bracket.yes_book_status is None
        assert bracket.no_book_status is None

    def test_disabled_flag_leaves_every_book_field_untouched(self):
        """ENABLE_CLOB_ENRICHMENT=False must not touch any book field at all."""
        from src.strategy.scanner import _enrich_from_clob

        shared = {"tok-YES": {"bids": [{"price": "0.4", "size": "1"}], "asks": []}}
        bracket = self._make_bracket()

        with patch("src.strategy.scanner.ENABLE_CLOB_ENRICHMENT", False):
            _enrich_from_clob(bracket, orderbooks=shared)

        assert bracket.yes_book_status is None
        assert bracket.yes_bid_raw is None
        assert bracket.yes_bid_levels is None


# ---------------------------------------------------------------------------
# _log_open_position_snapshots with shared orderbooks dict
# ---------------------------------------------------------------------------

class TestSnapshotLoggerSharedOrderbooks:

    def _make_open_position(self, token_id: str = "tok-A") -> dict:
        return {
            "no_token_id": token_id,
            "station": "WSSS",
            "bracket_low": 86.0,
            "bracket_high": 88.0,
            "price_cents": 40,
            "predicted_price": 38,
            "ticker": "TEST-WSSS-86-88",
            "order_id": "buy-1",
            "side": "NO",
        }

    def test_snapshot_logger_uses_shared_orderbooks_not_get_orderbook(self, tmp_path):
        """_log_open_position_snapshots must NOT call get_orderbook when a shared
        orderbooks dict is provided."""
        from src.execution.position_tracker import _log_open_position_snapshots

        shared = {
            "tok-A": {
                "bids": [{"price": "0.45", "size": "10"}],
                "asks": [{"price": "0.47", "size": "5"}],
            }
        }

        with (
            patch("src.execution.position_tracker.get_orderbook") as mock_gob,
            patch(
                "src.execution.position_tracker._load_open_all_positions",
                return_value=[self._make_open_position("tok-A")],
            ),
            patch("src.execution.position_tracker.LOG_DIR", tmp_path),
            patch("src.execution.position_tracker.rotated_path", return_value=tmp_path / "snaps.jsonl"),
            patch("src.execution.position_tracker.housekeep"),
        ):
            result = _log_open_position_snapshots(
                weather={},
                ts="2026-06-16T10:00:00+00:00",
                db=None,
                orderbooks=shared,
            )

        mock_gob.assert_not_called()
        assert len(result) == 1
        snap = result[0]["snap"]
        # Bid from shared dict: 0.45 * 100 = 45
        assert snap["no_best_bid"] == 45
        assert snap["no_best_ask"] == 47

    def test_snapshot_logger_fallback_calls_get_orderbook_when_no_shared_dict(self, tmp_path):
        """When orderbooks=None, get_orderbook must be called for each token."""
        from src.execution.position_tracker import _log_open_position_snapshots

        ob = {"bids": [{"price": "0.60", "size": "8"}], "asks": []}

        with (
            patch("src.execution.position_tracker.get_orderbook", return_value=ob) as mock_gob,
            patch(
                "src.execution.position_tracker._load_open_all_positions",
                return_value=[self._make_open_position("tok-B")],
            ),
            patch("src.execution.position_tracker.LOG_DIR", tmp_path),
            patch("src.execution.position_tracker.rotated_path", return_value=tmp_path / "snaps.jsonl"),
            patch("src.execution.position_tracker.housekeep"),
        ):
            result = _log_open_position_snapshots(
                weather={},
                ts="2026-06-16T10:00:00+00:00",
                db=None,
                orderbooks=None,
            )

        mock_gob.assert_called_once_with("tok-B")
        assert len(result) == 1

    def test_no_positions_returns_empty_list_without_orderbook_calls(self):
        """When there are no open positions, no orderbook lookup should occur."""
        from src.execution.position_tracker import _log_open_position_snapshots

        with (
            patch("src.execution.position_tracker.get_orderbook") as mock_gob,
            patch(
                "src.execution.position_tracker._load_open_all_positions",
                return_value=[],
            ),
        ):
            result = _log_open_position_snapshots(
                weather={},
                ts="ts",
                db=None,
                orderbooks={"tok-A": {}},
            )

        mock_gob.assert_not_called()
        assert result == []
