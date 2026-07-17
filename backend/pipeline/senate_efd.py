"""
Primary source: the Senate's electronic Financial Disclosure search
(https://efdsearch.senate.gov/search/).

Unlike the House Clerk's static bulk ZIPs, the Senate eFD site requires:
  1. GETting the search home page to receive a CSRF token + session cookie.
  2. POSTing agreement to the site's terms (prohibition_agreement=1) using
     that token -- this is what unlocks the actual search endpoints for the
     rest of the session.
  3. POSTing to the DataTables-backed /search/report/data/ endpoint to list
     matching reports (filtered to report_types=[11], Periodic Transaction
     Reports, i.e. actual stock trades).
  4. GETting each individual report's HTML page
     (/search/view/ptr/{report_id}/) and parsing the transactions table on
     it.

This session handshake (steps 1-2) must be redone whenever a fresh
CSRF/session cookie is needed (each collect_trades() call gets its own
session), since the site's CSRF token is tied to the session cookie.

Format-version detection: the /search/view/ptr/ page's transaction table is
expected to have the exact header EXPECTED_TABLE_COLUMNS. If the Senate
changes it, that's reported as an unrecognized format rather than
mis-mapping columns silently.
"""

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from . import dedup, monitoring
from .errors import check_cancelled
from .http_client import build_session, fetch_with_cache
from .schema import RawFiling, normalize_trade

SOURCE_ID = "senate_efd"

BASE_URL = "https://efdsearch.senate.gov"
HOME_URL = f"{BASE_URL}/search/home/"
SEARCH_URL = f"{BASE_URL}/search/"
REPORT_DATA_URL = f"{BASE_URL}/search/report/data/"
REPORT_VIEW_URL_TEMPLATE = f"{BASE_URL}/search/view/ptr/{{report_id}}/"

# report_types=[11] is the Senate eFD site's internal code for "Periodic
# Transaction Report" (the only report type that lists stock trades).
PTR_REPORT_TYPE = "[11]"
SENATOR_FILER_TYPE = "[1]"

PARSER_VERSION = "senate_efd_html_v1"

EXPECTED_TABLE_COLUMNS = [
    "#", "Transaction Date", "Owner", "Ticker", "Asset Name", "Asset Type", "Type", "Amount", "Comment",
]

_REPORT_LINK_RE = re.compile(r"/search/view/ptr/([0-9a-fA-F-]+)/")
_CSRF_RE = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')

PAGE_SIZE = 100


class UnrecognizedFormatError(Exception):
    pass


def _start_session(session):
    """Performs the CSRF-token + agreement handshake required before any
    search/report endpoint will respond. Returns the csrf token string to
    use on subsequent POSTs (the Senate site issues a fresh csrftoken cookie
    after the agreement POST, which is what must be used from then on)."""
    resp = session.get(HOME_URL, timeout=30)
    resp.raise_for_status()
    m = _CSRF_RE.search(resp.text)
    if not m:
        raise UnrecognizedFormatError("Could not find csrfmiddlewaretoken on eFD home page")
    initial_csrf = m.group(1)

    resp2 = session.post(
        HOME_URL,
        data={"csrfmiddlewaretoken": initial_csrf, "prohibition_agreement": "1"},
        headers={"Referer": HOME_URL},
        timeout=30,
    )
    resp2.raise_for_status()

    csrf_cookie = session.cookies.get("csrftoken")
    if not csrf_cookie:
        raise UnrecognizedFormatError("No csrftoken cookie set after agreeing to eFD terms")
    return csrf_cookie


