"""Unit tests for src/dashboard/api.py (Issue 2 — Epic 4).

Updated in Issue #369: removed live_state.json / STATE_PATH references;
enrichment is now sourced from the DB open_positions table.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_portfolio_cache():
    """Clear the issue #812 /api/portfolio cache around every test in this
    module so one test's cached response can't leak into the next (many
    tests here hit the endpoint repeatedly with different mocks)."""
    from src.dashboard import api
    original = dict(api._portfolio_cache)
    api._portfolio_cache["ts"] = 0.0
    api._portfolio_cache["data"] = None
    yield
    api._portfolio_cache.update(original)


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_returns_200(self):
        from src.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_returns_status_ok(self):
        from src.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.json()["status"] == "ok"

    def test_returns_ts_field(self):
        from src.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/health")
        assert "ts" in resp.json()


# ---------------------------------------------------------------------------
# /api/portfolio
# ---------------------------------------------------------------------------

class TestPortfolioEndpoint:
    def test_returns_200_with_no_db(self):
        """Portfolio endpoint returns 200 even when _db is None (no enrichment)."""
        from src.dashboard import api
        with patch.object(api, "_db", None):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                client = TestClient(api.app)
                resp = client.get("/api/portfolio")
        assert resp.status_code == 200

    @pytest.mark.skip(reason="pre-existing: issue #174 — Dashboard state isolation")
    def test_empty_open_positions_when_no_db(self):
        from src.dashboard import api
        with patch.object(api, "_db", None):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                client = TestClient(api.app)
                resp = client.get("/api/portfolio")
        assert resp.json()["open_positions"] == []

    def test_returns_expected_keys(self):
        from src.dashboard import api
        with patch.object(api, "_db", None):
            with patch.object(api, "_cash_usdc", return_value=42.0):
                with patch.object(api, "_midpoint_cents", return_value=65):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        data = resp.json()
        for key in ("cash_usdc", "open_positions", "updated_at"):
            assert key in data, f"Missing key: {key}"

    def test_cash_usdc_returned(self):
        from src.dashboard import api
        with patch.object(api, "_db", None):
            with patch.object(api, "_cash_usdc", return_value=99.5):
                client = TestClient(api.app)
                resp = client.get("/api/portfolio")
        assert resp.json()["cash_usdc"] == pytest.approx(99.5)

    @pytest.mark.skip(reason="pre-existing: issue #174 — Dashboard state isolation")
    def test_position_fields_present(self):
        from src.dashboard import api
        mock_ob = {
            "bids": [{"price": "0.63", "size": "100"}],
            "asks": [{"price": "0.67", "size": "100"}],
        }
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [{
            "token_id": "0xabc",
            "station": "KLAX",
            "bracket_low": 72.0,
            "bracket_high": 78.0,
            "predicted_price": 68,
        }]
        with patch.object(api, "_db", mock_db):
            with patch.object(api, "_cash_usdc", return_value=10.0):
                with patch("src.dashboard.api.get_orderbook", return_value=mock_ob):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        positions = resp.json()["open_positions"]
        assert len(positions) == 1
        pos = positions[0]
        for field in ("station", "side", "bracket_low", "bracket_high",
                      "entry_price", "market_prob", "my_prob", "edge",
                      "shares", "invested", "current_value", "target_value"):
            assert field in pos, f"Missing field: {field}"

    @pytest.mark.skip(reason="pre-existing: issue #174 — Dashboard state isolation")
    def test_market_prob_uses_clob_midpoint(self):
        from src.dashboard import api
        mock_ob = {
            "bids": [{"price": "0.62", "size": "100"}],
            "asks": [{"price": "0.66", "size": "100"}],
        }
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [{
            "token_id": "0xabc",
            "station": "KLAX",
            "bracket_low": 72.0,
            "bracket_high": 78.0,
            "predicted_price": 68,
        }]
        with patch.object(api, "_db", mock_db):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                with patch("src.dashboard.api.get_orderbook", return_value=mock_ob):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        pos = resp.json()["open_positions"][0]
        assert pos["market_prob"] == 64

    @pytest.mark.skip(reason="pre-existing: issue #174 — Dashboard state isolation")
    def test_orderbook_failure_falls_back_to_entry_price(self):
        from src.dashboard import api
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [{
            "token_id": "0xabc",
            "station": "KLAX",
            "bracket_low": 72.0,
            "bracket_high": 78.0,
            "predicted_price": 68,
        }]
        with patch.object(api, "_db", mock_db):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                with patch("src.dashboard.api.get_orderbook",
                           side_effect=RuntimeError("fetch failed")):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        assert resp.status_code == 200
        pos = resp.json()["open_positions"][0]
        assert pos["market_prob"] == pos["entry_price"]

    @pytest.mark.skip(reason="pre-existing: issue #174 — Dashboard state isolation")
    def test_single_orderbook_failure_does_not_break_endpoint(self):
        """Even with a failing orderbook, /api/portfolio returns 200."""
        from src.dashboard import api
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [{
            "token_id": "0x1",
            "station": "KATL",
            "bracket_low": 80.0,
            "bracket_high": 84.0,
            "predicted_price": 75,
        }]
        with patch.object(api, "_db", mock_db):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                with patch("src.dashboard.api.get_orderbook",
                           side_effect=RuntimeError("network error")):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        assert resp.status_code == 200
        assert len(resp.json()["open_positions"]) == 1


# ---------------------------------------------------------------------------
# /api/portfolio — issue #813: single CLOB client reuse + concurrency
# ---------------------------------------------------------------------------

class TestPortfolioClobClientReuse:
    """Only one CLOB client should be constructed per /api/portfolio request,
    and it must be the same instance handed to both the positions fetch and
    the cash fetch."""

    def test_builds_clob_client_once_and_shares_it(self):
        from src.dashboard import api

        sentinel_client = object()
        pos_mock = MagicMock(return_value=([], []))
        cash_mock = MagicMock(return_value=10.0)

        with patch.object(api, "_positions_from_wallet", pos_mock), \
             patch.object(api, "_cash_usdc", cash_mock), \
             patch("src.execution.auth.get_clob_client",
                   return_value=sentinel_client) as mock_get_client:
            client = TestClient(api.app)
            resp = client.get("/api/portfolio")

        assert resp.status_code == 200
        assert mock_get_client.call_count == 1
        pos_mock.assert_called_once_with(sentinel_client)
        cash_mock.assert_called_once_with(sentinel_client)

    def test_client_init_failure_falls_back_to_none_for_both_fetches(self):
        """No behavior-shape change: if building the shared client fails,
        both fetches still run (with client=None) and each is responsible
        for its own fallback, exactly as before #813."""
        from src.dashboard import api

        pos_mock = MagicMock(return_value=([], []))
        cash_mock = MagicMock(return_value=0.0)

        with patch.object(api, "_positions_from_wallet", pos_mock), \
             patch.object(api, "_cash_usdc", cash_mock), \
             patch("src.execution.auth.get_clob_client",
                   side_effect=RuntimeError("missing creds")):
            client = TestClient(api.app)
            resp = client.get("/api/portfolio")

        assert resp.status_code == 200
        pos_mock.assert_called_once_with(None)
        cash_mock.assert_called_once_with(None)


