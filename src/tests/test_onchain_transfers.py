"""Unit tests for src/data/onchain_transfers.py (issue #1345). All HTTP is
mocked -- no real Etherscan calls -- mirroring test_polymarket_traders.py's
pattern of patching the module's own ``fetch``.
"""
from unittest.mock import MagicMock, patch

from src.data.onchain_transfers import (
    PUSD_CONTRACT_ADDRESS_CHAIN_137,
    fetch_pusd_transfers,
    pusd_contract_address,
)

WALLET = "0xb5f4c28c2F38a4f95c67054B013523994ba3a8B8"


def _mock_response(json_data):
    resp = MagicMock()
    resp.json.return_value = json_data
    return resp


def _ok(result):
    return {"status": "1", "message": "OK", "result": result}


def _no_key():
    return {"status": "0", "message": "NOTOK", "result": "Missing/Invalid API Key"}


def _no_transactions():
    return {"status": "0", "message": "No transactions found", "result": []}


class TestPusdContractAddress:
    def test_resolves_via_installed_clob_client_for_chain_137(self):
        # py_clob_client_v2 is a real, already-installed dependency of this
        # codebase (src/execution/auth.py) -- this is the live resolution
        # path, not a mock.
        assert pusd_contract_address(137) == PUSD_CONTRACT_ADDRESS_CHAIN_137

    def test_falls_back_to_hardcoded_constant_on_import_failure(self):
        with patch.dict("sys.modules", {"py_clob_client_v2.config": None}):
            assert pusd_contract_address(137) == PUSD_CONTRACT_ADDRESS_CHAIN_137


class TestFetchPusdTransfersKeyHandling:
    """Deliberately ``monkeypatch.delenv`` in every test here -- the dev
    machine this was written on has a real ``ETHERSCAN_API_KEY`` in
    ``.env``, and ``api_key=None`` means "let the function fall back to
    the environment" by design (mirrors every other lazily-read secret in
    this codebase), so these tests must not depend on the ambient
    environment to actually exercise the "no key at all" path."""

    def test_no_key_returns_unavailable_without_any_request(self, monkeypatch):
        monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
        with patch("src.data.onchain_transfers.fetch") as mock_fetch:
            result = fetch_pusd_transfers(WALLET, api_key=None)

        mock_fetch.assert_not_called()
        assert result.available is False
        assert result.reason == "no_api_key"
        assert result.transfers == []

    def test_explicit_empty_string_key_also_treated_as_absent(self, monkeypatch):
        monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
        result = fetch_pusd_transfers(WALLET, api_key="")
        assert result.available is False
        assert result.reason == "no_api_key"

    def test_invalid_key_etherscan_error_shape_reported_distinctly(self):
        """Etherscan returns HTTP 200 with a status=0/NOTOK body for an
        invalid key -- must be detected explicitly, never treated as
        "zero transfers"."""
        with patch(
            "src.data.onchain_transfers.fetch", return_value=_mock_response(_no_key()),
        ):
            result = fetch_pusd_transfers(WALLET, api_key="bad-key")

        assert result.available is False
        assert "etherscan_error" in result.reason
        assert result.transfers == []


class TestFetchPusdTransfersPagination:
    def test_single_short_page_stops_pagination(self):
        page = [{"hash": "0x1", "value": "1000000", "from": "0xa", "to": WALLET.lower()}]
        with patch(
            "src.data.onchain_transfers.fetch", return_value=_mock_response(_ok(page)),
        ) as mock_fetch:
            result = fetch_pusd_transfers(WALLET, api_key="k", page_size=1000)

        assert result.available is True
        assert result.truncated is False
        assert result.transfers == page
        mock_fetch.assert_called_once()
        assert "page=1" in mock_fetch.call_args[0][0]

    def test_full_page_then_short_page_covers_full_history(self):
        page1 = [{"hash": f"0x{i}", "value": "1", "from": "0xa", "to": WALLET} for i in range(2)]
        page2 = [{"hash": "0xlast", "value": "1", "from": "0xa", "to": WALLET}]
        with patch(
            "src.data.onchain_transfers.fetch",
            side_effect=[_mock_response(_ok(page1)), _mock_response(_ok(page2))],
        ):
            result = fetch_pusd_transfers(WALLET, api_key="k", page_size=2)

        assert result.available is True
        assert result.truncated is False
        assert len(result.transfers) == 3

    def test_genuinely_zero_transactions_is_a_confirmed_empty_success(self):
        with patch(
            "src.data.onchain_transfers.fetch", return_value=_mock_response(_no_transactions()),
        ):
            result = fetch_pusd_transfers(WALLET, api_key="k")

        assert result.available is True
        assert result.transfers == []
        assert result.truncated is False

    def test_first_page_request_exception_is_a_full_failure(self):
        with patch("src.data.onchain_transfers.fetch", side_effect=ConnectionError("boom")):
            result = fetch_pusd_transfers(WALLET, api_key="k")

        assert result.available is False
        assert "request_failed" in result.reason

    def test_mid_pagination_failure_keeps_partial_result_truncated(self):
        page1 = [{"hash": f"0x{i}", "value": "1", "from": "0xa", "to": WALLET} for i in range(2)]
        with patch(
            "src.data.onchain_transfers.fetch",
            side_effect=[_mock_response(_ok(page1)), ConnectionError("boom")],
        ):
            result = fetch_pusd_transfers(WALLET, api_key="k", page_size=2)

        assert result.available is True
        assert result.truncated is True
        assert len(result.transfers) == 2

    def test_depth_cap_without_any_short_page_is_truncated(self):
        full_page = [{"hash": f"0x{i}", "value": "1", "from": "0xa", "to": WALLET} for i in range(2)]
        with patch(
            "src.data.onchain_transfers.fetch", return_value=_mock_response(_ok(full_page)),
        ):
            result = fetch_pusd_transfers(WALLET, api_key="k", page_size=2, max_pages=3)

        assert result.available is True
        assert result.truncated is True
        assert len(result.transfers) == 6

    def test_request_url_includes_chainid_and_contract_address(self):
        with patch(
            "src.data.onchain_transfers.fetch", return_value=_mock_response(_ok([])),
        ) as mock_fetch:
            fetch_pusd_transfers(WALLET, api_key="my-key", chain_id=137)

        url = mock_fetch.call_args[0][0]
        assert "chainid=137" in url
        assert "action=tokentx" in url
        assert PUSD_CONTRACT_ADDRESS_CHAIN_137 in url
        assert "apikey=my-key" in url
