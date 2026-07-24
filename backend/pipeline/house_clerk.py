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

from .. import asset_quality
from . import checkbox_form, dedup, monitoring, ocr
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
# "(WFC$D)"-style suffix stuck on the asset description.
# v4: made header/table reconstruction robust to OCR noise -- tolerance-based
# line grouping instead of exact rounding (_LINE_GROUPING_TOLERANCE), header
# cells accumulated across wrapped lines instead of requiring them all on
# one line, and the Notification Date anchor is no longer required when
# OCR never recognized that column at all. Previously this meant a scanned
# filing could OCR successfully and still recover zero transactions.
# v5: fixed two more OCR issues found on filings scanned sideways (whole
# page rotated 90 degrees, confirmed on real filings) -- ocr.py now detects
# and corrects page rotation before OCR, and switched Tesseract's page
# segmentation mode to --psm 6 (uniform text block), since the default mode
# badly under-reads dense grid-lined PTR tables (confirmed: 34 words found
# vs 260 with --psm 6, on the same page). Also dropped "owner" as a
# required header anchor -- some forms label that column "SP/DC/JT"
# instead and never contain the word "owner" at all, making it impossible
# to require. See ocr.py and _HEADER_ANCHOR_COLS for details.
# v6: _DATE_RE now accepts non-zero-padded month/day ("2/24/2024", not just
# "02/24/2024") -- some real filings (and some OCR passes) render dates
# that way, and the strict 2-digit requirement silently rejected otherwise
# perfectly good transaction rows.
# v7: added the paper checkbox-form fallback tier (see checkbox_form.py) --
# scanned paper PTR filings (e.g. every one of Rep. Khanna's, previously
# recovering zero transactions) are now parsed from the page images
# directly: grid columns detected from pixels, checkbox type/amount marks
# read as ink density, rows anchored on the OCR'd date columns.
# v8: ocr.py now jointly searches orientation (0/180) AND page segmentation
# mode (PSM 6/3) whenever the primary pass reads poorly, instead of
# deciding those one stage at a time -- confirmed on a real sparse page
# (4 filled rows atop an empty printed grid) where the staged logic locked
# in a wrong 180-flip and PSM 6 misread everything, while the correct
# orientation under PSM 3 read the actual company names. Also adds a
# sparse-page PSM 3 retry for pages whose PSM 6 pass finds very few words.
# v9: every trade row now records whether it came from OCR/image parsing
# vs. a filing's native embedded text (RawFiling.ocr_sourced, threaded
# through to the trades table's new ocr_sourced column) -- lets the API
# safely apply the "Unreadable -- See Records" display label (see
# asset_quality.py) only to rows where OCR gibberish is actually possible,
# never to natively parsed text. Also: a filing that fails ALL parsing
# tiers now gets a single placeholder trade row (see
# _unreadable_placeholder) instead of vanishing from the app with no
# trace -- every filing on record is now represented somewhere, readable
# or not, with its Records button still linking to the real PDF.
# Bumping this forces already-processed filings (successes AND failures) to
# be re-parsed once (via dedup.should_process's parser_version check) so
# each fix actually reaches previously-loaded/skipped trades, not just
# newly-fetched filings.
PARSER_VERSION = "house_clerk_pdf_v9"


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

# Month/day allow 1 or 2 digits, not just 2 -- some real filings (and some
# OCR passes that drop a leading zero) render dates like "2/24/2024" rather
# than "02/24/2024". Strictly a superset of the old 2-digit-only pattern,
# so this can't reject anything that used to match.
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
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



# How close two words' y-positions ("top") need to be to count as the same
# visual line. Cleanly machine-generated PDF text has near-identical top
# values for words on the same line (a fraction of a point), so this barely
# matters for that path -- but OCR'd word boxes (see ocr.py) are noisier:
# small per-character bounding-box jitter routinely puts words that are
# visually on the same line a few points apart, which an exact-round()
# grouping was splitting into many spurious one/two-word "lines" and
# scrambling reading order entirely on some real scanned filings. Kept well
# below a table row's actual line height (~12-14pt in these forms) so
# genuinely separate rows never get merged.
_LINE_GROUPING_TOLERANCE = 4


