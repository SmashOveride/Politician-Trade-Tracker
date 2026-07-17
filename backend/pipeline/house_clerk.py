"""
Primary source: the House Clerk's bulk Financial Disclosure ZIP downloads
(https://disclosures-clerk.house.gov/FinancialDisclosure).

Each calendar year has one ZIP at a stable URL:

    https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip

containing:
  - {year}FD.txt  -- a tab-separated index of every filing that year (one
    row per filer/filing, with columns Prefix/Last/First/Suffix/FilingType/
    StateDst/Year/FilingDate/DocID). FilingType 'P' identifies a Periodic
    Transaction Report (the only filing type that contains stock trades).
  - {year}FD.xml  -- the same index in XML form (not used here; the .txt
    index is simpler and sufficient).

Each individual PTR is a separate PDF, fetched by DocID:

    https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf

This module downloads the year index (cached/conditional -- see
http_client.fetch_with_cache), figures out which PTR PDFs are new or changed
since the last run (see dedup.py), downloads + parses only those, and
returns normalized trade dicts (see pipeline/schema.py).

Format-version detection: the {year}FD.txt header row is checked against
EXPECTED_INDEX_COLUMNS on every run. If the Clerk ever changes this header
(reorders/renames/adds columns), that's treated as an unrecognized format
and reported via monitoring.log_unrecognized_format rather than silently
mis-parsing every row. Likewise, if a PTR PDF's transaction table no longer
contains a recognizable header row, that's also treated as an unrecognized
format.
"""

import io
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pdfplumber

from . import dedup, monitoring
from .errors import check_cancelled
from .http_client import build_session, fetch_with_cache
from .schema import RawFiling, normalize_trade

SOURCE_ID = "house_clerk"

INDEX_ZIP_URL_TEMPLATE = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_PDF_URL_TEMPLATE = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

# The {year}FD.txt index is tab-separated with this exact header. If the
# Clerk's office ever changes it, PARSER_VERSION below should be bumped once
# the parser is updated to match -- until then, a header mismatch is
# reported as an unrecognized format rather than guessed at.
EXPECTED_INDEX_COLUMNS = [
    "Prefix", "Last", "First", "Suffix", "FilingType", "StateDst", "Year", "FilingDate", "DocID",
]

# Only this FilingType contains stock trade transactions (Periodic
# Transaction Report). Other types (Annual/'O', Candidate/'C', Extension
# request/'X', Withdrawal/'W', etc.) don't list trades.
PTR_FILING_TYPE = "P"

# v3: fixed _TICKER_PAREN_RE to also match preferred-stock/depositary-share
# tickers that use a '$' series separator (e.g. "(WFC$D)", "(T$A)"),
# which previously came through with an empty ticker and a leftover
# "(WFC$D)"-style suffix stuck on the asset description. Bumping this
# forces already-successfully-parsed filings to be re-parsed once (via
# dedup.should_process's parser_version check) so the fix actually reaches
# previously-loaded trades, not just newly-fetched ones.
PARSER_VERSION = "house_clerk_pdf_v3"


class UnrecognizedFormatError(Exception):
    pass


# ---------------------------------------------------------------------------
# PTR PDF transaction table parsing
#
# The Clerk's PTR PDFs are NOT reliably regex-matchable as flattened text:
# pdfplumber's text extraction interleaves a transaction's Asset/Ticker/
# Type/Date/Amount cells with its (variable-length, wrapped) Owner/Source/
# Description metadata block in an order that shifts depending on how much
# each cell wraps in a given filing. Instead, this parses each page's words
# by their x/y position to reconstruct the actual table rows -- grouping
# words into visual lines by y-position, then into columns by x-position
# (calibrated from the header row's own word positions on that page) -- so
# it's robust to the line-wrapping variance seen across real filings.
# ---------------------------------------------------------------------------

# Header cell text -> canonical column name, used both to detect the header
# row and to calibrate that page's column x-boundaries from it.
_HEADER_CELL_NAMES = {
    "id": "id",
    "owner": "owner",
    "asset": "asset",
    "transaction": "type",
    "type": "type",
    "date": "txdate",  # first "Date" after Type is the transaction date
    "notification": "notifdate",
    "amount": "amount",
    "cap.": "capgains",
}

