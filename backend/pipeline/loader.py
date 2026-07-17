"""
Writes normalized trade dicts (see schema.py) produced by the pipeline's
source parsers into the shared `trades` table (db.py) -- the same table
data_fetch.py's legacy JSON-dump path writes into, so every source
ultimately lands in one place with one schema regardless of where it came
from.

Rows are upserted keyed on (data_source, external_id) so re-running the
pipeline over filings that were already loaded updates them in place
instead of duplicating them; this is what makes filing-level dedup (see
dedup.py) effective all the way through to the trades table, not just at
the download/parse stage.
"""

from .. import db
from ..normalize import resolve_bioguide, within_trade_history_window


def attribute_and_filter(normalized_trades, bioguide_lookup_by_name, cutoff=None):
    """Resolves each trade's bioguide_id from its disclosed politician_name
    (via the shared last-name index also used by data_fetch.py) and filters
    out rows older than `cutoff`, if given. Mutates and returns the list."""
    kept = []
    for t in normalized_trades:
        if cutoff and not within_trade_history_window(t.get("transaction_date"), cutoff):
            continue
        t["bioguide_id"] = resolve_bioguide(t.get("politician_name"), bioguide_lookup_by_name)
        kept.append(t)
    return kept


def upsert_trades(normalized_trades):
    """Inserts/updates rows in the trades table, one per normalized trade
    dict. Uses (data_source, external_id) as the natural key for filings
    that have one (i.e. every pipeline source, as opposed to the legacy
    aggregate-JSON path which has no stable per-transaction id and instead
    replaces its chamber's rows wholesale -- see data_fetch._replace_trades_for_chamber).
    Returns the number of rows written."""
    rows = [
        (
            t.get("bioguide_id"),
            t["politician_name"],
            t.get("ticker"),
            t.get("asset_description", ""),
            t.get("transaction_type", "unknown"),
            t.get("transaction_date", ""),
            t.get("disclosure_date", ""),
            t.get("amount_range", ""),
            t.get("amount_min"),
            t.get("amount_max"),
            t["chamber"],
            t.get("source_url", ""),
            t.get("data_source"),
            t.get("external_id"),
        )
        for t in normalized_trades
        # external_id is required for the upsert key to be meaningful --
        # rows without one (shouldn't happen for pipeline sources, but
        # guards against a parser bug silently duplicating rows) are skipped.
        if t.get("external_id") and t.get("data_source")
    ]
    if not rows:
        return 0

    with db.get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO trades
                (bioguide_id, politician_name, ticker, asset_description, transaction_type,
                 transaction_date, disclosure_date, amount_range, amount_min, amount_max,
                 chamber, source_url, data_source, external_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(data_source, external_id) DO UPDATE SET
                bioguide_id=excluded.bioguide_id,
                politician_name=excluded.politician_name,
                ticker=excluded.ticker,
                asset_description=excluded.asset_description,
                transaction_type=excluded.transaction_type,
                transaction_date=excluded.transaction_date,
                disclosure_date=excluded.disclosure_date,
                amount_range=excluded.amount_range,
                amount_min=excluded.amount_min,
                amount_max=excluded.amount_max,
                chamber=excluded.chamber,
                source_url=excluded.source_url
            """,
            rows,
        )
    return len(rows)
