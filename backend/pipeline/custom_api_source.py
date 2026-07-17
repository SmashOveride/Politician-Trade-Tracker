"""
Optional "try an API first" tier, sitting in front of the bulk-download
pipeline (House Clerk / Senate eFD). If the user has configured and enabled
a custom API source (see Settings > APIs in the UI; backend/settings.py's
add_custom_api_source) -- e.g. a paid Finnhub/FMP/Quiver Quantitative
endpoint, or any other congressional-trades API -- the pipeline tries it
first, since a ready-made JSON API is faster and lighter-weight than
downloading and parsing PDFs/HTML. Only if no custom source is configured,
or the configured one fails/returns an unrecognized shape, does the
orchestrator fall back to the bulk download + parser pipeline.

This mirrors the same "optional API first, transparent fallback" pattern
data_fetch.py already uses for the legislator directory (Congress.gov API
if a key is configured, else the community YAML source).

Because every third-party trades API has its own JSON shape and none is
standardized, this adapter uses a best-effort field-mapping heuristic
(_extract_field) rather than a hardcoded schema for one specific provider --
it looks for common key name variants (ticker/symbol, representative/
senator/name/politician, transaction_date/date/traded, amount/amount_range,
type/transaction_type/direction) and normalizes whatever it can. Records
that don't contain a usable ticker + politician name + date are skipped
rather than guessed at. If literally nothing in the response maps to those
fields, the response is treated as an unrecognized format so the pipeline
falls back to the bulk sources instead of silently loading zero/garbage
data.
"""

import json

from . import monitoring
from .http_client import build_session, fetch_with_cache
from .schema import RawFiling, normalize_trade

SOURCE_ID = "custom_api"


class UnrecognizedFormatError(Exception):
    pass


_TICKER_KEYS = ("ticker", "symbol", "stock", "asset_ticker")
_NAME_KEYS = ("representative", "senator", "politician", "name", "filer", "politician_name")
_DATE_KEYS = ("transaction_date", "date", "traded", "trade_date")
_DISCLOSURE_DATE_KEYS = ("disclosure_date", "filed_date", "reported_date")
_TYPE_KEYS = ("type", "transaction_type", "direction")
_AMOUNT_KEYS = ("amount", "amount_range", "range", "value")
_ASSET_DESC_KEYS = ("asset_description", "asset_name", "company", "asset")
_CHAMBER_KEYS = ("chamber", "office")
_LINK_KEYS = ("link", "ptr_link", "source_url", "url", "filing_url")


def _extract_field(record, candidate_keys):
    for key in candidate_keys:
        if key in record and record[key]:
            return record[key]
        # Some APIs use camelCase (e.g. "transactionDate") -- try that too.
        camel = "".join(
            w.capitalize() if i else w for i, w in enumerate(key.split("_"))
        )
        if camel in record and record[camel]:
            return record[camel]
    return None


def _guess_chamber(record, default_chamber=None):
    raw = _extract_field(record, _CHAMBER_KEYS)
    if raw:
        raw_lower = str(raw).strip().lower()
        if "sen" in raw_lower:
            return "senate"
        if "house" in raw_lower or "rep" in raw_lower:
            return "house"
    return default_chamber