_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_AMOUNT_TOKEN_RE = re.compile(r"^\$[\d,]+$")
_TXTYPE_RE = re.compile(r"^(P|S|E)$")
# Matches the ticker in a trailing "(TICKER)" on the asset description.
# Besides plain tickers (AAPL) and class-share tickers (BRK.B), this also
# needs to match preferred-stock/depositary-share tickers, which the House
# Clerk's PTR PDFs commonly render with a '$' separator before the series
# letter (e.g. "(WFC$D)", "(T$A)", "(COF$I)") -- these were previously left
# unmatched, so the ticker came back empty AND the "(WFC$D)"-style suffix
# was never stripped out of the asset description (see
# _finalize_transaction below, which uses this same regex for both).
_TICKER_PAREN_RE = re.compile(r"\(([A-Z]{1,6}(?:[./$][A-Z]+)?)\)")
_ASSET_TYPE_BRACKET_RE = re.compile(r"\[[A-Z\-]{1,6}\]")
_METADATA_LINE_RE = re.compile(r"^(F|S|D|L)\s*[:\x00]")  # "F S: New", "S O: ...", "D: ...", "L: US"


def _group_words_into_lines(words):
    """Groups a page's words (as returned by pdfplumber's extract_words())
    into visual lines by rounded y-position (top), each line's words sorted
    left-to-right."""
    lines = defaultdict(list)
    for w in words:
        lines[round(w["top"])].append(w)
    return [sorted(lines[top], key=lambda w: w["x0"]) for top in sorted(lines)]


def _find_header_columns(line_words):
    """Given one visual line's words, returns a dict of
    {canonical_column_name: x0_start} if this line looks like the table's
    header row (contains at least 'Owner', 'Asset', and 'Amount'), else
    None. Column boundaries are later derived by sorting these start
    positions and treating each as the left edge of that column's span."""
    found = {}
    for w in line_words:
        key = w["text"].strip(".").lower()
        col = _HEADER_CELL_NAMES.get(key)
        if col and col not in found:
            found[col] = w["x0"]
    if {"owner", "asset", "amount"} <= set(found.keys()):
        return found
    return None


def _column_for_x(x0, boundaries):
    """boundaries: list of (col_name, start_x) sorted by start_x ascending.
    Returns the column whose start_x is the largest one <= x0."""
    col = boundaries[0][0]
    for name, start_x in boundaries:
        if x0 >= start_x - 5:  # small tolerance for slight misalignment
            col = name
        else:
            break
    return col


def _extract_table_rows(raw_bytes):
    """Parses every page of the PDF into a list of dicts (one per visual
    line across the whole document), each mapping column name -> joined
    text for that line, using column boundaries calibrated from whichever
    page's header row is found. Returns (rows, found_header) -- rows only
    includes lines after the header line(s); found_header is False if no
    page contained a recognizable header row at all."""
    all_rows = []
    boundaries = None
    found_header = False

    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            if not words:
                continue
            lines = _group_words_into_lines(words)
            for line_words in lines:
                header_cols = _find_header_columns(line_words)
                if header_cols:
                    boundaries = sorted(header_cols.items(), key=lambda kv: kv[1])
                    found_header = True
                    continue  # header row itself isn't a data row
                if boundaries is None:
                    continue  # haven't seen a header yet on any page -- skip preamble
                row = defaultdict(list)
                for w in line_words:
                    col = _column_for_x(w["x0"], boundaries)
                    row[col].append(w["text"])
                all_rows.append({k: " ".join(v) for k, v in row.items()})

    return all_rows, found_header


def _parse_transaction_rows(rows):
    """Reassembles the per-line column dicts from _extract_table_rows into
    one dict per transaction, folding in wrapped continuation lines
    (wrapped asset names/tickers, and multi-line amount ranges), and
    stopping at the page footnote ('* For the complete list...')."""
    transactions: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for row in rows:
        joined_all = " ".join(row.values())
        if joined_all.strip().startswith("*") or "asset type abbreviations" in joined_all:
            break

        txtype_raw = row.get("type", "").strip()
        txdate = row.get("txdate", "").strip()
        notifdate = row.get("notifdate", "").strip()
        amount = row.get("amount", "").strip()

        is_new_tx = bool(_DATE_RE.match(txdate)) and bool(_DATE_RE.match(notifdate)) and bool(txtype_raw)

        if is_new_tx:
            if current:
                transactions.append(current)
            current = {
                "owner": row.get("owner", "").strip() or "SP",
                "asset_lines": [row.get("asset", "").strip()],
                "txtype": txtype_raw,
                "txdate": txdate,
                "amount": amount,
                "in_metadata": False,
            }
            continue

        if current is None:
            continue  # stray line before the first transaction (shouldn't normally happen)

        # Continuation of a wrapped amount range, e.g. "$50,001 -" / "$100,000"
        amount_frag = row.get("amount", "").strip()
        if amount_frag and _AMOUNT_TOKEN_RE.match(amount_frag.replace(" ", "")):
            current["amount"] = (current["amount"] + " " + amount_frag).strip()

        # Once the F/S/O/D/L metadata block starts (e.g. "F S: New", "S O:
        # ...", "D: ..."), every subsequent line belongs to that block --
        # including its own wrapped continuation lines, which don't
        # themselves start with an F/S/D/L marker. So track that we've
        # entered the metadata block for this transaction and stop folding
        # any further asset-column text in once we have.
        asset_frag = row.get("asset", "").strip()
        if asset_frag:
            if _METADATA_LINE_RE.match(asset_frag):
                current["in_metadata"] = True
            elif not current.get("in_metadata"):
                current["asset_lines"].append(asset_frag)

    if current:
        transactions.append(current)
    return transactions


