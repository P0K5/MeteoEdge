"""Full on-chain + Data API wallet-balance reconciliation (issue #1345).

Rebuilds ``check_wallet_balance_drift``'s ``expected_balance`` from a cash-
accounting identity over the wallet's **entire transaction history**, not a
static ``COPY_LIVE_CAPITAL_USD`` constant plus an internal ledger of bot-only
trades. The static-capital model breaks the moment the wallet sees anything
outside the bot's own bookkeeping -- a manual trade, a deposit, a withdrawal
-- which is exactly what happened in production (operator confirmation
2026-10-08: a real ~$20 top-up, plus ongoing manual trades on the same
wallet). Done correctly::

    expected_balance = trades_and_redeems_cash_flow + external_deposits - external_withdrawals

should equal the real CLOB/exchange balance at all times; a residual
``drift`` then means an actual data gap or bug, not a modeling blind spot.

Two independent data sources, each with its own failure mode that must never
crash the other:

1. **Data API activity** (``src.data.polymarket_traders.get_wallet_activity``)
   -- every ``TRADE`` (buy/sell fill) and ``REDEEM`` (resolved-market payout)
   for the wallet. This is the "cash flow from Polymarket activity" term, and
   also the authoritative set of transaction hashes that on-chain transfers
   get checked against (see below).
2. **On-chain pUSD transfers** (``src.data.onchain_transfers.fetch_pusd_transfers``,
   Etherscan) -- every raw ERC-20 ``Transfer`` event in/out of the wallet.
   Gated on ``ETHERSCAN_API_KEY``; degrades to ``transfers_unverified=True``
   (never a crash, never a silent "zero external transfers" assumption) when
   the key is absent or the fetch fails.

**Classification -- why an address allowlist does not work (issue #1345
correction, 2026-10-08).** The issue's own filing proposed excluding
transfers to/from a fixed set of "known Polymarket contract" addresses (CTF
Exchange, Neg Risk Adapter, ...) as "already captured by the Data API".
Verified against ``POLYMARKET_DEPOSIT_WALLET``'s real transfer history: real
``matchOrders``/``matchOrdersAndPrepareCombinatorial...`` settlement moves
pUSD **directly between the two trading wallets** (maker -> taker), not
through a central exchange contract as an intermediary -- 895 of 1,071 real
transfers on this wallet were ``matchOrders`` calls landing on dozens of
distinct counterparty addresses, essentially none of which is a "Polymarket
contract" in any fixed, enumerable sense. A static allowlist would
misclassify nearly all real trade settlement as "external".

What verifiably works instead: match each on-chain transfer's own
transaction hash against the Data API activity's transaction hashes
(``classify_onchain_transfers`` below). 944 of 952 distinct transfer tx
hashes in this wallet's real history are present in the Data API activity
feed (correctly excluded, including all 88 ERC-4337 ``handleOps``-routed
transfers -- this approach needs no function-name heuristic to get those
right). The 8 that were NOT present: 3 netted to exactly $0 for this wallet
(no cash impact, safely ignored) and 5 were genuine external deposits via
``permit2TransferAndMulticall`` (Polymarket's funding/relay entry point),
one of them for exactly $20 -- the ground-truth top-up this issue names.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.data.onchain_transfers import TransferFetchResult, fetch_pusd_transfers
from src.data.polymarket_traders import ACTIVITY_CASH_FLOW_TYPES, get_wallet_activity

log = logging.getLogger(__name__)

#: Observed live 2026-10-08 against POLYMARKET_DEPOSIT_WALLET's real
#: transfer history: ``matchOrders`` and
#: ``matchOrdersAndPrepareCombinatorialPosition``-shaped method selectors.
#: **This list is an empirical observation of this one wallet's history on
#: one day, not a verified-against-Polymarket's-ABI-docs fact** -- flagged
#: explicitly per issue #1345's "flag clearly anywhere you had to assume
#: rather than verify" instruction. It is used only as a secondary signal
#: for transfers NOT already matched by transaction hash (see module
#: docstring) -- to tell apart a possible "trade settlement missing from the
#: Data API" (issue #1342's known gap; must stay unresolved, never guessed
#: as a deposit) from a genuine external deposit/withdrawal. Getting this
#: list wrong only ever costs an extra "unresolved" entry (conservative
#: failure mode), never a wrong deposit/withdrawal classification, because
#: zero cases of the former existed in the real history checked.
KNOWN_TRADE_SETTLEMENT_METHOD_IDS = {
    "0x3c2b4399",  # matchOrders(bytes32,...)
    "0xe75cf6e5",  # matchOrdersAndPrepareCombinatorialPosition(...)
}

_ZERO_NET_EPSILON_USD = 1e-6


@dataclass
class ActivityFetchResult:
    """Result of ``fetch_captured_activity()``. ``available=False`` means
    the Data API activity feed could not be fetched at all (first page
    failed) -- distinct from a wallet with genuinely zero activity
    (``available=True, tx_hashes=set(), cash_flow_usd=0.0``)."""
    available: bool
    tx_hashes: set = field(default_factory=set)
    cash_flow_usd: float = 0.0
    n_trades: int = 0
    n_redeems: int = 0
    truncated: bool = False


def fetch_captured_activity(wallet_address: str) -> ActivityFetchResult:
    """Fetch the Data API's ``TRADE``/``REDEEM`` activity for
    *wallet_address* and reduce it to the two things the reconciliation
    needs: the cash-flow total (BUY is cash out, SELL and REDEEM are cash
    in -- all three use the record's own ``usdcSize``, already in dollar
    terms, never re-derived from ``size * price`` to avoid a second,
    possibly-inconsistent computation) and the set of transaction hashes
    this activity covers (used by ``classify_onchain_transfers`` to exclude
    already-captured on-chain transfers).

    ``available=False`` (no cash_flow/tx_hashes signal at all) only when
    the very first page request raised -- a wallet with zero real activity
    returns ``available=True`` with an empty hash set and
    ``cash_flow_usd=0.0``, never ``available=False``.
    """
    records = get_wallet_activity(wallet_address)
    # get_wallet_activity() never raises; a first-page failure shows up as
    # an empty list with .truncated=True (mirrors
    # polymarket_traders.get_wallet_trades()'s own contract) -- treat that
    # specific combination as "we have no signal at all", not "zero
    # activity, verified".
    if not records and getattr(records, "truncated", False):
        return ActivityFetchResult(available=False)

    tx_hashes: set = set()
    cash_flow = 0.0
    n_trades = 0
    n_redeems = 0
    for r in records:
        rtype = r.get("type")
        if rtype not in ACTIVITY_CASH_FLOW_TYPES:
            continue
        tx_hash = r.get("transactionHash")
        if tx_hash:
            tx_hashes.add(tx_hash.lower())
        try:
            usdc_size = float(r.get("usdcSize") or 0.0)
        except (TypeError, ValueError):
            usdc_size = 0.0

        if rtype == "REDEEM":
            cash_flow += usdc_size
            n_redeems += 1
        elif rtype == "TRADE":
            side = (r.get("side") or "").upper()
            if side == "BUY":
                cash_flow -= usdc_size
            elif side == "SELL":
                cash_flow += usdc_size
            else:
                # Unrecognized side -- never guess a sign; excluded from
                # cash_flow but its tx hash is still recorded above so an
                # on-chain transfer for this same tx is still correctly
                # excluded from "external" (it IS Polymarket activity,
                # just one this function can't dollar-value confidently).
                log.warning(
                    "[wallet-reconciliation] TRADE record with unrecognized "
                    "side=%r (tx=%s...) excluded from cash-flow sum",
                    r.get("side"), str(tx_hash)[:14],
                )
                continue
            n_trades += 1

    return ActivityFetchResult(
        available=True, tx_hashes=tx_hashes, cash_flow_usd=cash_flow,
        n_trades=n_trades, n_redeems=n_redeems,
        truncated=getattr(records, "truncated", False),
    )


@dataclass
class ClassificationResult:
    external_deposits_usd: float = 0.0
    external_withdrawals_usd: float = 0.0
    external_deposits: list = field(default_factory=list)
    external_withdrawals: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    matched_count: int = 0
    ignored_zero_net_count: int = 0


def classify_onchain_transfers(
    transfers: list[dict], captured_tx_hashes: set, wallet_address: str,
) -> ClassificationResult:
    """Group *transfers* (raw Etherscan ``tokentx`` rows) by transaction
    hash, and classify each transaction's net effect on *wallet_address*'s
    pUSD balance as: already captured by the Data API (``captured_tx_hashes``
    membership -- skip, it's trade/redeem settlement already counted
    elsewhere), a genuine external deposit/withdrawal, or unresolved (looks
    like trade settlement by its method selector, but its hash is absent
    from the Data API -- the known gap precedent from #1342; never guessed
    into a deposit/withdrawal).

    See this module's docstring for why tx-hash matching, not an address
    allowlist, is the correct exclusion mechanism.
    """
    wallet_lower = wallet_address.lower()
    by_tx: dict = {}
    for t in transfers:
        by_tx.setdefault(t["hash"].lower(), []).append(t)

    result = ClassificationResult()
    for tx_hash, legs in by_tx.items():
        if tx_hash in captured_tx_hashes:
            result.matched_count += 1
            continue

        net = 0.0
        for leg in legs:
            try:
                value = int(leg["value"]) / 10 ** int(leg.get("tokenDecimal", 6))
            except (TypeError, ValueError, KeyError):
                continue
            if leg.get("to", "").lower() == wallet_lower:
                net += value
            if leg.get("from", "").lower() == wallet_lower:
                net -= value

        if abs(net) < _ZERO_NET_EPSILON_USD:
            result.ignored_zero_net_count += 1
            continue

        method_id = (legs[0].get("methodId") or "").lower()
        entry = {
            "transaction_hash": tx_hash,
            "amount_usd": round(abs(net), 6),
            "function_name": legs[0].get("functionName", ""),
            "timestamp": legs[0].get("timeStamp"),
        }
        if method_id in KNOWN_TRADE_SETTLEMENT_METHOD_IDS:
            entry["reason"] = "possible_trade_missing_from_data_api"
            result.unresolved.append(entry)
            log.warning(
                "[wallet-reconciliation] unresolved on-chain transfer tx=%s... "
                "($%.4f, method=%s) looks like trade settlement but is absent "
                "from the Data API activity feed -- not counted as deposit/"
                "withdrawal, not folded into drift (known Data-API gap, #1342)",
                tx_hash[:14], abs(net), method_id,
            )
            continue

        if net > 0:
            result.external_deposits_usd += net
            result.external_deposits.append(entry)
        else:
            result.external_withdrawals_usd += -net
            result.external_withdrawals.append(entry)

    return result


@dataclass
class ReconciliationResult:
    """Everything ``check_wallet_balance_drift`` needs to compute
    ``expected_balance`` and report what could/couldn't be verified.

    ``expected_balance_usd`` is ``None`` only when the Data API activity
    feed itself was unreachable (``activity_available=False``) -- there is
    then no cash-flow signal at all to build an expected balance from.
    When only the on-chain-transfer dimension is unavailable
    (``transfers_unverified=True``, e.g. no ``ETHERSCAN_API_KEY``),
    ``expected_balance_usd`` is still computed from Data API activity alone
    (external transfers contribute 0 that run) -- per issue #1345's
    "skip [only] the on-chain-transfer dimension... clearly report
    unverified" instruction, this must never silently skip the whole check
    just because one of its two data sources is degraded.
    """
    activity_available: bool
    expected_balance_usd: "float | None"
    activity_cash_flow_usd: float = 0.0
    external_deposits_usd: float = 0.0
    external_withdrawals_usd: float = 0.0
    #: Per-transfer detail behind the two totals above -- each entry shaped
    #: like ``classify_onchain_transfers``'s own ``entry`` dict
    #: (``transaction_hash``, ``amount_usd``, ``function_name``,
    #: ``timestamp``). Kept alongside the totals (not instead of them) so a
    #: caller that only wants the number (``check_wallet_balance_drift``)
    #: doesn't have to care, while a caller that needs to name a specific
    #: transaction (``src.scripts.verify_wallet_reconciliation``, e.g. to
    #: confirm the issue #1345 ground-truth $20 deposit by name) can.
    external_deposits: list = field(default_factory=list)
    external_withdrawals: list = field(default_factory=list)
    transfers_unverified: bool = True
    transfers_unverified_reason: "str | None" = None
    unresolved: list = field(default_factory=list)
    unresolved_usd: float = 0.0
    activity_truncated: bool = False
    onchain_truncated: bool = False


def compute_wallet_reconciliation(
    wallet_address: str, *, etherscan_api_key: "str | None" = None,
) -> ReconciliationResult:
    """Tie ``fetch_captured_activity`` + ``fetch_pusd_transfers`` +
    ``classify_onchain_transfers`` together into the single
    ``expected_balance`` figure (and its unresolved/unverified detail)
    ``check_wallet_balance_drift`` persists and compares against the real
    exchange balance.
    """
    activity = fetch_captured_activity(wallet_address)
    if not activity.available:
        return ReconciliationResult(activity_available=False, expected_balance_usd=None)

    onchain: TransferFetchResult = fetch_pusd_transfers(wallet_address, api_key=etherscan_api_key)

    if not onchain.available:
        log.warning(
            "[wallet-reconciliation] on-chain transfer history for %s... is "
            "UNVERIFIED this run (%s) -- expected balance below reflects "
            "Polymarket trade/redeem activity only; any real external "
            "deposit/withdrawal since the last verified run will show up "
            "as unexplained drift, not as a classified transfer",
            wallet_address[:10], onchain.reason,
        )
        return ReconciliationResult(
            activity_available=True,
            expected_balance_usd=activity.cash_flow_usd,
            activity_cash_flow_usd=activity.cash_flow_usd,
            transfers_unverified=True,
            transfers_unverified_reason=onchain.reason,
            activity_truncated=activity.truncated,
        )

    classification = classify_onchain_transfers(
        onchain.transfers, activity.tx_hashes, wallet_address,
    )
    expected_balance = (
        activity.cash_flow_usd
        + classification.external_deposits_usd
        - classification.external_withdrawals_usd
    )
    return ReconciliationResult(
        activity_available=True,
        expected_balance_usd=expected_balance,
        activity_cash_flow_usd=activity.cash_flow_usd,
        external_deposits_usd=classification.external_deposits_usd,
        external_withdrawals_usd=classification.external_withdrawals_usd,
        external_deposits=classification.external_deposits,
        external_withdrawals=classification.external_withdrawals,
        transfers_unverified=False,
        unresolved=classification.unresolved,
        unresolved_usd=sum(e["amount_usd"] for e in classification.unresolved),
        activity_truncated=activity.truncated,
        onchain_truncated=onchain.truncated,
    )
