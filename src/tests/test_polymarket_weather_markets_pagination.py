"""Unit tests for get_weather_markets()'s cursor-based pagination
(issue #1434).

Covers:
- `/markets/keyset` is walked via `after_cursor`, not `offset` -- the Gamma
  API hard-rejects `offset >= 2100` for this query, and rejects `offset`
  outright on the keyset endpoint.
- Markets past where the old offset=2100 wall used to sit are present in
  the final result (the bug this issue fixes).
- A transient fetch failure mid-pagination (not "offset too large") raises
  rather than being treated as a silent, truncated "end of results".
- Existing dedup-by-conditionId behavior is preserved.
"""
from unittest.mock import patch

import pytest

from src.data.polymarket import get_weather_markets


def _market(mid, created_at):
    return {"conditionId": mid, "createdAt": created_at, "id": mid}


def _page(markets, next_cursor=None):
    page = {"markets": markets}
    if next_cursor:
        page["next_cursor"] = next_cursor
    return page


class TestGetWeatherMarketsPagination:
    def test_cursor_pagination_reaches_markets_past_old_offset_wall(self):
        # Three pages: the first two look like what offset=0..2099 used to
        # return (pre-cutoff markets); the third carries markets created
        # after the cutoff that offset=2100 used to make unreachable.
        page1 = _page(
            [_market(f"0x{i}", "2026-10-14T00:00:00Z") for i in range(100)],
            next_cursor="cursor-1",
        )
        page2 = _page(
            [_market(f"0x{100 + i}", "2026-10-14T12:00:00Z") for i in range(100)],
            next_cursor="cursor-2",
        )
        page3 = _page(
            [_market("0x-new-1", "2026-10-16T05:00:00Z"),
             _market("0x-new-2", "2026-10-16T06:00:00Z")],
            next_cursor=None,
        )
        responses = [page1, page2, page3]

        def fake_fetch(url, ttl_minutes=5):
            assert "offset=" not in url, "keyset endpoint rejects offset outright"
            assert "/markets/keyset" in url
            return responses.pop(0)

        with patch("src.data.polymarket.cached_fetch_json", side_effect=fake_fetch):
            result = get_weather_markets()

        ids = {m["conditionId"] for m in result}
        assert "0x-new-1" in ids
        assert "0x-new-2" in ids
        assert len(result) == 202

    def test_first_page_url_has_no_cursor_and_later_pages_pass_after_cursor(self):
        page1 = _page([_market("0xa", "2026-10-14T00:00:00Z")], next_cursor="cur-abc")
        page2 = _page([_market("0xb", "2026-10-15T00:00:00Z")], next_cursor=None)
        responses = [page1, page2]
        seen_urls = []

        def fake_fetch(url, ttl_minutes=5):
            seen_urls.append(url)
            return responses.pop(0)

        with patch("src.data.polymarket.cached_fetch_json", side_effect=fake_fetch):
            get_weather_markets()

        assert "after_cursor" not in seen_urls[0]
        assert "after_cursor=cur-abc" in seen_urls[1]

    def test_transient_fetch_failure_mid_pagination_raises(self):
        # First page succeeds with a next_cursor; the second page's fetch
        # fails (cached_fetch_json returns None on ANY error, including a
        # transient network failure -- NOT just "offset too large", since
        # this endpoint no longer uses offset at all).
        page1 = _page([_market("0xa", "2026-10-14T00:00:00Z")], next_cursor="cur-1")
        responses = [page1, None]

        def fake_fetch(url, ttl_minutes=5):
            return responses.pop(0)

        with patch("src.data.polymarket.cached_fetch_json", side_effect=fake_fetch):
            with pytest.raises(RuntimeError):
                get_weather_markets()

    def test_first_page_fetch_failure_raises_not_empty_list(self):
        with patch("src.data.polymarket.cached_fetch_json", return_value=None):
            with pytest.raises(RuntimeError):
                get_weather_markets()

    def test_dedup_by_condition_id_preserved(self):
        # Same conditionId appears twice across pages (shouldn't happen in
        # practice, but the old offset loop guarded against it -- preserve
        # that guarantee under cursor pagination too).
        page1 = _page(
            [_market("0xdup", "2026-10-14T00:00:00Z"), _market("0xunique", "2026-10-14T00:00:01Z")],
            next_cursor="cur-1",
        )
        page2 = _page([_market("0xdup", "2026-10-14T00:00:00Z")], next_cursor=None)
        responses = [page1, page2]

        def fake_fetch(url, ttl_minutes=5):
            return responses.pop(0)

        with patch("src.data.polymarket.cached_fetch_json", side_effect=fake_fetch):
            result = get_weather_markets()

        ids = [m["conditionId"] for m in result]
        assert ids.count("0xdup") == 1
        assert "0xunique" in ids
        assert len(result) == 2

    def test_empty_first_page_returns_empty_list_without_raising(self):
        with patch("src.data.polymarket.cached_fetch_json", return_value=_page([], next_cursor=None)):
            result = get_weather_markets()
        assert result == []
