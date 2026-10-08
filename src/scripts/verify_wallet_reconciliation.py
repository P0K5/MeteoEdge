"""Manual, read-only verification CLI for the issue #1345 wallet
reconciliation -- run this once the operator has added a real
``ETHERSCAN_API_KEY`` to ``.env``, to confirm the rebuilt
``check_wallet_balance_drift`` logic against REAL production data before
trusting it. Never writes anything (no DB, no CLOB order calls) -- it only
reads ``POLYMARKET_DEPOSIT_WALLET``'s on-chain + Data API history and
prints a report.

**Why this script exists, and what it specifically checks for.** This
codebase's test suite (``src/tests/test_wallet_reconciliation.py``,
``test_onchain_transfers.py``) only covers the reconciliation logic against
synthetic/mocked Etherscan and Data API responses -- it cannot prove the
real, live computation is correct, because the author's own sandbox had a
working ``ETHERSCAN_API_KEY`` (the operator's) ALREADY present in `.env` at
implementation time, so this *was* actually run against live data during
development (see the PR description for the exact command and output) --
but you, the operator, should still re-run it yourself after pulling this
change, especially after rotating the key or changing
``POLYMARKET_DEPOSIT_WALLET``. The one thing this script checks explicitly,
by name, is the ground-truth fact this whole issue is named for: a real,
confirmed ~$20 external top-up in ``POLYMARKET_DEPOSIT_WALLET``'s history.
If ``--expect-deposit-usd`` (default 20.0) is not found among the detected
external deposits within ``--tolerance-usd``, this script exits non-zero
and says so plainly -- it does not just print a report and let you eyeball
it.

Usage::

    python -m src.scripts.verify_wallet_reconciliation
    python -m src.scripts.verify_wallet_reconciliation --wallet 0x... --expect-deposit-usd 20
    python -m src.scripts.verify_wallet_reconciliation --json

Exit codes: ``0`` clean (reconciliation ran, expected deposit found, no
unresolved transfers), ``1`` the reconciliation ran but something needs
operator attention (expected deposit not found, unresolved transfers
present, or on-chain transfers unverified), ``2`` could not run at all
(no wallet address, Data API unreachable).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.wallet_reconciliation import compute_wallet_reconciliation  # noqa: E402


def _closest_deposit_match(deposits: list[dict], expect_usd: float, tolerance_usd: float) -> "dict | None":
    candidates = [d for d in deposits if abs(d["amount_usd"] - expect_usd) <= tolerance_usd]
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs(d["amount_usd"] - expect_usd))


def build_report(wallet: str, *, etherscan_api_key: "str | None", expect_deposit_usd: float,
                  tolerance_usd: float) -> dict:
    result = compute_wallet_reconciliation(wallet, etherscan_api_key=etherscan_api_key)

    report = {
        "wallet": wallet,
        "activity_available": result.activity_available,
        "activity_cash_flow_usd": round(result.activity_cash_flow_usd, 6),
        "transfers_unverified": result.transfers_unverified,
        "transfers_unverified_reason": result.transfers_unverified_reason,
        "external_deposits_usd": round(result.external_deposits_usd, 6),
        "external_withdrawals_usd": round(result.external_withdrawals_usd, 6),
        "unresolved_usd": round(result.unresolved_usd, 6),
        "unresolved_count": len(result.unresolved),
        "unresolved": result.unresolved,
        "expected_balance_usd": (
            round(result.expected_balance_usd, 6) if result.expected_balance_usd is not None else None
        ),
        "activity_truncated": result.activity_truncated,
        "onchain_truncated": result.onchain_truncated,
    }

    if not result.activity_available:
        report["verdict"] = "FAIL: Data API activity feed unreachable -- cannot verify anything this run"
        report["ground_truth_deposit_found"] = False
        return report

    if result.transfers_unverified:
        report["verdict"] = (
            f"ATTENTION: on-chain transfers UNVERIFIED ({result.transfers_unverified_reason}) -- "
            "set ETHERSCAN_API_KEY in .env and re-run to check the ground-truth deposit"
        )
        report["ground_truth_deposit_found"] = False
        return report

    match = _closest_deposit_match(result.external_deposits, expect_deposit_usd, tolerance_usd)
    report["ground_truth_deposit_found"] = match is not None
    report["ground_truth_match"] = match

    problems = []
    if match is None:
        problems.append(
            f"expected deposit of ~${expect_deposit_usd:.2f} (+/- ${tolerance_usd:.2f}) NOT found "
            f"among detected external deposits (total external_deposits_usd=${result.external_deposits_usd:.4f})"
        )
    if result.unresolved:
        problems.append(f"{len(result.unresolved)} unresolved transfer(s) totalling ${result.unresolved_usd:.4f}")

    report["verdict"] = "PASS" if not problems else "FAIL: " + "; ".join(problems)
    return report


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--wallet", default=os.getenv("POLYMARKET_DEPOSIT_WALLET"),
                        help="Deposit wallet to reconcile (default: POLYMARKET_DEPOSIT_WALLET env var)")
    parser.add_argument("--etherscan-key", default=None,
                        help="Override ETHERSCAN_API_KEY env var for this run")
    parser.add_argument("--expect-deposit-usd", type=float, default=20.0,
                        help="The ground-truth external deposit this issue names (default: 20.0)")
    parser.add_argument("--tolerance-usd", type=float, default=0.5,
                        help="Matching tolerance for --expect-deposit-usd (default: 0.5)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a human-readable report")
    args = parser.parse_args(argv)

    if not args.wallet:
        print(
            "ABORTED: no deposit wallet (pass --wallet or set POLYMARKET_DEPOSIT_WALLET) -- "
            "nothing to reconcile against.",
            file=sys.stderr,
        )
        return 2

    report = build_report(
        args.wallet, etherscan_api_key=args.etherscan_key,
        expect_deposit_usd=args.expect_deposit_usd, tolerance_usd=args.tolerance_usd,
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"Wallet: {report['wallet']}")
        print(f"Data API activity available: {report['activity_available']}")
        print(f"On-chain transfers unverified: {report['transfers_unverified']} ({report['transfers_unverified_reason']})")
        print(f"Trades/redeems cash flow:     ${report['activity_cash_flow_usd']:.4f}")
        print(f"External deposits detected:   ${report['external_deposits_usd']:.4f}")
        print(f"External withdrawals detected: ${report['external_withdrawals_usd']:.4f}")
        print(f"Unresolved transfers:          {report['unresolved_count']} (${report['unresolved_usd']:.4f})")
        for u in report["unresolved"]:
            print(f"    - {u}")
        print(f"Expected balance:              ${report['expected_balance_usd']}")
        if report["activity_truncated"] or report["onchain_truncated"]:
            print(
                f"WARNING: pagination truncated (activity={report['activity_truncated']}, "
                f"onchain={report['onchain_truncated']}) -- this run may not cover the "
                "wallet's full history"
            )
        print()
        print(f"Ground-truth ~${args.expect_deposit_usd:.2f} deposit found: {report['ground_truth_deposit_found']}")
        if report.get("ground_truth_match"):
            print(f"    matched: {report['ground_truth_match']}")
        print()
        print(f"VERDICT: {report['verdict']}")

    return 0 if report.get("verdict") == "PASS" else (2 if not report["activity_available"] else 1)


if __name__ == "__main__":
    sys.exit(main())
