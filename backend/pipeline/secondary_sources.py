"""
Secondary/fallback data sources, used only when a primary source (House
Clerk bulk ZIP+PDFs, Senate eFD search) fails outright for a given run --
e.g. the site is down, blocked on this network, or its format has changed
in a way the primary parser doesn't yet understand.

These wrap the same community JSON dumps data_fetch.py has used from the
start (House/Senate Stock Watcher), normalized into the same common trade
schema (see schema.py) so the orchestrator can treat "primary" and
"secondary" results identically once fetched.
"""

from collections import defaultdict

from . import monitoring
from .http_client import build_session, fetch_with_cache
from .schema import RawFiling, normalize_trade

HOUSE_SECONDARY_ID = "house_stock_watcher_fallback"
SENATE_SECONDARY_ID = "senate_stock_watcher_fallback"

SENATE_TRADES_URL = (
    "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json"
)
HOUSE_TRADES_URLS = [
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
    "https://raw.githubusercontent.com/TattooedHead/house-stock-watcher-data/main/data/all_transactions.json",
]


def collect_senate_fallback(session=None, progress_cb=None, tracker=None):
    """Fetches the Senate Stock Watcher aggregate JSON dump as a fallback
    for senate_efd. Returns (normalized_trades, stats). `tracker`, if given
    (see pipeline/progress.py), is treated as a single unit of work (this
    fallback is one bulk download, not a per-filing loop)."""
    import json

    session = session or build_session()
    stats = {"filings_seen": 0, "filings_parsed": 0, "filings_skipped_cached": 0, "filings_failed": 0}
    normalized_trades = []

    if tracker:
        tracker.add_total(1)

    monitoring.mark_attempt(SENATE_SECONDARY_ID)
    try:
        raw_bytes, _changed = fetch_with_cache(SENATE_TRADES_URL, session=session)
        trades = json.loads(raw_bytes)
    except Exception as e:
        monitoring.mark_failure(SENATE_SECONDARY_ID, e)
        if progress_cb:
            progress_cb(f"Senate fallback source failed: {e}")
        if tracker:
            tracker.add_completed()
        return normalized_trades, stats

    stats["filings_seen"] = len(trades)
    # A single PTR filing (one ptr_link) commonly lists many individual
    # trades, so a per-filing counter is needed to give each transaction
    # its own unique external_id -- otherwise every transaction sharing a
    # filing's link would collide on the (data_source, external_id) upsert
    # key used by loader.upsert_trades() and only the last one would survive.
    line_index_by_filing = defaultdict(int)
    for t in trades:
        senator = t.get("senator") or t.get("Senator") or ""
        filing_key = str(
            t.get("ptr_link") or t.get("link") or f"{senator}:{t.get('transaction_date')}"
        )
        filing = RawFiling(
            source=SENATE_SECONDARY_ID,
            filing_id=filing_key,
            filer_name=senator,
            chamber="senate",
            source_url=t.get("ptr_link") or t.get("link") or "",
            disclosure_date=t.get("disclosure_date") or "",
        )
        raw_tx = {
            "ticker": t.get("ticker") or "",
            "asset_description": t.get("asset_description") or t.get("asset_name") or "",
            "raw_type": t.get("type") or t.get("transaction_type") or "",
            "transaction_date": t.get("transaction_date") or "",
            "amount": t.get("amount") or "",
        }
        line_index = line_index_by_filing[filing_key]
        line_index_by_filing[filing_key] += 1
        normalized_trades.append(normalize_trade(filing, raw_tx, line_index))
    stats["filings_parsed"] = len(normalized_trades)
    monitoring.mark_success(SENATE_SECONDARY_ID)
    if tracker:
        tracker.add_completed()
    return normalized_trades, stats


def collect_house_fallback(session=None, progress_cb=None, tracker=None):
    """Fetches the House Stock Watcher aggregate JSON dump (trying each
    mirror in HOUSE_TRADES_URLS in turn) as a fallback for house_clerk.
    Returns (normalized_trades, stats). `tracker`, if given (see
    pipeline/progress.py), is treated as a single unit of work (this
    fallback is one bulk download, not a per-filing loop)."""
    import json

    session = session or build_session()
    stats = {"filings_seen": 0, "filings_parsed": 0, "filings_skipped_cached": 0, "filings_failed": 0}
    normalized_trades = []

    if tracker:
        tracker.add_total(1)

    monitoring.mark_attempt(HOUSE_SECONDARY_ID)
    trades = None
    last_error = None
    for url in HOUSE_TRADES_URLS:
        try:
            raw_bytes, _changed = fetch_with_cache(url, session=session)
            trades = json.loads(raw_bytes)
            break
        except Exception as e:
            last_error = e
            continue

    if trades is None:
        monitoring.mark_failure(HOUSE_SECONDARY_ID, last_error or "all mirrors unreachable")
        if progress_cb:
            progress_cb(f"House fallback source failed: {last_error}")
        if tracker:
            tracker.add_completed()
        return normalized_trades, stats

    stats["filings_seen"] = len(trades)
    # Same reasoning as collect_senate_fallback above: a single PTR filing's
    # ptr_link is shared by every trade it discloses, so each transaction
    # needs its own line_index to avoid colliding on the upsert key.
    line_index_by_filing = defaultdict(int)
    for t in trades:
        first = t.get("representative") or t.get("first_name") or ""
        last = t.get("last_name") or ""
        rep_name = t.get("representative") or f"{first} {last}".strip()
        filing_key = str(t.get("ptr_link") or f"{rep_name}:{t.get('transaction_date')}")
        filing = RawFiling(
            source=HOUSE_SECONDARY_ID,
            filing_id=filing_key,
            filer_name=rep_name,
            chamber="house",
            source_url=t.get("ptr_link") or "",
            disclosure_date=t.get("disclosure_date") or "",
        )
        raw_tx = {
            "ticker": t.get("ticker") or "",
            "asset_description": t.get("asset_description") or "",
            "raw_type": t.get("type") or "",
            "transaction_date": t.get("transaction_date") or "",
            "amount": t.get("amount") or "",
        }
        line_index = line_index_by_filing[filing_key]
        line_index_by_filing[filing_key] += 1
        normalized_trades.append(normalize_trade(filing, raw_tx, line_index))
    stats["filings_parsed"] = len(normalized_trades)
    monitoring.mark_success(HOUSE_SECONDARY_ID)
    if tracker:
        tracker.add_completed()
    return normalized_trades, stats
