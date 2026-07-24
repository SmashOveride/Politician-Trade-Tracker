"""
Top-level orchestrator for the congressional trading data collection
pipeline: for each chamber, try the primary source (House Clerk bulk ZIP /
Senate eFD search) first; if it fails outright, fall back to the secondary
JSON-dump source. Every result is normalized into the common trade schema
(schema.py), attributed to a bioguide_id, filtered to the requested date
window, and upserted into the trades table (loader.py). Runs staleness
checks for every source at the end so a silently-broken source gets
surfaced even on a run where nothing else went wrong.
"""

from datetime import datetime, timezone
from typing import Any, Dict

from . import custom_api_source, house_clerk, monitoring, secondary_sources, senate_efd
from .errors import check_cancelled
from .http_client import build_session
from .loader import attribute_and_filter, purge_stale_filing_rows, upsert_trades

ALL_SOURCE_IDS = (
    house_clerk.SOURCE_ID,
    senate_efd.SOURCE_ID,
    secondary_sources.HOUSE_SECONDARY_ID,
    secondary_sources.SENATE_SECONDARY_ID,
)


def _years_since(cutoff_iso_date):
    """Returns the list of calendar years (as ints) from `cutoff_iso_date`
    (an ISO 'YYYY-MM-DD' string) through the current year, inclusive -- the
    set of House Clerk yearly ZIPs that need to be checked to cover the
    requested window."""
    start_year = int(cutoff_iso_date[:4])
    current_year = datetime.now(timezone.utc).year
    return list(range(start_year, current_year + 1))


def _to_senate_date_format(cutoff_iso_date):
    """Converts an ISO 'YYYY-MM-DD' cutoff into the 'MM/DD/YYYY' format the
    Senate eFD search form expects."""
    dt = datetime.strptime(cutoff_iso_date, "%Y-%m-%d")
    return dt.strftime("%m/%d/%Y")


def run_pipeline(
    bioguide_lookup_by_name, cutoff, progress_cb=None, custom_api_sources=None, cancel_check=None, tracker=None,
):
    """Runs the full pipeline and returns a summary dict with per-source
    stats and any fallbacks used. `bioguide_lookup_by_name` is the shared
    name index built by data_fetch.py (so trades resolve to the same
    bioguide_ids as the legacy path); `cutoff` is an ISO 'YYYY-MM-DD' date
    string bounding how far back to fetch. `custom_api_sources`, if given,
    is the user's list of enabled custom API sources (see settings.py) --
    tried first, before falling back to the House Clerk / Senate eFD bulk
    download + parser pipeline (with its own further fallback to the
    secondary Stock Watcher JSON dumps). `cancel_check`, if given, is a
    zero-arg callable returning True once the user has clicked "Stop
    Refresh" (see app.py's /api/refresh/stop) -- checked between major
    steps below (and passed through to the House/Senate collectors, which
    check it between individual filings) so a stop request unwinds
    promptly via pipeline.errors.RefreshCancelled rather than waiting for
    the whole pipeline to finish. `tracker`, if given (see
    pipeline/progress.py), is passed through to the House/Senate collectors
    to drive the refresh progress bar/ETA in the UI."""

    def report(msg):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    session = build_session()
    summary: Dict[str, Any] = {
        "sources": {}, "fallbacks_used": [], "total_trades_loaded": 0, "custom_api_source_used": None,
    }

    # --- Optional custom API source(s), tried first --------------------
    if custom_api_sources:
        report("Checking configured custom API source(s)...")
        custom_trades, source_used = custom_api_source.try_enabled_custom_sources(
            custom_api_sources, progress_cb=report
        )
        if source_used:
            summary["custom_api_source_used"] = source_used
            custom_trades = attribute_and_filter(custom_trades, bioguide_lookup_by_name, cutoff)
            custom_loaded = upsert_trades(custom_trades)
            summary["total_trades_loaded"] += custom_loaded
            report(f"Custom API source loaded {custom_loaded} trades -- skipping bulk download this run")
            summary["stale_sources"] = []
            for source_id in ALL_SOURCE_IDS:
                is_stale, last_success_at = monitoring.check_staleness(source_id)
                if is_stale:
                    summary["stale_sources"].append({"source": source_id, "last_success_at": last_success_at})
            return summary
        report("No usable custom API source -- falling back to bulk download + parser pipeline...")

    # --- House ---------------------------------------------------------
    check_cancelled(cancel_check)
    report("House Clerk: fetching bulk PTR filings...")
    house_trades, house_stats = house_clerk.collect_trades(
        _years_since(cutoff), session=session, progress_cb=report, cancel_check=cancel_check, tracker=tracker
    )
    house_primary_ok = house_stats["filings_parsed"] > 0 or house_stats["filings_skipped_cached"] > 0
    summary["sources"][house_clerk.SOURCE_ID] = house_stats

    if not house_primary_ok:
        monitoring.log_fallback_used(
            house_clerk.SOURCE_ID,
            "primary source produced no usable filings this run",
        )
        report("House Clerk unavailable -- falling back to secondary source...")
        check_cancelled(cancel_check)
        house_trades, house_fallback_stats = secondary_sources.collect_house_fallback(
            session=session, progress_cb=report, tracker=tracker
        )
        summary["sources"][secondary_sources.HOUSE_SECONDARY_ID] = house_fallback_stats
        summary["fallbacks_used"].append("house")

    house_trades = attribute_and_filter(house_trades, bioguide_lookup_by_name, cutoff)
    house_loaded = upsert_trades(house_trades)
    stale_purged = purge_stale_filing_rows(house_clerk.SOURCE_ID)
    if stale_purged:
        report(f"House: removed {stale_purged} stale rows from re-parsed filings")
    report(f"House: {house_loaded} trades loaded/updated")

    # --- Senate ----------------------------------------------------------
    check_cancelled(cancel_check)
    report("Senate eFD: searching for PTR filings...")
    senate_trades, senate_stats = senate_efd.collect_trades(
        _to_senate_date_format(cutoff), session=session, progress_cb=report, cancel_check=cancel_check, tracker=tracker
    )
    senate_primary_ok = senate_stats["filings_parsed"] > 0 or senate_stats["filings_skipped_cached"] > 0
    summary["sources"][senate_efd.SOURCE_ID] = senate_stats

    if not senate_primary_ok:
        monitoring.log_fallback_used(
            senate_efd.SOURCE_ID,
            "primary source produced no usable filings this run",
        )
        report("Senate eFD unavailable -- falling back to secondary source...")
        check_cancelled(cancel_check)
        senate_trades, senate_fallback_stats = secondary_sources.collect_senate_fallback(
            session=session, progress_cb=report, tracker=tracker
        )
        summary["sources"][secondary_sources.SENATE_SECONDARY_ID] = senate_fallback_stats
        summary["fallbacks_used"].append("senate")

    senate_trades = attribute_and_filter(senate_trades, bioguide_lookup_by_name, cutoff)
    senate_loaded = upsert_trades(senate_trades)
    report(f"Senate: {senate_loaded} trades loaded/updated")

    summary["total_trades_loaded"] = house_loaded + senate_loaded

    # --- Staleness check (every source, regardless of what happened above) --
    stale_sources = []
    for source_id in ALL_SOURCE_IDS:
        is_stale, last_success_at = monitoring.check_staleness(source_id)
        if is_stale:
            stale_sources.append({"source": source_id, "last_success_at": last_success_at})
    summary["stale_sources"] = stale_sources

    return summary
