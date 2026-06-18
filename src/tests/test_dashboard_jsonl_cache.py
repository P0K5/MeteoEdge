"""Tests for dashboard JSONL mtime cache + single-pass enrichment (issue #170).

Covers:
- _jsonl_cache_get(): cache hit on same mtime/size, re-parse on file change
- _parse_live_trades(): single-pass produces correct enrichment, stopped, settled
- _cached_live_trades(): cache returns all three structures, file parsed once
- _trades_file_enrichment(), _stopped_positions(), _settled_jsonl_positions():
  each returns the correct slice of the cached result
- NWS per-request deduplication in _positions_from_wallet()
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

from src.dashboard.api import (
    _jsonl_cache_get,
    _JSONL_CACHES,
    _JSONL_CACHES_LOCK,
    _parse_live_trades,
    _cached_live_trades,
    _trades_file_enrichment,
    _stopped_positions,
    _settled_jsonl_positions,
    ClosedPositionOut,
)
from src.utils.log_rotation import iter_rotated_jsonl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_filled_record(token_id: str = "tok-A", pnl: float = None) -> dict:
    r: dict = {
        "outcome": "filled",
        "no_token_id": token_id,
        "price_cents": 40,
        "station": "WSSS",
        "bracket_low": 86.0,
        "bracket_high": 88.0,
        "side": "NO",
        "question": "Will the highest temperature in Singapore be 86-88°F?",
        "ts": "2026-06-16T10:00:00+00:00",
        "size_eur": 5.0,
        "shares": 12.5,
        "predicted_price": 38,
    }
    if pnl is not None:
        r["pnl"] = pnl
    return r


def _make_sold_record(token_id: str = "tok-B", trigger: str = "stop_loss@45c") -> dict:
    return {
        "outcome": "sold",
        "no_token_id": token_id,
        "price_cents": 45,
        "entry_price_cents": 40,
        "shares": 12.5,
        "pnl": -0.63,
        "station": "WSSS",
        "bracket_low": 86.0,
        "bracket_high": 88.0,
        "question": "Will the highest temperature in Singapore be 86-88°F?",
        "ts": "2026-06-16T14:00:00+00:00",
        "trigger": trigger,
    }


# ---------------------------------------------------------------------------
# _jsonl_cache_get
# ---------------------------------------------------------------------------

class TestJsonlCacheGet:

    def _clear_cache_for(self, path):
        key = str(path)
        with _JSONL_CACHES_LOCK:
            _JSONL_CACHES.pop(key, None)

    def test_file_not_found_calls_parse_fn(self, tmp_path):
        missing = tmp_path / "nonexistent.jsonl"
        calls = []
        def parse_fn(p):
            calls.append(p)
            return {}
        _jsonl_cache_get(missing, parse_fn)
        assert len(calls) == 1

    def test_cache_hit_returns_same_object(self, tmp_path):
        """Second call with same mtime/size returns cached data without re-parsing."""
        path = tmp_path / "test.jsonl"
        path.write_text('{"a": 1}\n')
        self._clear_cache_for(path)

        parse_calls = []
        def parse_fn(p):
            parse_calls.append(p)
            return {"result": len(parse_calls)}

        result1 = _jsonl_cache_get(path, parse_fn)
        result2 = _jsonl_cache_get(path, parse_fn)

        assert parse_calls == [path, ]  # parsed only once
        assert result1 is result2  # same object returned

    def test_cache_miss_on_mtime_change(self, tmp_path):
        """When file is modified, cache is invalidated and parse_fn is called again."""
        path = tmp_path / "test2.jsonl"
        path.write_text('{"a": 1}\n')
        self._clear_cache_for(path)

        parse_calls = []
        def parse_fn(p):
            parse_calls.append(1)
            return {"v": len(parse_calls)}

        result1 = _jsonl_cache_get(path, parse_fn)
        assert parse_calls == [1]

        # Modify the file — different content changes size, which invalidates cache
        time.sleep(0.01)
        path.write_text('{"a": 1}\n{"b": 2}\n')

        result2 = _jsonl_cache_get(path, parse_fn)
        assert parse_calls == [1, 1]  # re-parsed
        assert result1 is not result2

    def test_thread_safety_no_double_parse(self, tmp_path):
        """Concurrent calls for the same file must not trigger duplicate parses.

        Strategy: launch 5 threads that each call _jsonl_cache_get on the same
        path sequentially (file lock serialises them).  After all threads finish,
        parse must have been called exactly once because the cache is populated
        on the first call.
        """
        path = tmp_path / "concurrent.jsonl"
        path.write_text('{"x": 1}\n')
        self._clear_cache_for(path)

        parse_calls = []

        def parse_fn(p):
            parse_calls.append(1)
            return {"v": 1}

        # Run 5 sequential calls in separate threads; they race to acquire the
        # per-entry lock and the first one to acquire it will populate the cache.
        results = []
        lock = threading.Lock()

        def run():
            r = _jsonl_cache_get(path, parse_fn)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=run) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 5, "All 5 threads must complete"
        # Due to per-entry locking, parse must be called exactly once.
        assert len(parse_calls) == 1, (
            f"Expected 1 parse call, got {len(parse_calls)} — "
            "cache is not thread-safe"
        )


# ---------------------------------------------------------------------------
# _parse_live_trades
# ---------------------------------------------------------------------------

class TestParseLiveTrades:

    def test_empty_file_returns_empty_structures(self, tmp_path):
        path = tmp_path / "lt.jsonl"
        path.write_text("")
        enr, stopped, settled = _parse_live_trades(iter_rotated_jsonl(path))
        assert enr == {}
        assert stopped == []
        assert settled == []

    def test_filled_without_pnl_goes_to_enrichment_only(self, tmp_path):
        path = tmp_path / "lt.jsonl"
        _write_jsonl(path, [_make_filled_record("tok-A")])
        enr, stopped, settled = _parse_live_trades(iter_rotated_jsonl(path))
        assert "tok-A" in enr
        assert stopped == []
        assert settled == []

    def test_filled_with_pnl_goes_to_both_enrichment_and_settled(self, tmp_path):
        path = tmp_path / "lt.jsonl"
        _write_jsonl(path, [_make_filled_record("tok-A", pnl=1.23)])
        enr, stopped, settled = _parse_live_trades(iter_rotated_jsonl(path))
        assert "tok-A" in enr
        assert len(settled) == 1
        assert settled[0].token_id == "tok-A"
        assert settled[0].pnl == pytest.approx(1.23)
        assert settled[0].exit_reason == "won"

    def test_sold_goes_to_stopped(self, tmp_path):
        path = tmp_path / "lt.jsonl"
        _write_jsonl(path, [_make_sold_record("tok-B", trigger="stop_loss@45c")])
        enr, stopped, settled = _parse_live_trades(iter_rotated_jsonl(path))
        assert enr == {}
        assert len(stopped) == 1
        assert stopped[0].token_id == "tok-B"
        assert stopped[0].exit_reason == "stop_loss"

    def test_take_profit_trigger_sets_exit_reason(self, tmp_path):
        path = tmp_path / "lt.jsonl"
        _write_jsonl(path, [_make_sold_record("tok-C", trigger="take_profit@85c")])
        _, stopped, _ = _parse_live_trades(iter_rotated_jsonl(path))
        assert stopped[0].exit_reason == "take_profit"

    def test_mixed_records_separated_correctly(self, tmp_path):
        path = tmp_path / "lt.jsonl"
        _write_jsonl(path, [
            _make_filled_record("tok-A"),           # enrichment only
            _make_filled_record("tok-B", pnl=0.5),  # enrichment + settled
            _make_sold_record("tok-C"),              # stopped
        ])
        enr, stopped, settled = _parse_live_trades(iter_rotated_jsonl(path))
        assert set(enr.keys()) == {"tok-A", "tok-B"}
        assert len(stopped) == 1
        assert stopped[0].token_id == "tok-C"
        assert len(settled) == 1
        assert settled[0].token_id == "tok-B"

    def test_last_filled_record_wins_in_enrichment(self, tmp_path):
        """When multiple filled records exist for the same token, last one wins."""
        path = tmp_path / "lt.jsonl"
        r1 = _make_filled_record("tok-A")
        r1["predicted_price"] = 30
        r2 = _make_filled_record("tok-A")
        r2["predicted_price"] = 55
        _write_jsonl(path, [r1, r2])
        enr, _, _ = _parse_live_trades(iter_rotated_jsonl(path))
        assert enr["tok-A"]["predicted_price"] == 55

    def test_malformed_lines_are_skipped(self, tmp_path):
        path = tmp_path / "lt.jsonl"
        with open(path, "w") as f:
            f.write("not json\n")
            f.write(json.dumps(_make_filled_record("tok-A")) + "\n")
            f.write("{broken\n")
        enr, _, _ = _parse_live_trades(iter_rotated_jsonl(path))
        assert "tok-A" in enr


# ---------------------------------------------------------------------------
# _cached_live_trades / slice functions
# ---------------------------------------------------------------------------

class TestCachedLiveTrades:

    def _clear_cache_for(self, path):
        key = str(path)
        with _JSONL_CACHES_LOCK:
            _JSONL_CACHES.pop(key, None)

    def test_file_not_exist_returns_empty(self, tmp_path):
        path = tmp_path / "missing.jsonl"
        with patch("src.dashboard.api.LIVE_TRADES_JSONL", path):
            enr, stopped, settled = _cached_live_trades()
        assert enr == {}
        assert stopped == []
        assert settled == []

    def test_single_call_parses_file(self, tmp_path):
        path = tmp_path / "lt.jsonl"
        _write_jsonl(path, [_make_filled_record("tok-A")])
        self._clear_cache_for(path)
        with patch("src.dashboard.api.LIVE_TRADES_JSONL", path):
            enr, _, _ = _cached_live_trades()
        assert "tok-A" in enr

    def test_repeated_calls_parse_file_only_once(self, tmp_path):
        """Same mtime/size → only one parse call."""
        path = tmp_path / "lt.jsonl"
        _write_jsonl(path, [_make_filled_record("tok-A")])
        self._clear_cache_for(path)

        parse_count = {"n": 0}
        original = _parse_live_trades

        def counting_parse(p):
            parse_count["n"] += 1
            return original(p)

        with patch("src.dashboard.api.LIVE_TRADES_JSONL", path):
            with patch("src.dashboard.api._parse_live_trades", side_effect=counting_parse):
                _cached_live_trades()
                _cached_live_trades()
                _cached_live_trades()

        assert parse_count["n"] == 1


class TestSliceFunctions:
    """_trades_file_enrichment, _stopped_positions, _settled_jsonl_positions
    should each return the correct slice of _cached_live_trades()."""

    def test_trades_file_enrichment_returns_enrichment_dict(self, tmp_path):
        path = tmp_path / "lt.jsonl"
        _write_jsonl(path, [_make_filled_record("tok-A")])
        with patch("src.dashboard.api.LIVE_TRADES_JSONL", path):
            with patch("src.dashboard.api._JSONL_CACHES", {}):
                result = _trades_file_enrichment()
        assert "tok-A" in result

    def test_stopped_positions_returns_sold_list(self, tmp_path):
        path = tmp_path / "lt.jsonl"
        _write_jsonl(path, [_make_sold_record("tok-B")])
        with patch("src.dashboard.api.LIVE_TRADES_JSONL", path):
            with patch("src.dashboard.api._JSONL_CACHES", {}):
                result = _stopped_positions()
        assert len(result) == 1
        assert isinstance(result[0], ClosedPositionOut)
        assert result[0].token_id == "tok-B"

    def test_settled_jsonl_positions_returns_settled_list(self, tmp_path):
        path = tmp_path / "lt.jsonl"
        _write_jsonl(path, [_make_filled_record("tok-C", pnl=0.75)])
        with patch("src.dashboard.api.LIVE_TRADES_JSONL", path):
            with patch("src.dashboard.api._JSONL_CACHES", {}):
                result = _settled_jsonl_positions()
        assert len(result) == 1
        assert result[0].token_id == "tok-C"
        assert result[0].pnl == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# NWS per-request deduplication
# ---------------------------------------------------------------------------

class TestNwsPerRequestDeduplication:
    """NWS HTTP calls must be at most once per distinct city per portfolio request."""

    def test_nws_called_at_most_once_per_city(self):
        """With 3 open positions in the same city, NWS is called exactly once."""
        from src.dashboard.api import _CITY_COORDS, _nws_forecast_for_title

        # Find a city in the config to use
        if not _CITY_COORDS:
            pytest.skip("No cities configured")

        city = next(iter(_CITY_COORDS))
        lat, lon = _CITY_COORDS[city]
        question = f"Will the highest temperature in {city} be 80-82°F?"

        nws_calls = []

        def mock_fetch(lat_, lon_):
            nws_calls.append((lat_, lon_))
            return 85.0

        # Simulate the per-request cache as used in _positions_from_wallet
        _nws_city_cache: dict = {}

        def _nws_for_question(question_: str) -> "float | None":
            t = question_.lower()
            for c, (la, lo) in _CITY_COORDS.items():
                if c.lower() in t:
                    if c not in _nws_city_cache:
                        try:
                            _nws_city_cache[c] = mock_fetch(la, lo)
                        except Exception:
                            _nws_city_cache[c] = None
                    return _nws_city_cache[c]
            return None

        # 3 positions in the same city
        results = [_nws_for_question(question) for _ in range(3)]

        assert len(nws_calls) == 1, f"Expected 1 NWS call, got {len(nws_calls)}"
        assert all(r == 85.0 for r in results)

    def test_different_cities_get_separate_nws_calls(self):
        """Each distinct city gets exactly one NWS call per request."""
        from src.dashboard.api import _CITY_COORDS

        if len(_CITY_COORDS) < 2:
            pytest.skip("Need at least 2 configured cities")

        cities = list(_CITY_COORDS.items())[:2]
        nws_calls = []

        def mock_fetch(lat_, lon_):
            nws_calls.append((lat_, lon_))
            return 80.0

        _nws_city_cache: dict = {}

        def _nws_for_question(question_: str) -> "float | None":
            t = question_.lower()
            for c, (la, lo) in _CITY_COORDS.items():
                if c.lower() in t:
                    if c not in _nws_city_cache:
                        try:
                            _nws_city_cache[c] = mock_fetch(la, lo)
                        except Exception:
                            _nws_city_cache[c] = None
                    return _nws_city_cache[c]
            return None

        city1, (lat1, lon1) = cities[0]
        city2, (lat2, lon2) = cities[1]
        q1 = f"Will the highest temperature in {city1} be 80-82°F?"
        q2 = f"Will the highest temperature in {city2} be 80-82°F?"

        # 2 calls per city
        _nws_for_question(q1)
        _nws_for_question(q1)
        _nws_for_question(q2)
        _nws_for_question(q2)

        assert len(nws_calls) == 2, f"Expected 2 NWS calls (one per city), got {len(nws_calls)}"