def _group_words_into_lines(words):
    """Groups a page's words (as returned by pdfplumber's extract_words(),
    or ocr.py's OCR'd equivalent) into visual lines by y-position (top,
    within _LINE_GROUPING_TOLERANCE of each other), each line's words sorted
    left-to-right."""
    ordered = sorted(words, key=lambda w: w["top"])
    lines = []
    current = []
    current_top = None
    for w in ordered:
        if current and abs(w["top"] - current_top) > _LINE_GROUPING_TOLERANCE:
            lines.append(current)
            current = []
        current.append(w)
        # Running average keeps a long line's start from drifting the
        # threshold too far by the time later (rightmost) words are seen.
        current_top = sum(cw["top"] for cw in current) / len(current)
    if current:
        lines.append(current)
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _find_header_columns(line_words):
    """Given one visual line's words, returns {canonical_column_name:
    x0_start} for whichever recognized header-cell tokens appear on this
    line (however few -- no minimum count required here). Column boundaries
    are later derived by sorting these start positions and treating each as
    the left edge of that column's span.

    The caller (_build_rows_from_pages_words) accumulates these across
    consecutive lines rather than requiring a single line to contain every
    expected header cell -- real PTR forms' column headers ("Transaction
    Type", "Notification Date", ...) routinely wrap across 2+ visual lines,
    and OCR'd scans especially so."""
    found = {}
    for w in line_words:
        key = w["text"].strip(".").lower()
        col = _HEADER_CELL_NAMES.get(key)
        if col and col not in found:
            found[col] = w["x0"]
    return found


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


_HEADER_ANCHOR_COLS = {"asset", "amount"}

# The "owner" column requires a lower bar to recognize on top of the anchor
# check above: some real PTR forms label it "Owner" (matches
# _HEADER_CELL_NAMES directly), but others -- confirmed on a filing that
# otherwise parsed fine once this stopped requiring "owner" -- label it
# "SP/DC/JT" (the code values that column actually holds: SPouse/Dependent
# Child/Joint tenant) and never contain the literal word "owner" anywhere,
# making it structurally impossible to require as an anchor. "asset" and
# "amount" appear on every real PTR template seen so far and are
# distinctive enough (unlike e.g. "date") not to false-positive on
# ordinary data-row text, so they're the anchors; "owner" is still
# recognized and captured into boundaries when its cell does say "Owner",
# just no longer required for a header to count as found.

# A real header block is at most a handful of physically wrapped lines
# ("Notification" / "Date" stacked under "Transaction" / "Type", etc.) --
# capping accumulation this tightly means a stray later line that happens to
# repeat a header word (rare, but not impossible in noisy OCR output) can't
# get misread as more header content deep in the table.
_MAX_HEADER_ACCUMULATION_LINES = 6


def _build_rows_from_pages_words(pages_words):
    """Given a list of per-page word lists (each word a dict with at least
    'text', 'x0', 'top' -- either pdfplumber's native extract_words()
    output, or OCR'd words shaped the same way, see ocr.py), reconstructs
    the same per-line column dicts _extract_table_rows produces. Returns
    (rows, found_header) -- rows only includes lines after the header
    line(s); found_header is False if no page contained a recognizable
    header row at all.

    Header cells are accumulated across consecutive lines (see
    _find_header_columns) rather than requiring every expected column on a
    single line, since the real form's column headers routinely wrap across
    2+ visual lines -- especially so once OCR'd (see
    _LINE_GROUPING_TOLERANCE)."""
    all_rows = []
    boundaries = None
    found_header = False

    for words in pages_words:
        if not words:
            continue
        lines = _group_words_into_lines(words)
        header_accum = {}
        header_accum_lines = 0
        for line_words in lines:
            new_cols = {
                col: x0 for col, x0 in _find_header_columns(line_words).items() if col not in header_accum
            }
            if new_cols and header_accum_lines < _MAX_HEADER_ACCUMULATION_LINES:
                header_accum.update(new_cols)
                header_accum_lines += 1
                if _HEADER_ANCHOR_COLS <= header_accum.keys():
                    boundaries = sorted(header_accum.items(), key=lambda kv: kv[1])
                    found_header = True
                continue  # this line contributed header cells -- never a data row
            if boundaries is None:
                continue  # haven't completed a header yet on this page -- skip preamble
            row = defaultdict(list)
            for w in line_words:
                col = _column_for_x(w["x0"], boundaries)
                row[col].append(w["text"])
            all_rows.append({k: " ".join(v) for k, v in row.items()})

    return all_rows, found_header


