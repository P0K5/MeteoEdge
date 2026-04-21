"""Minimal Kalshi API wrapper. Read-only endpoints for the spike."""
import base64
import time
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from config import KALSHI_API_BASE, KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, HTTP_TIMEOUT_SECONDS, USER_AGENT


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


def get_weather_events() -> list[dict]:
    """Return all open weather events. The spike filters to daily-high only."""
    path = "/events?status=open&with_nested_markets=true&limit=200"
    url = f"{KALSHI_API_BASE}{path}"
    try:
        headers = _sign_request("GET", path)
        r = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json().get("events", [])
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"Kalshi HTTP error {e.response.status_code}: {e.response.text}") from e
    except Exception as e:
        raise RuntimeError(f"Kalshi request failed: {e}") from e


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
