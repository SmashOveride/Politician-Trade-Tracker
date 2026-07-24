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
            int(bool(t.get("ocr_sourced"))),
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
                 chamber, source_url, data_source, external_id, ocr_sourced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(data_source, external_id) DO UPDATE SET
                bioguide_id=excluded.bioguide_id,
                politician_name=excluded.politician_name,
                -- A human-reviewed row (see asset_quality.py and
                -- asset_description_reviewed's column comment in db.py)
                -- keeps its finalized ticker/name across every future
                -- re-parse of this filing, instead of a fresh (and
                -- possibly still-garbled, or differently-garbled) OCR
                -- pass silently reverting the review. Unqualified column
                -- names here refer to the EXISTING row (pre-update);
                -- `excluded.*` is the value this parse just produced.
                ticker = CASE WHEN asset_description_reviewed = 1 THEN ticker ELSE excluded.ticker END,
                asset_description = CASE WHEN asset_description_reviewed = 1 THEN asset_description ELSE excluded.asset_description END,
                transaction_type=excluded.transaction_type,
                transaction_date=excluded.transaction_date,
                disclosure_date=excluded.disclosure_date,
                amount_range=excluded.amount_range,
                amount_min=excluded.amount_min,
                amount_max=excluded.amount_max,
                chamber=excluded.chamber,
                source_url=excluded.source_url,
                ocr_sourced=excluded.ocr_sourced
            """,
            rows,
        )
    return len(rows)


def purge_stale_filing_rows(source):
    """Deletes trades left behind by an earlier parse of a filing that no
    longer matches how that filing parses now. Two cases, both gated on
    the filing's LATEST parse having succeeded (pf.status = 'ok') -- a
    failed re-parse never wipes previously-loaded data:

    1. Numeric line-index rows ('#N') at or beyond the filing's CURRENT
       transaction count. The upsert above only writes indexes the latest
       parse produced -- when a parser improvement makes a re-parsed
       filing yield FEWER transactions than a previous version did (e.g.
       junk rows a newer parser correctly rejects), the old higher-index
       rows would otherwise linger forever as stale phantoms (confirmed:
       602 such rows after a real parser upgrade).
    2. The single '#unreadable' placeholder row (see
       house_clerk._unreadable_placeholder) a filing gets when every
       parsing tier failed. Once that filing later parses successfully,
       the placeholder is obsolete and must go even though its (non-
       numeric) suffix can't be compared against trade_count the way case
       1's can -- SQLite's CAST('unreadable' AS INTEGER) silently
       evaluates to 0, which would otherwise never satisfy ">= trade_count"
       for any real trade_count and leave the placeholder behind forever
       alongside the now-correct data.

    A human-reviewed row (asset_description_reviewed=1) is exempt from
    both cases -- it represents a specific, deliberately identified
    disclosed transaction, and a later parse producing fewer rows for that
    page (a different run's OCR read slightly differently) doesn't mean
    the transaction stopped existing; it should never be silently deleted
    just because its line index no longer fits the newest parse's count.

    Returns the number of rows deleted."""
    with db.get_conn() as conn:
        cur = conn.execute(
            """
            DELETE FROM trades WHERE id IN (
                SELECT t.id FROM trades t
                JOIN processed_filings pf
                  ON pf.source = t.data_source
                 AND pf.filing_id = substr(t.external_id, 1, instr(t.external_id, '#') - 1)
                WHERE t.data_source = ?
                  AND pf.status = 'ok'
                  AND instr(t.external_id, '#') > 0
                  AND t.asset_description_reviewed != 1
                  AND (
                        t.external_id LIKE '%#unreadable'
                        OR CAST(substr(t.external_id, instr(t.external_id, '#') + 1) AS INTEGER)
                           >= pf.trade_count
                      )
            )
            """,
            (source,),
        )
        return cur.rowcount
