"""
Small local settings store (e.g. optional API keys) for the Politician
Trades app.

Settings are stored as plain JSON next to the SQLite database (in the same
portable `data/` folder), never inside the codebase and never committed to
git. Nothing here is sent anywhere except as credentials attached to the
user's own outbound requests to the API they configured.
"""

import json
import os

from . import db

SETTINGS_FILENAME = "settings.json"


def _settings_path():
    return os.path.join(db.get_data_dir(), SETTINGS_FILENAME)


def load_settings():
    path = _settings_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(updates):
    """Merges `updates` into the existing settings file and writes it back."""
    current = load_settings()
    current.update(updates)
    path = _settings_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
    return current


def get_congress_api_key():
    """Returns the user's optional api.data.gov / Congress.gov API key, or ''
    if none has been configured. This key is entirely optional -- the app
    works fully without it, using free community data sources instead."""
    return (load_settings().get("congress_gov_api_key") or "").strip()


DEFAULT_AUTO_REFRESH_MINUTES = 60  # every hour


def get_auto_refresh_minutes():
    """Returns how often (in minutes) the app should automatically check for
    new disclosures in the background, or 0 if automatic checking is
    disabled. Defaults to every hour. This is independent of the manual
    'Refresh Data' button, which always works regardless of this setting.
    Automatic refreshes are cheap when nothing has changed upstream (see
    data_fetch._fetch_with_cache), so a frequent default is safe."""
    value = load_settings().get("auto_refresh_minutes", DEFAULT_AUTO_REFRESH_MINUTES)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_AUTO_REFRESH_MINUTES


def mask_key(key):
    """Returns a masked representation of a key safe to display/return over
    the API (e.g. '••••••••wxyz'), never the full value."""
    if not key:
        return ""
    tail = key[-4:] if len(key) >= 4 else key
    return ("•" * max(len(key) - len(tail), 4)) + tail


# ---------------------------------------------------------------------------
# Trade disclosure data source registry (APIs panel)
#
# This stores which of the built-in candidate sources below the user has
# enabled, plus any custom sources/credentials they've added. The actual
# resilient multi-source pipeline lives in backend/pipeline/ (see its
# __init__.py for an overview) and is wired into data_fetch.refresh_data():
# House Clerk bulk ZIPs + Senate eFD search are the primary, most-
# authoritative sources; the House/Senate Stock Watcher JSON dumps are an
# automatic fallback if a primary source is unreachable or its format
# changes; a custom API source configured/enabled here is tried first of
# all, before the bulk download + parser pipeline. Every source is
# normalized into the same trades table schema, filings are content-hashed
# so only new/changed ones are re-parsed, parse failures and format-version
# mismatches are logged and alertable (see /api/pipeline/status), and every
# network call retries transient failures with backoff and caches
# responses locally.
# ---------------------------------------------------------------------------

