"""
Companion to reprocess_stale_filings.py for the rare case where a stale
filing's trade rows were already purged entirely (e.g. an earlier
purge_stale_filing_rows run removed them) -- there's no existing trade row
left to read politician_name/bioguide_id/disclosure_date from, so this
looks them up from the House Clerk year index instead (the same source
collect_trades itself uses), same as a real first-time parse would.

Usage: python scripts/reprocess_orphan_filings.py <input.json>
  <input.json>: JSON list of "{year}:{doc_id}" filing_id strings
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.normalize import clean_name_tokens, resolve_bioguide
from backend.pipeline import dedup, house_clerk
from backend.pipeline.loader import upsert_trades, purge_stale_filing_rows
from backend import db


def _build_bioguide_lookup_from_db():
    """Same {last_name_token: [(first_tokens_set, bioguide_id), ...]} shape
    resolve_bioguide() expects, built directly from the already-populated
    politicians table -- avoids re-invoking data_fetch's legislator-loading
    machinery (which re-fetches/upserts from the source YAML/API) just to
    get a name index that already exists in the DB."""
    lookup = {}
    with db.get_conn() as conn:
        for row in conn.execute("SELECT bioguide_id, full_name FROM politicians"):
            tokens = clean_name_tokens(row["full_name"])
            if not tokens:
                continue
            last, first_tokens = tokens[-1], set(tokens[:-1])
            lookup.setdefault(last, []).append((first_tokens, row["bioguide_id"]))
    return lookup


def main():
    if len(sys.argv) < 2:
        print("usage: python scripts/reprocess_orphan_filings.py <input.json>")
        sys.exit(1)
    filing_ids = json.load(open(Path(sys.argv[1]), encoding="utf-8"))
    session = house_clerk.build_session()

    bioguide_lookup_by_name = _build_bioguide_lookup_from_db()

    years_needed = sorted({fid.split(":")[0] for fid in filing_ids})
    index_by_year = {}
    for year in years_needed:
        try:
            index_by_year[year] = {
                row["DocID"]: row for row in house_clerk.fetch_filings(year, session=session)
            }
        except Exception as e:
            print(f"failed to fetch {year} index: {e!r}")
            index_by_year[year] = {}

    normalized_trades = []
    for i, filing_id in enumerate(filing_ids, 1):
        year, doc_id = filing_id.split(":", 1)
        index_row = index_by_year.get(year, {}).get(doc_id)
        print(f"[{i}/{len(filing_ids)}] {filing_id}...", flush=True)
        if not index_row:
            print(f"    not found in {year} index -- skipping")
            continue
        filer_name = " ".join(p for p in (index_row.get("First", ""), index_row.get("Last", "")) if p).strip()
        disclosure_date = index_row.get("FilingDate", "")
        pdf_url = house_clerk.PTR_PDF_URL_TEMPLATE.format(year=year, doc_id=doc_id)

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
                filer_name=filer_name,
                chamber="house",
                source_url=pdf_url,
                disclosure_date=disclosure_date,
                ocr_sourced=used_ocr,
            )
            for line_index, raw_tx in enumerate(raw_transactions):
                t = house_clerk.normalize_trade(filing, raw_tx, line_index)
                t["bioguide_id"] = resolve_bioguide(filer_name, bioguide_lookup_by_name)
                normalized_trades.append(t)
            dedup.record_result(
                house_clerk.SOURCE_ID, filing_id, new_hash, house_clerk.PARSER_VERSION,
                "ok", len(raw_transactions), None, datetime.now(timezone.utc).isoformat(),
            )
            print(f"    -> {len(raw_transactions)} rows for {filer_name} (used_ocr={used_ocr})")
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
