"""Tests for src/execution/auth.py::get_clob_client (issue #1292).

Pins signature_type to POLY_1271 (3). Verified live on 2026-10-04: under this
type, with this key and deposit wallet, an order was accepted and cancelled, and
the funded balance ($20.78) was readable. Changing it breaks both.
"""
from unittest.mock import MagicMock, patch

from py_clob_client_v2 import SignatureTypeV2

from src.execution.auth import get_clob_client

_ENV = {
    "POLYMARKET_API_KEY": "0x" + "1" * 64,
    "POLYMARKET_L2_API_KEY": "l2-key",
    "POLYMARKET_L2_API_SECRET": "l2-secret",
    "POLYMARKET_L2_API_PASSPHRASE": "l2-passphrase",
    "POLYMARKET_DEPOSIT_WALLET": "0x" + "2" * 40,
}


def test_get_clob_client_uses_poly_1271_signature_type(monkeypatch):
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)

    with patch("src.execution.auth.ClobClient") as mock_clob_client:
        mock_clob_client.return_value = MagicMock()
        get_clob_client()

    assert mock_clob_client.call_count == 1
    _, kwargs = mock_clob_client.call_args
    assert kwargs["signature_type"] == int(SignatureTypeV2.POLY_1271)
    assert kwargs["funder"] == _ENV["POLYMARKET_DEPOSIT_WALLET"]