def _finalize_transaction(t):
    full_asset = " ".join(t["asset_lines"])
    ticker_m = _TICKER_PAREN_RE.search(full_asset)
    ticker = ticker_m.group(1) if ticker_m else ""
    asset_desc = _TICKER_PAREN_RE.sub("", full_asset)
    asset_desc = _ASSET_TYPE_BRACKET_RE.sub("", asset_desc)
    asset_desc = re.sub(r"\s+", " ", asset_desc).strip(" -")
    return {
        "owner": t["owner"],
        "asset_description": asset_desc,
        "ticker": ticker,
        "raw_type": t["txtype"],
        "transaction_date": t["txdate"],
        "amount": t["amount"].strip(),
    }


def parse_ptr_pdf(raw_bytes, doc_id):
    """Parses one PTR PDF's raw bytes into a list of raw transaction dicts.
    Returns an empty list (not an error) for a filing with zero disclosed
    transactions -- e.g. some PTRs cover only exempt assets. Raises
    UnrecognizedFormatError if no transaction table header could be found
    at all, which usually means the Clerk's PDF layout has changed in a way
    this parser no longer understands."""
    rows, found_header = _extract_table_rows(raw_bytes)
    if not found_header:
        raise UnrecognizedFormatError(
            f"PTR PDF for DocID {doc_id}: no recognizable transaction table header found"
        )
    raw_transactions = _parse_transaction_rows(rows)
    return [_finalize_transaction(t) for t in raw_transactions]


