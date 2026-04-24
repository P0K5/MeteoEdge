"""Minimal Kalshi API wrapper. Read-only endpoints for the spike."""
import base64
import time
from datetime import datetime, timezone
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from config import KALSHI_API_BASE, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, HTTP_TIMEOUT_SECONDS, USER_AGENT, KALSHI_TEMP_SERIES


def _load_private_key():
    with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign_request(method: str, path: str) -> dict:
    """Kalshi RSA-PSS request signing. Returns headers."""
    private_key = _load_private_key()
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path}".encode("utf-8")

    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def _get_event(event_ticker: str) -> dict | None:
    """Fetch a single event by ticker. Returns None if not found (404)."""
    path = f"/events/{event_ticker}?with_nested_markets=true"
    url = f"{KALSHI_API_BASE}{path}"
    try:
        headers = _sign_request("GET", path)
        r = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("event")
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"Kalshi HTTP error {e.response.status_code}: {e.response.text}") from e
    except Exception as e:
        raise RuntimeError(f"Kalshi request failed: {e}") from e


def get_weather_events() -> list[dict]:
    """Fetch today's daily high temperature event for each configured city."""
    date_suffix = datetime.now(timezone.utc).strftime("%y%b%d").upper()  # e.g. 26APR21
    events = []
    for station, series in KALSHI_TEMP_SERIES.items():
        event_ticker = f"{series}-{date_suffix}"
        event = _get_event(event_ticker)
        if event:
            events.append(event)
            print(f"[kalshi] {event_ticker} OK ({len(event.get('markets', []))} markets)")
        else:
            print(f"[kalshi] {event_ticker} not found (wrong series ticker?)")
    return events


def get_orderbook(ticker: str) -> dict:
    """Return the orderbook for a given market ticker."""
    path = f"/markets/{ticker}/orderbook"
    url = f"{KALSHI_API_BASE}{path}"
    try:
        headers = _sign_request("GET", path)
        r = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"Kalshi HTTP error {e.response.status_code}: {e.response.text}") from e
    except Exception as e:
        raise RuntimeError(f"Kalshi orderbook request failed: {e}") from e
