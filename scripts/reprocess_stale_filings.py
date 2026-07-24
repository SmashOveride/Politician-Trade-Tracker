"""
Targeted re-parse of specific House Clerk filing_ids under the CURRENT
parser (house_clerk.PARSER_VERSION), bypassing the full collect_trades()
year-index scan (which would re-check every filer's filings just to find
these few stale ones). Mirrors collect_trades' inner per-filing loop
exactly -- fetch, parse, normalize, dedup.record_result -- so the DB ends
up in the same state a real refresh would leave it in for these filings.

Politician name/bioguide_id and disclosure_date are looked up per filing
from that filing's existing trade rows (set by whichever parser version
processed it last) rather than hardcoded, so the same script works for any
politician's stale filings, not just one -- see the full-app OCR review
this was generalized for (scratch_all_stale_filings.json, split into
per-process chunks so several instances can run in parallel; each is a
separate OS process so they don't contend on Python's GIL during OCR
subprocess calls, and backend/db.py's get_conn() has a generous busy
timeout so their occasional concurrent writes don't collide).

Usage: python scripts/reprocess_stale_filings.py <input.json> [output.log]
  <input.json>: JSON list of "{year}:{doc_id}" filing_id strings
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.pipeline import dedup, house_clerk
from backend.pipeline.loader import upsert_trades, purge_stale_filing_rows
from backend import db


def main():
    if len(sys.argv) < 2:
        print("usage: python scripts/reprocess_stale_filings.py <input.json>")
        sys.exit(1)
    in_path = Path(sys.argv[1])
    filing_ids = json.load(open(in_path, encoding="utf-8"))
    session = house_clerk.build_session()

    # Pull each filing's existing politician_name/bioguide_id/disclosure_date
    # from the DB (set by whichever parser last processed it) -- none of
    # these are recoverable from the PDF itself, and upsert_trades'
    # unconditional overwrite of disclosure_date/politician_name/bioguide_id
    # would otherwise blank/scramble them on this targeted re-parse.
    filing_meta = {}
    with db.get_conn() as conn:
        for filing_id in filing_ids:
            row = conn.execute(
                "SELECT politician_name, bioguide_id, disclosure_date FROM trades "
                "WHERE external_id LIKE ? LIMIT 1",
                (f"{filing_id}#%",),
            ).fetchone()
            filing_meta[filing_id] = dict(row) if row else None

    normalized_trades = []
    for i, filing_id in enumerate(filing_ids, 1):
        meta = filing_meta.get(filing_id)
        if not meta:
            print(f"[{i}/{len(filing_ids)}] {filing_id}: SKIP (no existing trade row to attribute from)")
            continue

        year, doc_id = filing_id.split(":", 1)
        pdf_url = house_clerk.PTR_PDF_URL_TEMPLATE.format(year=year, doc_id=doc_id)
        print(f"[{i}/{len(filing_ids)}] {filing_id} ({meta['politician_name']})...", flush=True)
        try:
            raw_bytes, _ = house_clerk.fetch_with_cache(
                pdf_url, session=session, cache_key=f"house_clerk_ptr_{filing_id}"
            )
        except Exception as e:
            print(f"    download failed: {e!r}")
            continue

        new_hash = dedup.content_hash(raw_bytes)
        try:
            raw_transactions, used_ocr = house_clerk.parse_ptr_pdf(raw_bytes, doc_id)
            filing = house_clerk.RawFiling(
                source=house_clerk.SOURCE_ID,
                filing_id=filing_id,
                filer_name=meta["politician_name"],
                chamber="house",
                source_url=pdf_url,
                disclosure_date=meta["disclosure_date"] or "",
                ocr_sourced=used_ocr,
            )
            for line_index, raw_tx in enumerate(raw_transactions):
                t = house_clerk.normalize_trade(filing, raw_tx, line_index)
                t["bioguide_id"] = meta["bioguide_id"]
                normalized_trades.append(t)
            dedup.record_result(
                house_clerk.SOURCE_ID, filing_id, new_hash, house_clerk.PARSER_VERSION,
                "ok", len(raw_transactions), None, datetime.now(timezone.utc).isoformat(),
            )
            print(f"    -> {len(raw_transactions)} rows (used_ocr={used_ocr})")
        except house_clerk.UnrecognizedFormatError as e:
            print(f"    UNRECOGNIZED: {e}")
            dedup.record_result(
                house_clerk.SOURCE_ID, filing_id, new_hash, house_clerk.PARSER_VERSION,
                "unrecognized_format", 0, str(e), datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            print(f"    ERROR: {e!r}")
            dedup.record_result(
                house_clerk.SOURCE_ID, filing_id, new_hash, house_clerk.PARSER_VERSION,
                "failed", 0, str(e), datetime.now(timezone.utc).isoformat(),
            )

    loaded = upsert_trades(normalized_trades)
    purged = purge_stale_filing_rows(house_clerk.SOURCE_ID)
    print(f"\nupserted {loaded} trades from {len(filing_ids)} re-parsed filings, purged {purged} stale rows")


if __name__ == "__main__":
    main()