def _extract_records(payload):
    """Custom APIs commonly return either a bare JSON array, or an object
    with the array under a common wrapper key (data/results/trades)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "results", "trades", "transactions"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return None


def fetch_and_normalize(custom_source, session=None):
    """Fetches `custom_source` (a dict with endpoint_url and optional
    api_key, as stored by settings.get_custom_api_sources()) and normalizes
    whatever trade-shaped records it can find. Raises UnrecognizedFormatError
    if the response can't be parsed as JSON, or contains no records that map
    to at least ticker + politician name + transaction date."""
    session = session or build_session()
    endpoint_url = custom_source.get("endpoint_url")
    api_key = custom_source.get("api_key") or ""
    name = custom_source.get("name") or endpoint_url

    headers = {}
    if api_key:
        # Try the most common convention (Authorization header). Some
        # free-tier finance APIs instead expect the key as a query param
        # (e.g. "?apikey=...") -- fetch_with_cache is a thin GET/POST
        # wrapper with no params= support, so a user whose API needs that
        # convention should include it directly in the endpoint_url they
        # configure in Settings.
        headers["Authorization"] = f"Bearer {api_key}"

    raw_bytes, _changed = fetch_with_cache(
        endpoint_url,
        session=session,
        cache_key=f"custom_api_{custom_source.get('id', name)}",
        headers=headers,
    )
    try:
        payload = json.loads(raw_bytes)
    except ValueError as e:
        raise UnrecognizedFormatError(f"Response from '{name}' is not valid JSON: {e}")

    records = _extract_records(payload)
    if records is None:
        raise UnrecognizedFormatError(
            f"Response from '{name}' is not a list (or list-valued data/results/trades/"
            f"transactions key) -- got {type(payload).__name__}"
        )

    normalized = []
    skipped = 0
    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            continue
        ticker = _extract_field(record, _TICKER_KEYS)
        filer_name = _extract_field(record, _NAME_KEYS)
        tx_date = _extract_field(record, _DATE_KEYS)
        if not (ticker and filer_name and tx_date):
            skipped += 1
            continue

        chamber = _guess_chamber(record) or "house"
        filing = RawFiling(
            source=SOURCE_ID,
            filing_id=str(
                _extract_field(record, _LINK_KEYS)
                or f"{custom_source.get('id')}:{filer_name}:{tx_date}:{ticker}"
            ),
            filer_name=str(filer_name),
            chamber=chamber,
            source_url=str(_extract_field(record, _LINK_KEYS) or ""),
            disclosure_date=str(_extract_field(record, _DISCLOSURE_DATE_KEYS) or ""),
        )
        raw_tx = {
            "ticker": str(ticker),
            "asset_description": str(_extract_field(record, _ASSET_DESC_KEYS) or ""),
            "raw_type": str(_extract_field(record, _TYPE_KEYS) or ""),
            "transaction_date": str(tx_date),
            "amount": str(_extract_field(record, _AMOUNT_KEYS) or ""),
        }
        normalized.append(normalize_trade(filing, raw_tx))

    if not normalized and records:
        raise UnrecognizedFormatError(
            f"None of the {len(records)} record(s) from '{name}' contained a recognizable "
            "ticker + politician name + transaction date"
        )

    return normalized, {"records_seen": len(records), "records_normalized": len(normalized), "records_skipped": skipped}


def try_enabled_custom_sources(custom_sources, progress_cb=None):
    """Tries each enabled custom source in turn (first one that returns
    usable data wins). Returns (normalized_trades, source_id_used) or
    ([], None) if none are configured/enabled or all fail."""
    session = build_session()
    for source in custom_sources:
        if not source.get("enabled"):
            continue
        if not source.get("endpoint_url"):
            continue
        source_key = f"{SOURCE_ID}:{source.get('id')}"
        monitoring.mark_attempt(source_key)
        try:
            normalized, stats = fetch_and_normalize(source, session=session)
            monitoring.mark_success(source_key)
            monitoring.log_info(
                source_key,
                f"Loaded {stats['records_normalized']} trades from custom source '{source.get('name')}'",
            )
            if progress_cb:
                progress_cb(f"Custom API source '{source.get('name')}': {stats['records_normalized']} trades")
            return normalized, source_key
        except UnrecognizedFormatError as e:
            monitoring.log_unrecognized_format(source_key, str(e))
            monitoring.mark_failure(source_key, e)
        except Exception as e:
            monitoring.mark_failure(source_key, e)
            if progress_cb:
                progress_cb(f"Custom API source '{source.get('name')}' failed: {e}")
    return [], None