def _parse_index(raw_bytes):
    """Parses the {year}FD.txt tab-separated index. Raises
    UnrecognizedFormatError if the header row doesn't match
    EXPECTED_INDEX_COLUMNS, rather than silently misreading columns."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile as e:
        raise UnrecognizedFormatError(f"Downloaded content is not a valid ZIP file: {e}")
    txt_names = [n for n in z.namelist() if n.lower().endswith(".txt")]
    if not txt_names:
        raise UnrecognizedFormatError(f"ZIP contains no .txt index file (entries: {z.namelist()})")

    with z.open(txt_names[0]) as f:
        content = f.read().decode("latin-1")

    lines = content.split("\r\n") if "\r\n" in content else content.split("\n")
    lines = [l for l in lines if l.strip()]
    if not lines:
        raise UnrecognizedFormatError("Index file is empty")

    header = lines[0].split("\t")
    if header != EXPECTED_INDEX_COLUMNS:
        raise UnrecognizedFormatError(
            f"Index header changed: expected {EXPECTED_INDEX_COLUMNS}, got {header}"
        )

    rows = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != len(header):
            continue  # malformed row -- skip rather than crash the whole index
        rows.append(dict(zip(header, parts)))
    return rows


def fetch_filings(year, session=None):
    """Downloads and parses the {year}FD.zip index (cached/conditional).
    Returns the list of PTR ('P') filing index rows for that year. Raises
    UnrecognizedFormatError if the index format doesn't match what this
    parser expects."""
    session = session or build_session()
    url = INDEX_ZIP_URL_TEMPLATE.format(year=year)
    raw_bytes, _changed = fetch_with_cache(url, session=session, cache_key=f"house_clerk_index_{year}")
    all_rows = _parse_index(raw_bytes)
    return [r for r in all_rows if r.get("FilingType") == PTR_FILING_TYPE]


def collect_trades(years, session=None, progress_cb=None, cancel_check=None, tracker=None):
    """Top-level entry point: fetches the index for each year in `years`,
    downloads+parses only new/changed PTR PDFs (per dedup.py), and returns
    (normalized_trades, stats). Individual filing parse failures are logged
    and skipped rather than aborting the whole run; an unrecognized index
    format aborts (and is logged) since it likely means every filing that
    year would be mis-parsed.

    `cancel_check`, if given, is a zero-arg callable checked between
    individual filing downloads -- see pipeline/errors.py -- so a
    user-requested "Stop Refresh" takes effect promptly instead of waiting
    for every filing in every requested year to finish downloading first.

    `tracker`, if given (see pipeline/progress.py), has its total bumped by
    the number of filings found in each year's index, and its completed
    count bumped once per filing (parsed, skipped-as-cached, or failed) --
    used to drive the refresh progress bar/ETA in the UI.
    """
    session = session or build_session()
    normalized_trades = []
    stats = {"filings_seen": 0, "filings_parsed": 0, "filings_skipped_cached": 0, "filings_failed": 0}

    monitoring.mark_attempt(SOURCE_ID)

    for year in years:
        try:
            filings = fetch_filings(year, session=session)
        except UnrecognizedFormatError as e:
            monitoring.log_unrecognized_format(SOURCE_ID, f"year {year}: {e}")
            monitoring.mark_failure(SOURCE_ID, e)
            continue
        except Exception as e:
            monitoring.mark_failure(SOURCE_ID, e)
            if progress_cb:
                progress_cb(f"House Clerk: failed to fetch {year} index ({e})")
            continue

        stats["filings_seen"] += len(filings)
        if tracker:
            tracker.add_total(len(filings))
        if progress_cb:
            progress_cb(f"House Clerk {year}: {len(filings)} PTR filings in index")

        for row in filings:
            check_cancelled(cancel_check)
            doc_id = row.get("DocID", "").strip()
            if not doc_id:
                if tracker:
                    tracker.add_completed()
                continue
            filing_id = f"{year}:{doc_id}"
            pdf_url = PTR_PDF_URL_TEMPLATE.format(year=year, doc_id=doc_id)

            try:
                raw_bytes, _changed = fetch_with_cache(
                    pdf_url, session=session, cache_key=f"house_clerk_ptr_{filing_id}"
                )
            except Exception as e:
                stats["filings_failed"] += 1
                monitoring.log_parse_failure(SOURCE_ID, filing_id, f"download failed: {e}")
                dedup.record_result(
                    SOURCE_ID, filing_id, None, PARSER_VERSION, "failed", 0, str(e),
                    datetime.now(timezone.utc).isoformat(),
                )
                if tracker:
                    tracker.add_completed()
                continue

            new_hash = dedup.content_hash(raw_bytes)
            if not dedup.should_process(SOURCE_ID, filing_id, new_hash, parser_version=PARSER_VERSION):
                stats["filings_skipped_cached"] += 1
                if tracker:
                    tracker.add_completed()
                continue

            filer_name = " ".join(
                p for p in (row.get("First", ""), row.get("Last", "")) if p
            ).strip()

            try:
                raw_transactions = parse_ptr_pdf(raw_bytes, doc_id)
                filing = RawFiling(
                    source=SOURCE_ID,
                    filing_id=filing_id,
                    filer_name=filer_name,
                    chamber="house",
                    source_url=pdf_url,
                    disclosure_date=row.get("FilingDate", ""),
                )
                for line_index, raw_tx in enumerate(raw_transactions):
                    normalized_trades.append(normalize_trade(filing, raw_tx, line_index))

                stats["filings_parsed"] += 1
                dedup.record_result(
                    SOURCE_ID, filing_id, new_hash, PARSER_VERSION, "ok",
                    len(raw_transactions), None, datetime.now(timezone.utc).isoformat(),
                )
            except UnrecognizedFormatError as e:
                stats["filings_failed"] += 1
                monitoring.log_unrecognized_format(SOURCE_ID, f"filing {filing_id}: {e}")
                dedup.record_result(
                    SOURCE_ID, filing_id, new_hash, PARSER_VERSION, "unrecognized_format", 0, str(e),
                    datetime.now(timezone.utc).isoformat(),
                )
            except Exception as e:
                stats["filings_failed"] += 1
                monitoring.log_parse_failure(SOURCE_ID, filing_id, e, exc_info=True)
                dedup.record_result(
                    SOURCE_ID, filing_id, new_hash, PARSER_VERSION, "failed", 0, str(e),
                    datetime.now(timezone.utc).isoformat(),
                )
            finally:
                if tracker:
                    tracker.add_completed()

    if stats["filings_parsed"] > 0 or stats["filings_skipped_cached"] > 0:
        monitoring.mark_success(SOURCE_ID)
    elif stats["filings_seen"] == 0:
        # No years produced a usable index at all -- treat as a failure so
        # staleness/alerting notices it, rather than silently reporting
        # "0 filings" as a quiet success.
        monitoring.mark_failure(SOURCE_ID, "no usable filing index for any requested year")

    return normalized_trades, stats