def _extract_table_rows(raw_bytes):
    """Parses every page of the PDF into a list of dicts (one per visual
    line across the whole document), each mapping column name -> joined
    text for that line, using column boundaries calibrated from whichever
    page's header row is found. Returns (rows, found_header) -- rows only
    includes lines after the header line(s); found_header is False if no
    page contained a recognizable header row at all."""
    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
        pages_words = [page.extract_words() for page in pdf.pages]
    return _build_rows_from_pages_words(pages_words)


def _parse_transaction_rows(rows):
    """Reassembles the per-line column dicts from _extract_table_rows into
    one dict per transaction, folding in wrapped continuation lines
    (wrapped asset names/tickers, and multi-line amount ranges), and
    stopping at the page footnote ('* For the complete list...')."""
    transactions: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    # Some scans/OCR runs never recognize the "Notification Date" header
    # cell at all (its column boundary never gets established -- see
    # _build_rows_from_pages_words), so no row in the whole document ever
    # gets a 'notifdate' value even for otherwise perfectly readable
    # transaction rows. Requiring it unconditionally would then silently
    # recover zero transactions from an entire filing over one missed
    # header cell. Only require it as a confirming anchor when it's
    # structurally possible to -- i.e. when at least one row in this
    # document actually has it -- and fall back to also requiring txtype to
    # be a real P/S/E code (rather than just non-empty) to compensate for
    # losing that anchor.
    notifdate_ever_seen = any(_DATE_RE.match(row.get("notifdate", "").strip()) for row in rows)

    for row in rows:
        joined_all = " ".join(row.values())
        if joined_all.strip().startswith("*") or "asset type abbreviations" in joined_all:
            break

        txtype_raw = row.get("type", "").strip()
        txdate = row.get("txdate", "").strip()
        notifdate = row.get("notifdate", "").strip()
        amount = row.get("amount", "").strip()

        if notifdate_ever_seen:
            is_new_tx = bool(_DATE_RE.match(txdate)) and bool(_DATE_RE.match(notifdate)) and bool(txtype_raw)
        else:
            is_new_tx = bool(_DATE_RE.match(txdate)) and bool(_TXTYPE_RE.match(txtype_raw))

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


# Parenthesized words in security names that are NOT tickers: "Coca-Cola
# Company (The)", "Wells Fargo & Co (New)", "(Del)" for Delaware
# incorporations. Without this, those were extracted as tickers 'THE'/'NEW'
# (confirmed on real paper-form filings) and then spread to other rows via
# ticker_resolve's name matching.
#
# IXNZF: a scanned paper PTR page carries some fixed background artifact
# (likely a watermark or stamp) that OCR misreads as "(IXNZF)" on many
# different pages -- confirmed on real data: this single bogus "ticker"
# ended up attached to 1381 otherwise-unrelated (and mostly already-garbled)
# rows across two different filers' scanned filings, with nothing in any of
# those rows' actual asset text resembling a real IXNZF security.
_NOT_A_TICKER = {"THE", "NEW", "OLD", "DEL", "IXNZF"}