def _search_reports(session, csrf_token, start_date, progress_cb=None):
    """Paginates through the DataTables search endpoint for all Periodic
    Transaction Reports filed by senators on/after `start_date` (a
    'MM/DD/YYYY' string). Returns a list of (first_name, last_name,
    report_url_path) tuples."""
    results = []
    start = 0
    while True:
        payload = {
            "start": str(start),
            "length": str(PAGE_SIZE),
            "report_types": PTR_REPORT_TYPE,
            "filer_types": SENATOR_FILER_TYPE,
            "submitted_start_date": start_date,
            "submitted_end_date": "",
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
            "csrfmiddlewaretoken": csrf_token,
        }
        resp = session.post(
            REPORT_DATA_URL,
            data=payload,
            headers={"Referer": SEARCH_URL, "X-Requested-With": "XMLHttpRequest"},
            timeout=30,
        )
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError as e:
            raise UnrecognizedFormatError(f"Search endpoint did not return JSON: {e}")

        if "data" not in body:
            raise UnrecognizedFormatError(f"Search response missing 'data' key: {list(body.keys())}")

        rows = body["data"]
        if not rows:
            break

        for row in rows:
            if len(row) < 4:
                continue
            first_name, last_name, _filer_label, link_html = row[0], row[1], row[2], row[3]
            m = _REPORT_LINK_RE.search(link_html)
            if not m:
                continue
            results.append((first_name, last_name, m.group(1)))

        start += PAGE_SIZE
        if progress_cb and start % (PAGE_SIZE * 5) == 0:
            progress_cb(f"Senate eFD: found {len(results)} PTR filings so far...")
        if start >= body.get("recordsFiltered", 0):
            break

    return results


def _parse_amount_cell(text):
    return text.strip()