class TestPortfolioConcurrency:
    """USDC balance fetch and wallet-position fetch must run concurrently,
    not sequentially (issue #813 acceptance criteria)."""

    def test_cash_and_positions_fetched_concurrently(self):
        from src.dashboard import api

        def slow_positions(client):
            time.sleep(0.3)
            return [], []

        def slow_cash(client):
            time.sleep(0.3)
            return 42.0

        with patch.object(api, "_positions_from_wallet", side_effect=slow_positions), \
             patch.object(api, "_cash_usdc", side_effect=slow_cash), \
             patch("src.execution.auth.get_clob_client", return_value=MagicMock()):
            client = TestClient(api.app)
            start = time.monotonic()
            resp = client.get("/api/portfolio")
            elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert resp.json()["cash_usdc"] == pytest.approx(42.0)
        # Concurrent: total time ~= max(0.3, 0.3), not sum (0.6).
        assert elapsed < 0.5, f"expected concurrent execution, took {elapsed:.3f}s"


class TestCashUsdcClientReuse:
    """_cash_usdc() must reuse a passed-in client instead of rebuilding one."""

    def test_uses_provided_client_without_rebuilding(self):
        from src.dashboard import api

        mock_client = MagicMock()
        mock_trader = MagicMock()
        mock_trader.get_usdc_balance.return_value = 7.5

        with patch("src.execution.live_trader.LiveTrader",
                   return_value=mock_trader) as mock_lt, \
             patch("src.execution.auth.get_clob_client") as mock_get_client:
            result = api._cash_usdc(mock_client)

        assert result == 7.5
        mock_get_client.assert_not_called()
        mock_lt.assert_called_once_with(mock_client)

    def test_builds_own_client_when_none_provided(self):
        """Backward compatible: calling _cash_usdc() with no args (e.g. from
        existing tests/callers) still builds its own client."""
        from src.dashboard import api

        mock_client = object()
        mock_trader = MagicMock()
        mock_trader.get_usdc_balance.return_value = 3.0

        with patch("src.execution.live_trader.LiveTrader",
                   return_value=mock_trader) as mock_lt, \
             patch("src.execution.auth.get_clob_client",
                   return_value=mock_client) as mock_get_client:
            result = api._cash_usdc()

        assert result == 3.0
        mock_get_client.assert_called_once()
        mock_lt.assert_called_once_with(mock_client)


