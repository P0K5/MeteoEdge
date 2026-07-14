"""Settlement writer for MeteoEdge — records resolved market outcomes."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.db import Database


class SettlementWriter:
    """Writes settlement records to the settlements table.

    One settlement per market ticker (UNIQUE constraint). Safe to call
    multiple times for the same ticker — subsequent calls update the row.
    """

    def __init__(self, db: "Database") -> None:
        self.db = db

    def record_settlement(
        self,
        ticker: str,
        station: str,
        bracket_low: float,
        bracket_high: float,
        actual_high_f: float,
        resolved_yes: bool,
        market_final_price: int | None = None,
        resolution_source: str | None = None,
    ) -> None:
        """Upsert a settlement record. ticker is UNIQUE — idempotent."""
        ts = datetime.now(timezone.utc).isoformat()
        self.db.insert_settlement(
            ts=ts,
            station=station,
            ticker=ticker,
            bracket_low=bracket_low,
            bracket_high=bracket_high,
            actual_high_f=actual_high_f,
            resolved_yes=int(resolved_yes),
            market_final_price=market_final_price,
            resolution_source=resolution_source,
        )
