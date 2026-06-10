"""Shared HTTP client with per-domain rate limiting, retry, and response caching.

Design decisions:
- User-Agent is mandatory: both api.weather.gov and aviationweather.gov block
  requests without it.
- DomainRateLimiter enforces a minimum gap between successive requests to the
  same host, across all threads/coroutines (lock-protected).
- fetch() retries on 429 / 503 with exponential backoff, honoring Retry-After.
- NWS /points cache: the grid-cell URL for a lat/lon never changes, so we
  cache it for the lifetime of the process (no TTL needed).
- Forecast cache (30-min TTL): NWS hourly forecast and Open-Meteo change at
  most hourly — polling faster is pure noise and burns quota.
- Polymarket market list (5-min TTL): markets open/close infrequently within
  a single session.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

USER_AGENT = (
    "MeteoEdge/1.0 "
    "(Polymarket weather-arbitrage research bot; "
    "contact: andre.freixo.santos@gmail.com)"
)

# ---------------------------------------------------------------------------
# Per-domain rate limiter
# ---------------------------------------------------------------------------

class DomainRateLimiter:
    """Enforce a minimum interval (seconds) between consecutive requests to
    the same domain.  Thread-safe via a per-domain lock slot."""

    def __init__(self, default_interval_s: float = 1.0):
        self._default = default_interval_s
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, domain: str, interval_s: float | None = None) -> None:
        gap = interval_s if interval_s is not None else self._default
        with self._lock:
            now = time.monotonic()
            last = self._last.get(domain, 0.0)
            remaining = gap - (now - last)
            if remaining > 0:
                time.sleep(remaining)
            self._last[domain] = time.monotonic()


# Singleton used by all callers in this process.
_limiter = DomainRateLimiter(default_interval_s=1.0)

# Per-domain overrides (seconds between requests).
_DOMAIN_INTERVALS: dict[str, float] = {
    "api.weather.gov": 1.2,           # conservative; NWS has undisclosed limit
    "aviationweather.gov": 0.5,
    "api.open-meteo.com": 0.5,
    "gamma-api.polymarket.com": 0.5,
}

# ---------------------------------------------------------------------------
# Core fetch with retry
# ---------------------------------------------------------------------------

def fetch(url: str, timeout: float = 15.0, max_retries: int = 3) -> httpx.Response:
    """GET *url* with User-Agent, domain rate limiting, and exponential
    backoff on 429 / 503 / transient network errors.

    Raises the last exception if all retries are exhausted.
    """
    from urllib.parse import urlparse
    domain = urlparse(url).netloc
    interval = _DOMAIN_INTERVALS.get(domain, 1.0)

    backoff = 2.0
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        _limiter.wait(domain, interval)
        try:
            r = httpx.get(
                url,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )

            if r.status_code in (429, 503):
                retry_after = float(r.headers.get("Retry-After", backoff))
                retry_after = min(retry_after, 60.0)  # cap at 60 s
                log.warning(
                    "[http] %s returned %s (attempt %s/%s), sleeping %ss",
                    domain, r.status_code, attempt + 1, max_retries, f"{retry_after:.0f}",
                )
                time.sleep(retry_after)
                backoff = min(backoff * 2, 60.0)
                continue

            r.raise_for_status()
            return r

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (429, 503):
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                last_exc = exc
            else:
                raise  # non-retriable 4xx / 5xx

        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
            log.warning(
                "[http] %s network error (%s) attempt %s/%s: %s",
                domain, type(exc).__name__, attempt + 1, max_retries, exc,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            last_exc = exc

    raise last_exc or RuntimeError(f"fetch failed after {max_retries} attempts: {url}")


# ---------------------------------------------------------------------------
# NWS /points cache  (no TTL — grid cell URLs are permanent)
# ---------------------------------------------------------------------------

_nws_points_cache: dict[tuple[float, float], str] = {}


def get_nws_forecast_url(lat: float, lon: float) -> str | None:
    """Return the NWS hourly-forecast URL for *lat/lon*, fetching once and
    caching permanently for the lifetime of the process.

    The /points endpoint maps coordinates to a static grid-cell URL that never
    changes, so re-fetching it every poll is pure waste.
    """
    key = (round(lat, 4), round(lon, 4))
    if key in _nws_points_cache:
        return _nws_points_cache[key]

    try:
        r = fetch(f"https://api.weather.gov/points/{lat},{lon}")
        url = r.json()["properties"]["forecastHourly"]
        _nws_points_cache[key] = url
        log.debug("[nws-points] cached forecast URL for (%s,%s)", lat, lon)
        return url
    except Exception as exc:
        # Cache None so we don't retry (and re-log) every poll — NWS only covers US coords
        _nws_points_cache[key] = None
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 404:
            log.info("[nws-points] (%s,%s) not covered by NWS (non-US station)", lat, lon)
        else:
            log.warning("[nws-points] (%s,%s) error: %s", lat, lon, exc)
        return None


# ---------------------------------------------------------------------------
# Generic JSON response cache with TTL
# ---------------------------------------------------------------------------

@dataclass
class _CacheEntry:
    value: Any
    expires: datetime


_json_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def cached_fetch_json(url: str, ttl_minutes: float = 30) -> Any:
    """GET *url* and return parsed JSON, serving from an in-memory cache
    for *ttl_minutes* before re-fetching.

    Returns None on error (caller should treat as "data unavailable").
    """
    now = datetime.utcnow()
    with _cache_lock:
        entry = _json_cache.get(url)
        if entry and now < entry.expires:
            return entry.value  # cache hit

    try:
        r = fetch(url)
        data = r.json()
        expires = now + timedelta(minutes=ttl_minutes)
        with _cache_lock:
            _json_cache[url] = _CacheEntry(value=data, expires=expires)
        return data
    except Exception as exc:
        log.warning("[cache] fetch error for %s: %s", url[:80], exc)
        # Return stale value if available rather than None
        with _cache_lock:
            entry = _json_cache.get(url)
            if entry:
                log.info("[cache] serving stale entry for %s", url[:80])
                return entry.value
        return None


def invalidate_cache(url: str) -> None:
    """Force a fresh fetch on the next call for *url*."""
    with _cache_lock:
        _json_cache.pop(url, None)