class TestPositionsFromWalletConcurrency:
    """Within _positions_from_wallet(), the midpoints batch call and the
    per-city NWS lookups must run concurrently (issue #813)."""

    @staticmethod
    def _fake_wallet_response(rows):
        class _FakeResp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        def _fake_get(url, timeout=None):
            if "offset=0" in url:
                return _FakeResp(rows)
            return _FakeResp([])

        return _fake_get

    def test_midpoints_and_nws_run_concurrently(self, monkeypatch):
        from src.dashboard import api

        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", "0x" + "1" * 40)

        wallet_row = {
            "asset": "tok1",
            "size": 10,
            "avgPrice": 0.5,
            "outcome": "YES",
            "title": "Will the high temperature in Chicago be above 80F?",
            "redeemable": False,
            "conditionId": "0xcond1",
            "currentValue": 5.0,
        }

        def slow_batch_midpoints(client, token_ids, fallbacks):
            time.sleep(0.3)
            return {t: 50 for t in token_ids}

        def slow_nws(lat, lon):
            time.sleep(0.3)
            return 82.0

        with patch.object(api, "_db", None), \
             patch.object(api, "httpx") as mock_httpx, \
             patch.object(api, "_batch_midpoints", side_effect=slow_batch_midpoints), \
             patch.object(api, "fetch_nws_forecast_high", side_effect=slow_nws):
            mock_httpx.get.side_effect = self._fake_wallet_response([wallet_row])
            start = time.monotonic()
            open_pos, _closed_pos = api._positions_from_wallet(client=MagicMock())
            elapsed = time.monotonic() - start

        assert len(open_pos) == 1
        assert open_pos[0].forecast_high_f == 82.0
        # Concurrent: total time ~= max(0.3, 0.3), not sum (0.6).
        assert elapsed < 0.5, f"expected concurrent execution, took {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# /api/portfolio — issue #812: short-TTL server-side cache
# ---------------------------------------------------------------------------

