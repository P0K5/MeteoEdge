"""Raw on-chain ERC-20 ``Transfer`` event history for a wallet, via
Etherscan's unified v2 multichain API (issue #1345).

**Why this module exists.** The Polymarket Data API (``data-api.polymarket.com``,
wrapped by ``src.data.polymarket_traders``) only sees *Polymarket* activity --
trades, redemptions, splits/merges. It has no visibility into a wallet's raw
collateral-token transfers that have nothing to do with Polymarket at all:
a manual USDC-equivalent deposit, an external withdrawal. Those only exist
as ERC-20 ``Transfer`` events on the collateral token's contract on Polygon,
which requires reading them directly from a block explorer.

**The collateral token is pUSD, not USDC (corrected 2026-10-08).** This
issue's own filing assumed the wallet transacts in native USDC
(``0x3c499c...``) or bridged USDC.e (``0x2791Bca1...``). Live verification
against ``POLYMARKET_DEPOSIT_WALLET``'s real transfer history on 2026-10-08
found zero transfers on either of those contracts -- the wallet's actual
collateral token is Polymarket's own **pUSD ("Polymarket USD")**,
``0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`` on Polygon (chain 137), 6
decimals. This is independently confirmed by ``py_clob_client_v2`` (the CLOB
client this codebase already depends on, ``src/execution/auth.py``) --
``get_contract_config(137).collateral`` returns the same address -- so
``pusd_contract_address()`` below sources it from that installed package
at the same ``chain_id`` ``get_clob_client()`` uses (``POLYMARKET_CHAIN_ID``,
default 137), rather than hardcoding a guessed address. This is very likely
the collateral token for every Polymarket deposit wallet on this chain, not
a fact specific to one wallet -- but that has only been verified for this
one wallet's real history, so treat it as a strong default, not a universal
proof.

**Key handling.** ``ETHERSCAN_API_KEY`` is read lazily via ``os.environ``
(not a module-level ``src.config`` constant) at call time, mirroring
``src.execution.auth.get_clob_client()``'s own lazy-secret-read convention
-- this is what lets ``fetch_pusd_transfers()`` degrade cleanly (return an
explicit "unavailable, no key" result, never raise, never silently pretend
there are no external transfers) when the operator hasn't added the key
yet, confirmed live 2026-10-08: a keyless (or invalid-key) request returns
HTTP 200 with ``{"status":"0","message":"NOTOK","result":"Missing/Invalid
API Key"}`` -- a valid-looking response shape that must be detected
explicitly, not treated as "zero transfers".
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from src.config import ETHERSCAN_API_BASE, HTTP_TIMEOUT_SECONDS
from src.http_client import fetch

log = logging.getLogger(__name__)

#: Observed live 2026-10-08 against POLYMARKET_DEPOSIT_WALLET -- see this
#: module's docstring. Confirmed to equal py_clob_client_v2's own
#: `get_contract_config(137).collateral` (independent cross-check, not a
#: second guess).
PUSD_CONTRACT_ADDRESS_CHAIN_137 = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

#: Etherscan's own page-size ceiling for this endpoint on the free tier,
#: observed live 2026-10-08 (requests up to 1000/page succeeded). Kept as a
#: default, not a hardcoded request size, so a future paid-tier bump doesn't
#: require a code change.
DEFAULT_PAGE_SIZE = 1000
#: Defensive depth cap, mirrors polymarket_traders.MAX_TRADE_PAGES's own
#: "runaway guard, not a claim about where the server's real ceiling sits"
#: rationale.
MAX_PAGES = 40


def pusd_contract_address(chain_id: "int | None" = None) -> str:
    """Return the collateral (pUSD) ERC-20 contract address for *chain_id*
    (default: ``POLYMARKET_CHAIN_ID`` env var, falling back to 137 -- the
    same resolution ``src.execution.auth.get_clob_client()`` uses).

    Sources the address from ``py_clob_client_v2.config.get_contract_config``
    -- the installed CLOB client's own contract registry -- rather than a
    second hardcoded guess, so this module and the live trading client can
    never silently drift onto different addresses for the same chain. Falls
    back to ``PUSD_CONTRACT_ADDRESS_CHAIN_137`` (chain 137 only) if that
    import ever fails, logging a warning -- never raises.
    """
    if chain_id is None:
        chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
    try:
        from py_clob_client_v2.config import get_contract_config  # noqa: PLC0415
        return get_contract_config(chain_id).collateral
    except Exception as exc:
        if chain_id == 137:
            log.warning(
                "[onchain-transfers] could not resolve collateral address via "
                "py_clob_client_v2 (%s) -- falling back to the hardcoded "
                "chain-137 constant", exc,
            )
            return PUSD_CONTRACT_ADDRESS_CHAIN_137
        raise


@dataclass
class TransferFetchResult:
    """Result of ``fetch_pusd_transfers()`` -- distinguishes three outcomes
    that a caller must never conflate (issue #1345 acceptance criteria:
    "never silently pretend no external transfers exist"):

    - ``available=True, transfers=[...]`` -- the fetch succeeded; an empty
      list is a confirmed "this wallet genuinely has zero pUSD transfers",
      not "unknown".
    - ``available=False, reason="no_api_key"`` -- ``ETHERSCAN_API_KEY`` is
      unset; the on-chain-transfer dimension could not be checked at all.
    - ``available=False, reason="etherscan_error: ..."`` -- the key is set
      but the request failed (network error, invalid key, rate limit, etc).

    ``truncated`` is ``True`` iff pagination stopped for a reason that does
    NOT mean "this is genuinely all of this wallet's transfer history"
    (mirrors ``polymarket_traders.TradeList.truncated``'s own semantics) --
    only meaningful when ``available`` is ``True``.
    """
    available: bool
    transfers: list = field(default_factory=list)
    truncated: bool = False
    reason: "str | None" = None


def fetch_pusd_transfers(
    wallet_address: str,
    *,
    api_key: "str | None" = None,
    chain_id: "int | None" = None,
    contract_address: "str | None" = None,
    max_pages: int = MAX_PAGES,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> TransferFetchResult:
    """Fetch every pUSD ERC-20 ``Transfer`` event in/out of *wallet_address*
    on Polygon (chain 137 by default), via Etherscan's v2 multichain
    ``tokentx`` action.

    *api_key* defaults to ``os.environ.get("ETHERSCAN_API_KEY")`` -- read
    lazily so a caller/test can override it without touching the process
    environment. Returns immediately with
    ``TransferFetchResult(available=False, reason="no_api_key")`` -- no HTTP
    request at all -- when no key is available from either source, the same
    "operator hasn't set this up yet" absent-safe precedent
    ``check_wallet_balance_drift``'s own ``COPY_LIVE_CAPITAL_USD<=0`` gate
    and ``live_startup_sanity_check`` already establish elsewhere in this
    codebase.

    Paginated newest-first via Etherscan's own ``page``/``offset`` params
    (confirmed live 2026-10-08: page 1 returned 1000 rows, page 2 the
    remaining 71 for a ~1,071-row wallet history). A page that fails
    (network error, or Etherscan's "NOTOK" error shape -- e.g. an invalid
    key, or the rate limit) stops pagination; whatever was already
    collected is still returned, with ``truncated=True``, exactly mirroring
    ``polymarket_traders.get_wallet_trades()``'s own partial-result-over-no-
    result precedent. A first-page failure, though, is reported as a full
    ``available=False`` (not a truncated empty success) -- the distinction
    that matters here is "we have some signal, treat it as partial" vs "we
    have no signal at all, don't guess this wallet has zero transfers".

    Never raises.
    """
    key = api_key if api_key is not None else os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        log.warning(
            "[onchain-transfers] ETHERSCAN_API_KEY not set -- external "
            "transfer history for %s... is UNVERIFIED this run (on-chain "
            "deposits/withdrawals cannot be detected without it)",
            wallet_address[:10],
        )
        return TransferFetchResult(available=False, reason="no_api_key")

    if chain_id is None:
        chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
    if contract_address is None:
        contract_address = pusd_contract_address(chain_id)

    transfers: list[dict] = []
    truncated = False
    for page in range(1, max_pages + 1):
        url = (
            f"{ETHERSCAN_API_BASE}?chainid={chain_id}&module=account&action=tokentx"
            f"&address={wallet_address}&contractaddress={contract_address}"
            f"&page={page}&offset={page_size}&sort=asc&apikey={key}"
        )
        try:
            r = fetch(url, timeout=HTTP_TIMEOUT_SECONDS)
            payload = r.json()
        except Exception as exc:
            log.warning(
                "[onchain-transfers] fetch_pusd_transfers(%s...): page %s request "
                "failed: %s", wallet_address[:10], page, exc,
            )
            if page == 1:
                return TransferFetchResult(available=False, reason=f"request_failed: {exc}")
            truncated = True
            break

        status = payload.get("status")
        result = payload.get("result")
        if status != "1":
            message = payload.get("message", "")
            # Etherscan's own "genuinely zero results" signal for this
            # action is message=="No transactions found" with status=="0"
            # -- a real, confirmed-empty answer, not a fetch failure.
            # `result` itself is observed as either `[]` or `""` for this
            # case depending on the endpoint/version -- the message text is
            # the one stable signal, so branch on that, not on result's type.
            if message == "No transactions found":
                break
            log.warning(
                "[onchain-transfers] fetch_pusd_transfers(%s...): page %s "
                "Etherscan error: status=%s message=%s result=%r",
                wallet_address[:10], page, status, message, result,
            )
            if page == 1:
                return TransferFetchResult(
                    available=False, reason=f"etherscan_error: {message or result}",
                )
            truncated = True
            break

        if not isinstance(result, list) or not result:
            break
        transfers.extend(result)
        if len(result) < page_size:
            break
    else:
        truncated = True

    return TransferFetchResult(available=True, transfers=transfers, truncated=truncated)