# Built-in, well-known candidate sources for congressional trade disclosure
# data, grouped the same way they're presented in the APIs panel. The two
# "Primary Sources" entries below are always used by the pipeline (see
# backend/pipeline/house_clerk.py and senate_efd.py) regardless of their
# enabled state here -- that toggle is purely informational for these two.
# The "Secondary/Helper" entries are informational starter entries; adding
# a matching custom source (see custom source helpers below) and enabling
# it lets the pipeline try it first, before falling back to the two
# primary sources.
DEFAULT_API_SOURCES = [
    {
        "id": "house_clerk",
        "name": "House Clerk (Financial Disclosures)",
        "category": "Primary Sources (Most Authoritative)",
        "description": (
            "Bulk ZIP downloads of Periodic Transaction Reports (PTRs) -- "
            "yearly ZIPs with per-filer PDFs. The gold standard for accuracy, "
            "and always used by the House trade data pipeline."
        ),
        "url": "https://disclosures-clerk.house.gov/FinancialDisclosure",
    },
    {
        "id": "senate_fd",
        "name": "Senate Financial Disclosures",
        "category": "Primary Sources (Most Authoritative)",
        "description": (
            "Senate eFD search (efdsearch.senate.gov). Also gold-standard "
            "accuracy, and always used by the Senate trade data pipeline."
        ),
        "url": "https://efdsearch.senate.gov/search/",
    },
    {
        "id": "politician_trade_tracker_api",
        "name": "Politician Trade Tracker API",
        "category": "Secondary/Helper Sources (for easier parsing & timeliness)",
        "description": "Free-tier API for congressional trade data.",
        "url": "",
    },
    {
        "id": "finnhub_congressional_trades",
        "name": "Finnhub Congressional Trades",
        "category": "Secondary/Helper Sources (for easier parsing & timeliness)",
        "description": "Congressional trading endpoint on the Finnhub API.",
        "url": "https://finnhub.io/docs/api/congressional-trading",
    },
    {
        "id": "fmp",
        "name": "Financial Modeling Prep (FMP)",
        "category": "Secondary/Helper Sources (for easier parsing & timeliness)",
        "description": "Senate/House trading disclosure endpoints on the FMP API.",
        "url": "https://site.financialmodelingprep.com/developer/docs",
    },
    {
        "id": "quiver_quantitative",
        "name": "Quiver Quantitative",
        "category": "Secondary/Helper Sources (for easier parsing & timeliness)",
        "description": "Aggregator of congressional trading data. Check their API/terms.",
        "url": "https://www.quiverquant.com/congresstrading/",
    },
    {
        "id": "capitol_trades",
        "name": "Capitol Trades",
        "category": "Secondary/Helper Sources (for easier parsing & timeliness)",
        "description": "Aggregator of congressional trading data. Check their API/terms.",
        "url": "https://www.capitoltrades.com/",
    },
    {
        "id": "congress_trading_monitor",
        "name": "kadoa-org/congress-trading-monitor (GitHub)",
        "category": "Secondary/Helper Sources (for easier parsing & timeliness)",
        "description": "Open dataset/scraper tracking congressional trading disclosures.",
        "url": "https://github.com/kadoa-org/congress-trading-monitor",
    },
]


def get_enabled_source_ids():
    """Returns the set of built-in source ids the user has explicitly
    enabled. Defaults to none enabled. Only the House Clerk / Senate eFD
    primary sources are actually always used by the pipeline (regardless
    of this setting) -- this list mainly reflects which secondary/helper
    APIs are enabled as informational/future custom-source candidates."""
    value = load_settings().get("enabled_api_sources", [])
    return list(value) if isinstance(value, list) else []


def set_enabled_source_ids(ids):
    save_settings({"enabled_api_sources": list(dict.fromkeys(ids))})
    return get_enabled_source_ids()


def get_custom_api_sources():
    """Returns the user's custom-added API sources (name/endpoint/key/
    enabled), never including anything from DEFAULT_API_SOURCES."""
    value = load_settings().get("custom_api_sources", [])
    return list(value) if isinstance(value, list) else []


def _next_custom_source_id(existing):
    used = {s.get("id") for s in existing}
    n = 1
    while f"custom_{n}" in used:
        n += 1
    return f"custom_{n}"


def add_custom_api_source(name, endpoint_url, api_key=""):
    existing = get_custom_api_sources()
    source = {
        "id": _next_custom_source_id(existing),
        "name": name,
        "endpoint_url": endpoint_url,
        "api_key": api_key or "",
        "enabled": True,
    }
    existing.append(source)
    save_settings({"custom_api_sources": existing})
    return source


def remove_custom_api_source(source_id):
    existing = [s for s in get_custom_api_sources() if s.get("id") != source_id]
    save_settings({"custom_api_sources": existing})
    return existing


def set_custom_api_source_enabled(source_id, enabled):
    existing = get_custom_api_sources()
    for s in existing:
        if s.get("id") == source_id:
            s["enabled"] = bool(enabled)
    save_settings({"custom_api_sources": existing})
    return existing


def mask_custom_sources_for_display(sources):
    """Returns a copy of custom sources with api_key replaced by a masked
    preview, safe to send back to the frontend."""
    out = []
    for s in sources:
        copy = dict(s)
        key = copy.pop("api_key", "")
        copy["api_key_set"] = bool(key)
        copy["api_key_masked"] = mask_key(key)
        out.append(copy)
    return out