class TestPortfolioCache:
    """A short TTL cache should wrap the composite portfolio fetch so rapid
    repeat requests (multiple tabs, the 30s frontend poll firing early on a
    re-render) don't each re-pay the external round trip."""

    def test_ttl_is_a_named_constant_shorter_than_frontend_poll(self):
        from src.dashboard import api
        # The frontend polls /api/portfolio every 30s (index.html,
        # portfolioIntervalId) -- the cache TTL must stay below that so it
        # only absorbs bursts, never masks a genuinely new poll.
        assert api._PORTFOLIO_CACHE_TTL < 30.0

    def test_second_request_within_ttl_served_from_cache(self):
        from src.dashboard import api

        pos_mock = MagicMock(return_value=([], []))
        cash_mock = MagicMock(return_value=10.0)

        with patch.object(api, "_positions_from_wallet", pos_mock), \
             patch.object(api, "_cash_usdc", cash_mock), \
             patch("src.execution.auth.get_clob_client", return_value=MagicMock()):
            client = TestClient(api.app)
            resp1 = client.get("/api/portfolio")
            resp2 = client.get("/api/portfolio")

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json() == resp2.json()
        # Only the first request should hit the external fetches.
        pos_mock.assert_called_once()
        cash_mock.assert_called_once()

    def test_ttl_expiry_triggers_a_fresh_fetch(self):
        from src.dashboard import api

        pos_mock = MagicMock(return_value=([], []))
        cash_mock = MagicMock(side_effect=[10.0, 20.0])

        with patch.object(api, "_positions_from_wallet", pos_mock), \
             patch.object(api, "_cash_usdc", cash_mock), \
             patch("src.execution.auth.get_clob_client", return_value=MagicMock()):
            client = TestClient(api.app)
            resp1 = client.get("/api/portfolio")
            # Simulate TTL expiry without sleeping in the test.
            api._portfolio_cache["ts"] -= (api._PORTFOLIO_CACHE_TTL + 1.0)
            resp2 = client.get("/api/portfolio")

        assert resp1.json()["cash_usdc"] == pytest.approx(10.0)
        assert resp2.json()["cash_usdc"] == pytest.approx(20.0)
        assert pos_mock.call_count == 2
        assert cash_mock.call_count == 2

    def test_wallet_fetch_failure_is_not_cached(self):
        """A failed wallet fetch must not be cached, or the empty-positions
        fallback would be served for the full TTL instead of retrying the
        external call on the very next poll (issue #812 AC)."""
        from src.dashboard import api

        pos_mock = MagicMock(side_effect=RuntimeError("wallet API down"))
        cash_mock = MagicMock(return_value=0.0)

        with patch.object(api, "_positions_from_wallet", pos_mock), \
             patch.object(api, "_cash_usdc", cash_mock), \
             patch("src.execution.auth.get_clob_client", return_value=MagicMock()):
            client = TestClient(api.app)
            resp1 = client.get("/api/portfolio")
            resp2 = client.get("/api/portfolio")

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["open_positions"] == []
        assert resp2.json()["open_positions"] == []
        # Not cached -- each request retries the failing external call.
        assert pos_mock.call_count == 2

    def test_cache_populated_after_a_successful_fetch(self):
        from src.dashboard import api

        pos_mock = MagicMock(return_value=([], []))
        cash_mock = MagicMock(return_value=5.0)

        with patch.object(api, "_positions_from_wallet", pos_mock), \
             patch.object(api, "_cash_usdc", cash_mock), \
             patch("src.execution.auth.get_clob_client", return_value=MagicMock()):
            client = TestClient(api.app)
            client.get("/api/portfolio")

        assert api._portfolio_cache["data"] is not None
        assert api._portfolio_cache["ts"] > 0.0


# ---------------------------------------------------------------------------
# /api/portfolio — issue #814: trade history decoupled from the wallet fetch
# ---------------------------------------------------------------------------

