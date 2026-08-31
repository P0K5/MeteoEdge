"""Unit tests for slug parsing and window construction (no network)."""
from __future__ import annotations

import pytest

from cryptoedge import gamma


@pytest.mark.parametrize("slug,expected", [
    ("btc-updown-5m-1788268500", ("btc", 5, 1788268500)),
    ("eth-updown-15m-1788268500", ("eth", 15, 1788268500)),
    ("zec-updown-15m-1", ("zec", 15, 1)),
])
def test_parse_slug(slug, expected):
    assert gamma.parse_slug(slug) == expected


@pytest.mark.parametrize("slug", [
    "", "not-a-slug", "btc-updown-5m", "will-trump-win-2028", "btc-updown-xm-123",
])
def test_parse_slug_rejects(slug):
    assert gamma.parse_slug(slug) is None


def test_window_slugs_aligns_to_window_boundary():
    # 1788268500 is a 5m boundary; 1788268543 sits 43s inside that window
    got = gamma.window_slugs(["btc"], [5], 1788268543, offsets=(0,))
    assert got == ["btc-updown-5m-1788268500"]


def test_window_slugs_covers_past_and_future():
    got = gamma.window_slugs(["btc"], [5], 1788268500, offsets=(-1, 0, 1))
    assert got == ["btc-updown-5m-1788268200",
                   "btc-updown-5m-1788268500",
                   "btc-updown-5m-1788268800"]


def test_window_slugs_multi_asset_and_window():
    # 1788268500 is divisible by both 300 and 900, so it is simultaneously a
    # 5m and a 15m boundary -- both series align on it.
    got = gamma.window_slugs(["btc", "eth"], [5, 15], 1788268500, offsets=(0,))
    assert set(got) == {"btc-updown-5m-1788268500", "eth-updown-5m-1788268500",
                        "btc-updown-15m-1788268500", "eth-updown-15m-1788268500"}


def test_window_slugs_15m_floors_a_non_boundary_time():
    # 1788268800 is a 5m boundary but NOT a 15m one -> 15m floors to ...8500
    got = gamma.window_slugs(["btc"], [15], 1788268800, offsets=(0,))
    assert got == ["btc-updown-15m-1788268500"]


def test_safe_float_never_fabricates():
    """#1028's lesson: a substituted price is indistinguishable from a real one."""
    assert gamma._f(None) is None
    assert gamma._f("") is None
    assert gamma._f("not-a-number") is None
    assert gamma._f("0.505") == 0.505
