"""Polymarket CLOB V2 client initialisation.

Required env vars:
  POLYMARKET_API_KEY           — L1 wallet private key (hex string, no 0x prefix)
  POLYMARKET_L2_API_KEY        — L2 API key (from create_or_derive_api_creds)
  POLYMARKET_L2_API_SECRET     — L2 API secret
  POLYMARKET_L2_API_PASSPHRASE — L2 API passphrase
  POLYMARKET_DEPOSIT_WALLET    — ERC-1967 deposit wallet address (CREATE2-derived)
  POLYMARKET_CHAIN_ID          — 137 for Polygon mainnet, 80002 for Amoy testnet
  POLYMARKET_HOST              — https://clob.polymarket.com (default)
"""
import os

from py_clob_client_v2 import ClobClient, SignatureTypeV2
from py_clob_client_v2.clob_types import ApiCreds


def get_clob_client() -> ClobClient:
    host = os.environ.get("POLYMARKET_HOST", "https://clob.polymarket.com")
    key = os.environ["POLYMARKET_API_KEY"]       # Raise if missing — fail fast
    chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
    deposit_wallet = os.environ["POLYMARKET_DEPOSIT_WALLET"]
    creds = ApiCreds(
        api_key=os.environ["POLYMARKET_L2_API_KEY"],
        api_secret=os.environ["POLYMARKET_L2_API_SECRET"],
        api_passphrase=os.environ["POLYMARKET_L2_API_PASSPHRASE"],
    )
    return ClobClient(
        host=host,
        key=key,
        chain_id=chain_id,
        creds=creds,
        signature_type=int(SignatureTypeV2.POLY_1271),
        funder=deposit_wallet,
    )


def check_clob_health() -> bool:
    """Return True if the CLOB API is reachable and auth is valid."""
    try:
        client = get_clob_client()
        resp = client.get_ok()
        return resp in (True, "OK") or (isinstance(resp, dict) and resp.get("status") == "OK")
    except Exception as e:
        print(f"[clob-health] {e}")
        return False