class TestPortfolioHistoryDecoupling:
    """Trade history (closed_positions) must still be returned when the live
    wallet fetch is slow/failing (issue #814 AC) -- it comes from local
    DB/JSONL reads, unrelated to the failing wallet/CLOB call."""

    def test_wallet_fetch_failure_still_returns_trade_history(self):
        from src.dashboard import api
        from src.dashboard.api import ClosedPositionOut

        history = [
            ClosedPositionOut(
                station="KLAX", side="NO", entry_price=40, exit_price=0,
                pnl=-12.0, shares=30.0, closed_at="2026-10-01T00:00:00+00:00",
                token_id="tok-history", exit_reason="lost",
            ),
        ]

        pos_mock = MagicMock(side_effect=RuntimeError("wallet API timed out"))
        history_mock = MagicMock(return_value=(history, {}, set(), set()))
        cash_mock = MagicMock(return_value=0.0)

        with patch.object(api, "_positions_from_wallet", pos_mock), \
             patch.object(api, "_closed_positions_from_jsonl_and_db", history_mock), \
             patch.object(api, "_cash_usdc", cash_mock), \
             patch("src.execution.auth.get_clob_client", return_value=MagicMock()):
            client = TestClient(api.app)
            resp = client.get("/api/portfolio")

        assert resp.status_code == 200
        data = resp.json()
        assert data["open_positions"] == []
        assert len(data["closed_positions"]) == 1
        assert data["closed_positions"][0]["token_id"] == "tok-history"
        history_mock.assert_called_once()

    def test_wallet_fetch_slow_timeout_still_returns_trade_history(self):
        """A timeout (the other half of the test plan's "slow/failing") is
        just another exception from _positions_from_wallet()'s perspective --
        same fallback path as a hard failure."""
        import httpx as httpx_module
        from src.dashboard import api
        from src.dashboard.api import ClosedPositionOut

        history = [
            ClosedPositionOut(
                station="KORD", side="YES", entry_price=60, exit_price=100,
                pnl=20.0, shares=50.0, closed_at="2026-10-05T00:00:00+00:00",
                token_id="tok-timeout-history", exit_reason="won",
            ),
        ]

        pos_mock = MagicMock(side_effect=httpx_module.TimeoutException("timed out"))
        history_mock = MagicMock(return_value=(history, {}, set(), set()))
        cash_mock = MagicMock(return_value=0.0)

        with patch.object(api, "_positions_from_wallet", pos_mock), \
             patch.object(api, "_closed_positions_from_jsonl_and_db", history_mock), \
             patch.object(api, "_cash_usdc", cash_mock), \
             patch("src.execution.auth.get_clob_client", return_value=MagicMock()):
            client = TestClient(api.app)
            resp = client.get("/api/portfolio")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["closed_positions"]) == 1
        assert data["closed_positions"][0]["token_id"] == "tok-timeout-history"

    def test_history_fallback_itself_failing_does_not_break_endpoint(self):
        """Defense in depth: even if the local DB/JSONL fallback read also
        raises, /api/portfolio must still return 200 with empty history
        rather than propagating a 500."""
        from src.dashboard import api

        pos_mock = MagicMock(side_effect=RuntimeError("wallet API down"))
        history_mock = MagicMock(side_effect=RuntimeError("DB unavailable"))
        cash_mock = MagicMock(return_value=0.0)

        with patch.object(api, "_positions_from_wallet", pos_mock), \
             patch.object(api, "_closed_positions_from_jsonl_and_db", history_mock), \
             patch.object(api, "_cash_usdc", cash_mock), \
             patch("src.execution.auth.get_clob_client", return_value=MagicMock()):
            client = TestClient(api.app)
            resp = client.get("/api/portfolio")

        assert resp.status_code == 200
        assert resp.json()["closed_positions"] == []


# ---------------------------------------------------------------------------
# NWS forecast lookups — issue #814: process-level TTL cache across requests
# ---------------------------------------------------------------------------