def _finalize_transaction(t):
    full_asset = " ".join(t["asset_lines"])
    ticker_m = _TICKER_PAREN_RE.search(full_asset)
    ticker = ticker_m.group(1) if ticker_m else ""
    if ticker in _NOT_A_TICKER:
        ticker = ""
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
    Returns (transactions, used_ocr). transactions is [] (not an error) for
    a filing with zero disclosed transactions -- e.g. some PTRs cover only
    exempt assets. Raises UnrecognizedFormatError if no transaction table
    header could be found at all, which usually means the Clerk's PDF
    layout has changed in a way this parser no longer understands.

    Some PTR PDFs are filed as flat scanned images with no embedded text
    layer, so pdfplumber's normal text extraction finds nothing at all on
    any page (found_header is False, not just "header row looks
    different"). For that specific case only -- not for a genuinely changed
    layout, which should still be reported as an unrecognized format -- this
    retries via OCR (see ocr.py) before giving up.

    Scanned filings that use the *paper* checkbox-grid PTR form (see
    checkbox_form.py -- transaction type and amount are checkbox marks, not
    text, and the header OCRs to garbage by design of the scan quality) get
    one more fallback tier: if the OCR'd text yields no header or no
    transactions, the checkbox-form parser reads the grid and marks from
    the page images directly. OCR is best-effort and optional: if it's not
    installed, or nothing recovers a transaction, this still raises
    UnrecognizedFormatError exactly as before OCR support existed."""
    rows, found_header = _extract_table_rows(raw_bytes)
    used_ocr = False
    ocr_pages = None
    if not found_header and ocr.is_available():
        used_ocr = True
        ocr_pages = ocr.ocr_pdf_pages(raw_bytes)
        rows, found_header = _build_rows_from_pages_words([p["words"] for p in ocr_pages])

    transactions = []
    if found_header:
        raw_transactions = _parse_transaction_rows(rows)
        transactions = [_finalize_transaction(t) for t in raw_transactions]

    # Paper checkbox-form tier: only reachable via the OCR path (the paper
    # form never has a native text layer), and only consulted when the
    # text-table parse produced nothing -- a successfully parsed text table
    # is always preferred.
    if not transactions and ocr_pages:
        checkbox_rows, cb_pages = checkbox_form.parse_pages(ocr_pages)
        if checkbox_rows:
            transactions = [_finalize_transaction(t) for t in checkbox_rows]
            monitoring.log_info(
                SOURCE_ID,
                f"DocID {doc_id}: recovered {len(transactions)} transaction(s) via the "
                f"paper checkbox-form parser ({cb_pages} page(s))",
            )
            return transactions, used_ocr

    if not found_header:
        detail = f"PTR PDF for DocID {doc_id}: no recognizable transaction table header found"
        if used_ocr:
            detail += " (including after OCR fallback)"
        raise UnrecognizedFormatError(detail)

    if used_ocr:
        monitoring.log_info(
            SOURCE_ID, f"DocID {doc_id}: recovered {len(transactions)} transaction(s) via OCR fallback"
        )
    return transactions, used_ocr


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


def _unreadable_placeholder(filing_id, filer_name, pdf_url, disclosure_date):
    """Builds a single normalized trade dict standing in for a filing we
    fetched but could not extract any transactions from at all (every
    parsing tier -- native text, OCR, and the checkbox-form fallback --
    came up empty or raised). Without this, such a filing simply vanishes
    from the app with no trace, even though it's a real, on-record filing
    a user might expect to see reflected in a politician's trade list.

    Uses a fixed 'unreadable' line-index suffix (rather than a numeric
    one) so it can never collide with a real transaction's index, and so a
    later successful re-parse of the same filing (a parser improvement, or
    an upstream correction) naturally leaves this placeholder as a single
    stale-but-harmless row rather than colliding with anything -- though in
    practice a successful re-parse's own transactions coexist with it
    until the next refresh's stale-row purge (see loader.purge_stale_filing_rows)
    removes it, since a filing that now parses is no longer "unreadable"."""
    filing = RawFiling(
        source=SOURCE_ID,
        filing_id=filing_id,
        filer_name=filer_name,
        chamber="house",
        source_url=pdf_url,
        disclosure_date=disclosure_date,
        ocr_sourced=True,
    )
    placeholder_raw_tx = {
        "asset_description": asset_quality.UNREADABLE_ASSET_LABEL,
        "raw_type": "",
        "transaction_date": "",
        "amount": "",
        "ticker": "",
    }
    return normalize_trade(filing, placeholder_raw_tx, "unreadable")


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
    stats = {
        "filings_seen": 0, "filings_parsed": 0, "filings_skipped_cached": 0,
        "filings_failed": 0, "filings_recovered_via_ocr": 0,
    }

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
                raw_transactions, used_ocr = parse_ptr_pdf(raw_bytes, doc_id)
                filing = RawFiling(
                    source=SOURCE_ID,
                    filing_id=filing_id,
                    filer_name=filer_name,
                    chamber="house",
                    source_url=pdf_url,
                    disclosure_date=row.get("FilingDate", ""),
                    ocr_sourced=used_ocr,
                )
                for line_index, raw_tx in enumerate(raw_transactions):
                    normalized_trades.append(normalize_trade(filing, raw_tx, line_index))

                stats["filings_parsed"] += 1
                if used_ocr:
                    stats["filings_recovered_via_ocr"] += 1
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
                normalized_trades.append(
                    _unreadable_placeholder(filing_id, filer_name, pdf_url, row.get("FilingDate", ""))
                )
            except Exception as e:
                stats["filings_failed"] += 1
                monitoring.log_parse_failure(SOURCE_ID, filing_id, e, exc_info=True)
                dedup.record_result(
                    SOURCE_ID, filing_id, new_hash, PARSER_VERSION, "failed", 0, str(e),
                    datetime.now(timezone.utc).isoformat(),
                )
                normalized_trades.append(
                    _unreadable_placeholder(filing_id, filer_name, pdf_url, row.get("FilingDate", ""))
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
