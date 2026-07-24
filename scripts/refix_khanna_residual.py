"""
One-off follow-up to the Khanna asset-column cleanup: 176 rows (across 25
filings) were categorized as "readable" by the pre-fix is_garbled() and had
clean_company_name_text() applied, but the cleaned result still has leftover
junk (structural chars like |/_/[]{}, an OCR replacement char, or fewer than
3 real letters) -- signs the row was actually garbled and should have gotten
the "Unreadable" label instead.

Re-parses each affected filing fresh (deterministic given unchanged parser
code) to recover each row's TRUE raw asset_description via its external_id's
line_index, tracking (pdf_page_number, row_number_on_page) the same way the
earlier page/row recovery passes did: 1-indexed PDF page, 1-indexed position
within that page's own extracted-rows list (checkbox_form._parse_page's
output order) -- since checkbox-form rows don't carry an owner code, this
positional count is the practical equivalent of the SP/DC/JT row count used
for natively-parsed filings.

Writes trade_id -> {raw, garbled, page, row} to scratch_residual_results.json
for review before the caller applies it to the DB (this script only reads).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import asset_quality as aq
from backend.pipeline import house_clerk, checkbox_form, ocr as ocr_mod

IN_PATH = ROOT / "scratch_affected_remaining.json"
OUT_PATH = ROOT / "scratch_residual_results.json"


def parse_with_positions(raw_bytes, doc_id):
    """Like house_clerk.parse_ptr_pdf, but also returns a parallel list of
    (page_number, row_number_on_page) tuples (or None where not applicable --
    i.e. native text-table filings, which this whole cleanup never touches
    since it only affects ocr_sourced rows)."""
    rows, found_header = house_clerk._extract_table_rows(raw_bytes)
    used_ocr = False
    ocr_pages = None
    if not found_header and ocr_mod.is_available():
        used_ocr = True
        ocr_pages = ocr_mod.ocr_pdf_pages(raw_bytes)
        rows, found_header = house_clerk._build_rows_from_pages_words(
            [p["words"] for p in ocr_pages]
        )

    transactions = []
    positions = []
    if found_header:
        raw_transactions = house_clerk._parse_transaction_rows(rows)
        transactions = [house_clerk._finalize_transaction(t) for t in raw_transactions]
        positions = [None] * len(transactions)

    if not transactions and ocr_pages:
        all_rows = []
        row_positions = []
        for page_idx, page in enumerate(ocr_pages):
            try:
                page_rows = checkbox_form._parse_page(page)
            except Exception:
                page_rows = []
            for within_idx, r in enumerate(page_rows):
                all_rows.append(r)
                row_positions.append((page_idx + 1, within_idx + 1))
        if all_rows:
            for r in all_rows:
                r.pop("_top", None)
            transactions = [house_clerk._finalize_transaction(t) for t in all_rows]
            positions = row_positions

    return transactions, positions


def main():
    remaining = json.load(open(IN_PATH, encoding="utf-8"))
    from collections import defaultdict

    by_filing = defaultdict(list)
    for r in remaining:
        fid = r["external_id"].split("#")[0]
        by_filing[fid].append(r)

    session = house_clerk.build_session()
    results = {}
    filings = sorted(by_filing.items())
    for i, (filing_id, rows) in enumerate(filings, 1):
        year, doc_id = filing_id.split(":", 1)
        pdf_url = house_clerk.PTR_PDF_URL_TEMPLATE.format(year=year, doc_id=doc_id)
        print(f"[{i}/{len(filings)}] {filing_id} ({len(rows)} rows)...", flush=True)
        try:
            raw_bytes, _ = house_clerk.fetch_with_cache(
                pdf_url, session=session, cache_key=f"house_clerk_ptr_{filing_id}"
            )
            transactions, positions = parse_with_positions(raw_bytes, doc_id)
        except Exception as e:
            print(f"    ERROR: {e!r}")
            for r in rows:
                results[str(r["id"])] = {"error": str(e)}
            continue

        found = 0
        for r in rows:
            line_index = int(r["external_id"].split("#")[1])
            if line_index >= len(transactions):
                results[str(r["id"])] = {"error": "line_index out of range"}
                continue
            raw_desc = transactions[line_index]["asset_description"]
            pos = positions[line_index] if line_index < len(positions) else None
            garbled = aq.is_garbled(raw_desc)
            results[str(r["id"])] = {
                "raw": raw_desc,
                "garbled": garbled,
                "page": pos[0] if pos else None,
                "row": pos[1] if pos else None,
            }
            found += 1
        print(f"    -> {len(transactions)} rows re-parsed, {found}/{len(rows)} matched", flush=True)

    json.dump(results, open(OUT_PATH, "w", encoding="utf-8"), indent=2)
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()