class TestNwsForecastProcessCache:
    """_cached_nws_forecast_high() must cache per city across calls/requests
    (not just within one, as the old _nws_city_cache did), with a TTL."""

    def test_ttl_is_a_named_constant_of_several_minutes(self):
        from src.dashboard import api
        # 5-10 min per issue #814's fix proposal.
        assert 300.0 <= api._NWS_FORECAST_CACHE_TTL <= 600.0

    def test_second_call_within_ttl_is_served_from_cache(self):
        from src.dashboard import api

        fetch_mock = MagicMock(return_value=77.0)
        with patch.object(api, "fetch_nws_forecast_high", fetch_mock):
            first = api._cached_nws_forecast_high("Chicago", 41.97, -87.9)
            second = api._cached_nws_forecast_high("Chicago", 41.97, -87.9)

        assert first == 77.0
        assert second == 77.0
        fetch_mock.assert_called_once_with(41.97, -87.9)

    def test_different_cities_are_cached_independently(self):
        from src.dashboard import api

        fetch_mock = MagicMock(side_effect=[77.0, 55.0])
        with patch.object(api, "fetch_nws_forecast_high", fetch_mock):
            chicago = api._cached_nws_forecast_high("Chicago", 41.97, -87.9)
            atlanta = api._cached_nws_forecast_high("Atlanta", 33.64, -84.43)

        assert chicago == 77.0
        assert atlanta == 55.0
        assert fetch_mock.call_count == 2

    def test_ttl_expiry_triggers_a_fresh_fetch(self):
        from src.dashboard import api

        fetch_mock = MagicMock(side_effect=[77.0, 80.0])
        with patch.object(api, "fetch_nws_forecast_high", fetch_mock):
            api._cached_nws_forecast_high("Chicago", 41.97, -87.9)
            # Simulate TTL expiry without sleeping in the test.
            cached_ts, _ = api._NWS_FORECAST_CACHE["Chicago"]
            api._NWS_FORECAST_CACHE["Chicago"] = (
                cached_ts - (api._NWS_FORECAST_CACHE_TTL + 1.0), 77.0,
            )
            second = api._cached_nws_forecast_high("Chicago", 41.97, -87.9)

        assert second == 80.0
        assert fetch_mock.call_count == 2

    def test_fetch_failure_is_cached_too_not_retried_every_call(self):
        """A failure caches None for the TTL window rather than hammering a
        down NWS API on every subsequent call within the window."""
        from src.dashboard import api

        fetch_mock = MagicMock(side_effect=RuntimeError("NWS API down"))
        with patch.object(api, "fetch_nws_forecast_high", fetch_mock):
            first = api._cached_nws_forecast_high("Chicago", 41.97, -87.9)
            second = api._cached_nws_forecast_high("Chicago", 41.97, -87.9)

        assert first is None
        assert second is None
        fetch_mock.assert_called_once()

    def test_repeated_positions_from_wallet_calls_reuse_nws_cache(self, monkeypatch):
        """Two separate _positions_from_wallet() calls (standing in for two
        /api/portfolio polls within the NWS cache TTL) must only hit the NWS
        API once per city, not once per call."""
        from src.dashboard import api

        monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", "0x" + "2" * 40)

        wallet_row = {
            "asset": "tok-nws-reuse",
            "size": 10,
            "avgPrice": 0.5,
            "outcome": "YES",
            "title": "Will the high temperature in Chicago be above 80F?",
            "redeemable": False,
            "conditionId": "0xcondnws",
            "currentValue": 5.0,
        }

        class _FakeResp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        def _fake_get(url, timeout=None):
            if "offset=0" in url:
                return _FakeResp([wallet_row])
            return _FakeResp([])

        fetch_mock = MagicMock(return_value=82.0)

        with patch.object(api, "_db", None), \
             patch.object(api, "httpx") as mock_httpx, \
             patch.object(api, "fetch_nws_forecast_high", fetch_mock):
            mock_httpx.get.side_effect = _fake_get
            open_pos_1, _ = api._positions_from_wallet(client=MagicMock())
            open_pos_2, _ = api._positions_from_wallet(client=MagicMock())

        assert open_pos_1[0].forecast_high_f == 82.0
        assert open_pos_2[0].forecast_high_f == 82.0
        fetch_mock.assert_called_once()
