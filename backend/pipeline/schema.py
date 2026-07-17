"""
Common normalized trade schema shared by every pipeline source (House Clerk
PDF parser, Senate eFD HTML parser, and the secondary/fallback JSON API
sources). Every source-specific parser ultimately produces a list of dicts
in this exact shape, which is what gets written into the `trades` table
(see db.py) -- the rest of the app never needs to know which source or
parser version a given row came from.
"""

from dataclasses import dataclass
from typing import Any, Dict

from ..normalize import norm_date, normalize_transaction_type, parse_amount_range

# The canonical set of keys every normalized trade dict has, matching the
# `trades` table's columns (minus the autoincrement id and the resolved
# bioguide_id, which is filled in later by the orchestrator once the
# legislator name index is available).
TRADE_FIELDS = (
    "politician_name",
    "ticker",
    "asset_description",
    "transaction_type",
    "transaction_date",
    "disclosure_date",
    "amount_range",
    "amount_min",
    "amount_max",
    "chamber",
    "source_url",
    "data_source",
    "external_id",
)


@dataclass
class RawFiling:
    """Metadata about one filing (one PDF or one eFD report page) shared by
    every transaction parsed out of it."""
    source: str          # 'house_clerk' | 'senate_efd' | ...
    filing_id: str        # this source's own stable id for the filing
    filer_name: str
    chamber: str           # 'house' | 'senate'
    source_url: str
    disclosure_date: str   # source's raw disclosure/filing date string


def normalize_trade(filing: RawFiling, raw_tx: Dict[str, Any], line_index: int = 0) -> Dict[str, Any]:
    """Converts one raw parsed transaction (source-specific keys: ticker,
    asset_description, raw_type, transaction_date, amount) plus its filing's
    metadata into the canonical trade dict shape (TRADE_FIELDS).

    `line_index` is this transaction's position (0-based) within its filing
    -- a single filing commonly discloses many transactions, so it's folded
    into external_id (as 'filing_id#line_index') to give each individual
    transaction its own stable identity. Without this, every transaction
    from the same filing would collide on the (data_source, external_id)
    upsert key used by loader.upsert_trades() and only the last one parsed
    would survive."""
    tx_date = norm_date(raw_tx.get("transaction_date", ""))
    disc_date = norm_date(filing.disclosure_date)
    ttype = normalize_transaction_type(raw_tx.get("raw_type", ""))
    amount = raw_tx.get("amount", "")
    amin, amax = parse_amount_range(amount)
    ticker = (raw_tx.get("ticker") or "").strip().upper() or None

    return {
        "politician_name": filing.filer_name,
        "ticker": ticker,
        "asset_description": raw_tx.get("asset_description", ""),
        "transaction_type": ttype,
        "transaction_date": tx_date,
        "disclosure_date": disc_date,
        "amount_range": amount,
        "amount_min": amin,
        "amount_max": amax,
        "chamber": filing.chamber,
        "source_url": filing.source_url,
        "data_source": filing.source,
        "external_id": f"{filing.filing_id}#{line_index}",
    }