def parse_report_html(html, report_id):
    """Parses one PTR report's HTML page into a list of raw transaction
    dicts. Raises UnrecognizedFormatError if the transactions table's header
    doesn't match EXPECTED_TABLE_COLUMNS (rather than silently misreading
    columns), or if no transactions table is found at all."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        # Some PTRs are filed as a scanned/attached PDF instead of the
        # structured table (the eFD site supports both). This is a normal,
        # expected case -- not a format error -- we just can't extract
        # structured transactions from it.
        return []

    header_cells = [th.get_text(strip=True) for th in table.find_all("th")]
    if header_cells != EXPECTED_TABLE_COLUMNS:
        raise UnrecognizedFormatError(
            f"Transaction table header changed: expected {EXPECTED_TABLE_COLUMNS}, got {header_cells}"
        )

    transactions = []
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) != len(EXPECTED_TABLE_COLUMNS):
            continue
        (_num, tx_date, owner, ticker_cell, asset_name, _asset_type, tx_type, amount, _comment) = cells
        ticker = ticker_cell.get_text(strip=True)
        if ticker in ("--", ""):
            ticker = ""
        transactions.append(
            {
                "owner": owner.get_text(strip=True),
                "ticker": ticker,
                "asset_description": asset_name.get_text(strip=True),
                "raw_type": tx_type.get_text(strip=True),
                "transaction_date": tx_date.get_text(strip=True),
                "amount": _parse_amount_cell(amount.get_text()),
            }
        )
    return transactions


def collect_trades(start_date, session=None, progress_cb=None, cancel_check=None, tracker=None):
    """Top-level entry point. `start_date` is a 'MM/DD/YYYY' string (the
    Senate eFD search's own date format) bounding how far back to search.
    Returns (normalized_trades, stats). `cancel_check`, if given, is a
    zero-arg callable checked between individual report downloads -- see
    pipeline/errors.py -- so a user-requested "Stop Refresh" takes effect
    promptly instead of waiting for every found PTR report to be fetched
    first.

    `tracker`, if given (see pipeline/progress.py), has its total bumped
    once the full set of matching reports is known, and its completed count
    bumped once per report (parsed, skipped-as-cached, or failed) -- used to
    drive the refresh progress bar/ETA in the UI."""
    session = session or build_session()
    stats = {"filings_seen": 0, "filings_parsed": 0, "filings_skipped_cached": 0, "filings_failed": 0}
    normalized_trades = []

    monitoring.mark_attempt(SOURCE_ID)

    try:
        csrf_token = _start_session(session)
    except Exception as e:
        monitoring.mark_failure(SOURCE_ID, e)
        if isinstance(e, UnrecognizedFormatError):
            monitoring.log_unrecognized_format(SOURCE_ID, str(e))
        if progress_cb:
            progress_cb(f"Senate eFD: could not start search session ({e})")
        return normalized_trades, stats

    try:
        reports = _search_reports(session, csrf_token, start_date, progress_cb=progress_cb)
    except Exception as e:
        monitoring.mark_failure(SOURCE_ID, e)
        if isinstance(e, UnrecognizedFormatError):
            monitoring.log_unrecognized_format(SOURCE_ID, str(e))
        if progress_cb:
            progress_cb(f"Senate eFD: search failed ({e})")
        return normalized_trades, stats

    stats["filings_seen"] = len(reports)
    if tracker:
        tracker.add_total(len(reports))
    if progress_cb:
        progress_cb(f"Senate eFD: {len(reports)} PTR filings found")

    for first_name, last_name, report_id in reports:
        check_cancelled(cancel_check)
        filer_name = f"{first_name} {last_name}".strip()
        report_url = REPORT_VIEW_URL_TEMPLATE.format(report_id=report_id)

        try:
            raw_bytes, _changed = fetch_with_cache(
                report_url,
                session=session,
                cache_key=f"senate_efd_ptr_{report_id}",
                headers={"Referer": SEARCH_URL},
                use_conditional_headers=False,  # eFD report pages don't send ETag/Last-Modified
            )
        except Exception as e:
            stats["filings_failed"] += 1
            monitoring.log_parse_failure(SOURCE_ID, report_id, f"download failed: {e}")
            dedup.record_result(
                SOURCE_ID, report_id, None, PARSER_VERSION, "failed", 0, str(e),
                datetime.now(timezone.utc).isoformat(),
            )
            if tracker:
                tracker.add_completed()
            continue

        new_hash = dedup.content_hash(raw_bytes)
        if not dedup.should_process(SOURCE_ID, report_id, new_hash, parser_version=PARSER_VERSION):
            stats["filings_skipped_cached"] += 1
            if tracker:
                tracker.add_completed()
            continue

        try:
            raw_transactions = parse_report_html(raw_bytes.decode("utf-8", errors="replace"), report_id)
            filing = RawFiling(
                source=SOURCE_ID,
                filing_id=report_id,
                filer_name=filer_name,
                chamber="senate",
                source_url=report_url,
                disclosure_date="",  # not exposed on the report page itself; left blank rather than guessed
            )
            for line_index, raw_tx in enumerate(raw_transactions):
                normalized_trades.append(normalize_trade(filing, raw_tx, line_index))

            stats["filings_parsed"] += 1
            dedup.record_result(
                SOURCE_ID, report_id, new_hash, PARSER_VERSION, "ok",
                len(raw_transactions), None, datetime.now(timezone.utc).isoformat(),
            )
        except UnrecognizedFormatError as e:
            stats["filings_failed"] += 1
            monitoring.log_unrecognized_format(SOURCE_ID, f"report {report_id}: {e}")
            dedup.record_result(
                SOURCE_ID, report_id, new_hash, PARSER_VERSION, "unrecognized_format", 0, str(e),
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            stats["filings_failed"] += 1
            monitoring.log_parse_failure(SOURCE_ID, report_id, e, exc_info=True)
            dedup.record_result(
                SOURCE_ID, report_id, new_hash, PARSER_VERSION, "failed", 0, str(e),
                datetime.now(timezone.utc).isoformat(),
            )
        finally:
            if tracker:
                tracker.add_completed()

    if stats["filings_parsed"] > 0 or stats["filings_skipped_cached"] > 0:
        monitoring.mark_success(SOURCE_ID)
    elif stats["filings_seen"] == 0:
        monitoring.mark_failure(SOURCE_ID, "search returned no PTR filings")

    return normalized_trades, stats
