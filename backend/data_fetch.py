"""
Runtime data ingestion for the Politician Trades app.

This module downloads publicly available data at runtime (never hardcoded)
from a small set of open data sources and loads it into the local SQLite
database (see db.py). It is designed to fail gracefully: if any individual
source is unreachable (network blocked, source down, etc.) the rest of the
refresh continues and a clear status message is recorded so the UI can show
the user what happened.

Data sources:
- Legislator bios/party/state/photo: unitedstates/congress-legislators
  (raw.githubusercontent.com, YAML) by default -- no API key required.
- Optional alternate source for the same legislator directory: the official
  Congress.gov API (api.congress.gov), used only if the user has configured
  a free api.data.gov API key in Settings. If configured but unreachable for
  any reason, this automatically falls back to the community YAML source, so
  the app always works with or without a key. Committees, committee
  membership, and trade data are NOT affected by this setting -- Congress.gov
  has no public API for financial disclosure/trade data, so that always comes
  from the sources below regardless of key configuration.
- Committees + membership: unitedstates/congress-legislators repo
- Photos: unitedstates/images (gh-pages branch), keyed by bioguide_id (used
  as a fallback if a source doesn't provide its own photo URL)
- Senate trades: timothycarambat/senate-stock-watcher-data aggregate JSON
- House trades: attempts the original House Stock Watcher S3 JSON dump first
  (network conditions vary by machine/region), and falls back to a
  community-maintained GitHub mirror if that is unreachable.

Caching: every downloaded source is cached to disk (data/cache/) along with
its ETag/Last-Modified headers (see _fetch_with_cache). On each refresh, a
conditional request is made first -- if the server confirms nothing has
changed (304 Not Modified, or an identical content hash as a fallback for
sources without conditional-request support), the body is NOT re-downloaded
and the corresponding database write is skipped entirely. This makes
frequent/automatic refreshes (see backend/app.py's scheduler) cheap: a
refresh where nothing has changed anywhere typically completes in a couple
of seconds instead of ~30-90.

Retention: trade disclosures are kept for a fixed, predictable
TRADE_HISTORY_YEARS (10) window rather than an ever-growing "whatever the
source happens to include" range (the underlying datasets go back further,
to ~2012-2013). Rows older than that cutoff are excluded when loading new
data, and _purge_old_trades() removes any already-loaded rows outside the
window on every refresh -- including data loaded before this limit existed.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import requests
import yaml

from . import db
from .committees_map import get_sectors_for_thomas_id
from .normalize import clean_name_tokens as _clean_name_tokens
from .pipeline.errors import RefreshCancelled, check_cancelled
from .settings import get_congress_api_key, get_custom_api_sources
from .us_states import state_name_to_code

# NOTE: pipeline.orchestrator.run_pipeline is deliberately imported lazily,
# inside refresh_data() below, rather than up here at module level. It
# pulls in house_clerk.py -> pdfplumber (and, transitively, lxml/PIL/
# pytesseract) -- fine for the full build, but the Lite build (see
# backend/snapshot_download.py) never bundles those at all. If this import
# were unconditional here, just importing data_fetch itself (which app.py
# does at startup) would crash the Lite build immediately with a
# ModuleNotFoundError before it ever got a chance to use the snapshot
# download path instead.

LEGISLATORS_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/legislators-current.yaml"
)
LEGISLATORS_HISTORICAL_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/legislators-historical.yaml"
)
COMMITTEES_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/committees-current.yaml"
)
COMMITTEE_MEMBERSHIP_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/committee-membership-current.yaml"
)
PHOTO_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/unitedstates/images/gh-pages/congress/225x275/{bioguide_id}.jpg"
)

CONGRESS_GOV_MEMBER_LIST_URL = "https://api.congress.gov/v3/member"

# Note: the Senate/House Stock Watcher JSON dump URLs used to live here as
# the primary trade data source. They're now only used as an automatic
# fallback by the pipeline (see backend/pipeline/secondary_sources.py) --
# the House Clerk bulk ZIP / Senate eFD search are the primary sources.


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PoliticianTradesApp/1.0; +https://github.com)",
    "Accept": "application/json, text/yaml, */*",
}

TIMEOUT = 30


# ---------------------------------------------------------------------------
# HTTP caching -- avoids re-downloading (and re-processing) a data source
# that hasn't changed since the last refresh. Uses the server's own ETag /
# Last-Modified headers when available (conditional GET -> 304 Not Modified
# means no re-download at all), and always falls back to a SHA-256 hash
# comparison of the downloaded content so "nothing changed" is detected
# correctly even against sources that don't support conditional requests.
# Cached raw bytes are kept in data/cache/ so a 304 response can still be
# parsed locally without hitting the network for the body.
# ---------------------------------------------------------------------------

def _cache_dir():
    d = os.path.join(db.get_data_dir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_file_path(source_key):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in source_key)
    return os.path.join(_cache_dir(), f"{safe}.raw")


def _get_cache_meta(source_key):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM source_cache WHERE source_key = ?", (source_key,)
        ).fetchone()
        return dict(row) if row else None


def _save_cache_meta(source_key, url, etag, last_modified, content_hash, changed):
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT last_changed_at FROM source_cache WHERE source_key = ?", (source_key,)
        ).fetchone()
        last_changed_at = now if changed else (existing["last_changed_at"] if existing else now)
        conn.execute(
            """
            INSERT INTO source_cache
                (source_key, url, etag, last_modified, content_hash, last_checked_at, last_changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                url=excluded.url,
                etag=excluded.etag,
                last_modified=excluded.last_modified,
                content_hash=excluded.content_hash,
                last_checked_at=excluded.last_checked_at,
                last_changed_at=excluded.last_changed_at
            """,
            (source_key, url, etag, last_modified, content_hash, now, last_changed_at),
        )


def _fetch_with_cache(url):
    """Fetches `url`, using a locally cached copy plus the server's ETag /
    Last-Modified headers (when supported) to avoid re-downloading content
    that hasn't changed since the last refresh.

    Returns (content_bytes, changed):
    - changed=False means the server confirmed (via 304 Not Modified, or an
      identical content hash) that nothing has changed -- content_bytes is
      the locally cached copy, not re-downloaded.
    - changed=True means new/updated content was downloaded (first run, or
      the source has genuinely changed upstream).

    The URL itself is used as the cache key, so each distinct source (and
    each House Stock Watcher fallback mirror) is tracked independently.
    """
    cache_path = _cache_file_path(url)
    prior = _get_cache_meta(url)
    have_cache_file = prior is not None and os.path.exists(cache_path)

    request_headers = dict(REQUEST_HEADERS)
    if have_cache_file:
        if prior.get("etag"):
            request_headers["If-None-Match"] = prior["etag"]
        if prior.get("last_modified"):
            request_headers["If-Modified-Since"] = prior["last_modified"]

    resp = requests.get(url, headers=request_headers, timeout=TIMEOUT)

    if resp.status_code == 304 and have_cache_file:
        with open(cache_path, "rb") as f:
            content = f.read()
        _save_cache_meta(
            url, url, prior.get("etag"), prior.get("last_modified"),
            prior.get("content_hash"), changed=False,
        )
        return content, False

    resp.raise_for_status()
    content = resp.content
    new_hash = hashlib.sha256(content).hexdigest()
    changed = (not have_cache_file) or (not prior) or new_hash != prior.get("content_hash")

    with open(cache_path, "wb") as f:
        f.write(content)
    _save_cache_meta(
        url, url, resp.headers.get("ETag"), resp.headers.get("Last-Modified"),
        new_hash, changed=changed,
    )
    return content, changed


def _fetch_yaml_cached(url):
    content, changed = _fetch_with_cache(url)
    return yaml.safe_load(content), changed




def _count_rows(table):
    with db.get_conn() as conn:
        return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]




def _committee_id_map_from_db():
    with db.get_conn() as conn:
        return {row["thomas_id"]: row["id"] for row in conn.execute("SELECT id, thomas_id FROM committees")}


# _clean_name_tokens now lives in backend/normalize.py (imported above) so
# it can be shared with the House Clerk / Senate eFD pipeline in
# backend/pipeline/ without duplicating this logic. The other former
# siblings here (_parse_amount_range, _normalize_type, _norm_date,
# _resolve_bioguide) are no longer used in this module now that trade
# loading itself has moved to backend/pipeline/ -- they still live in
# normalize.py for the pipeline's own use.


# How far back trade disclosures are retained. The Senate/House Stock
# Watcher datasets themselves actually go back further (~2012-2013), but we
# deliberately cap ingestion at a defined, predictable 10-year window rather
# than an ever-growing "whatever the source happens to include" range.
TRADE_HISTORY_YEARS = 10

# Default lookback window for a normal "Refresh Data" click (or an automatic
# background refresh): only trades within roughly the last 12 months are
# fetched/refreshed, which keeps routine refreshes fast. The UI lets the
# user override this per-refresh with a custom start date further back (see
# refresh_data's `since_date` param and _resolve_refresh_cutoff below), up
# to the TRADE_HISTORY_YEARS retention cap above.
DEFAULT_REFRESH_LOOKBACK_DAYS = 365

_pipeline_available = None  # cached result, see pipeline_available() below


def pipeline_available():
    """True if the live parsing pipeline (pdfplumber/lxml/pytesseract/etc,
    via pipeline.orchestrator) can actually be imported in this build.
    Always True in the full build; always False in the Lite build, which
    never bundles those dependencies at all (see backend/snapshot_download.py,
    which is what Lite uses instead). Checked once and cached -- whether the
    import succeeds can't change over the life of a running process."""
    global _pipeline_available
    if _pipeline_available is None:
        try:
            from .pipeline.orchestrator import run_pipeline  # noqa: F401

            _pipeline_available = True
        except ImportError:
            _pipeline_available = False
    return _pipeline_available


def _trade_history_cutoff():
    """Returns the oldest transaction_date (as an ISO string) that should
    ever be retained, i.e. today minus TRADE_HISTORY_YEARS. This is the
    outer retention cap enforced by _purge_old_trades on every refresh,
    independent of whichever (possibly narrower) window a given refresh
    actually fetched."""
    return (datetime.now(timezone.utc).date() - timedelta(days=365 * TRADE_HISTORY_YEARS)).isoformat()


def _default_refresh_cutoff():
    """Returns the oldest transaction_date (as an ISO string) fetched by a
    normal, non-customized refresh -- today minus DEFAULT_REFRESH_LOOKBACK_DAYS."""
    return (datetime.now(timezone.utc).date() - timedelta(days=DEFAULT_REFRESH_LOOKBACK_DAYS)).isoformat()


def default_refresh_cutoff():
    """Public wrapper around _default_refresh_cutoff(), for other modules
    (e.g. app.py) that need to display the default refresh window's start
    date in the UI."""
    return _default_refresh_cutoff()


def min_refresh_since_date():
    """Public wrapper exposing the oldest allowed custom start date (the
    TRADE_HISTORY_YEARS retention cap), for populating the UI's date picker
    bounds."""
    return _trade_history_cutoff()


def _resolve_refresh_cutoff(since_date):
    """Resolves the effective cutoff date (ISO string) used to fetch/refresh
    trades for a single refresh_data() run. `since_date`, if given (an ISO
    'YYYY-MM-DD' string, typically from the UI's custom start date picker),
    lets the user pull further back than the 12-month default -- but never
    further back than the TRADE_HISTORY_YEARS retention cap, and never later
    than the default 12-month cutoff (the picker is only meant for going
    further back, not less). Falls back to the default 12-month cutoff if
    since_date is missing or invalid."""
    default_cutoff = _default_refresh_cutoff()
    if not since_date:
        return default_cutoff
    try:
        parsed = datetime.strptime(since_date, "%Y-%m-%d").date().isoformat()
    except (ValueError, TypeError):
        return default_cutoff
    retention_cutoff = _trade_history_cutoff()
    if parsed > default_cutoff:
        return default_cutoff
    if parsed < retention_cutoff:
        return retention_cutoff
    return parsed




def _purge_old_trades():
    """Removes any already-loaded trades older than the retention window
    (see TRADE_HISTORY_YEARS). This runs on every refresh independent of
    whether anything was actually re-downloaded, so the 10-year window is
    honored even for rows that were loaded before this limit existed, or
    during a refresh where the source turned out to be unchanged. Rows with
    a missing/unparsable transaction date are never purged."""
    cutoff = _trade_history_cutoff()
    with db.get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM trades WHERE transaction_date IS NOT NULL "
            "AND transaction_date != '' AND transaction_date < ?",
            (cutoff,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Departed-politician lifecycle
#
# New politicians are added automatically: _load_legislators() upserts every
# entry in the current legislator directory on every refresh where that
# directory has changed (or on first run), so anyone newly sworn in (and
# therefore newly appearing in legislators-current.yaml / the Congress.gov
# API) is added to the politicians table -- and their publicly disclosed
# trades become attributable/visible -- the very next refresh after they
# appear upstream. No separate mechanism is needed for that half of the
# lifecycle.
#
# The other half -- politicians who leave office -- is handled below. A
# politician who drops out of the current legislator directory isn't purged
# immediately (a source hiccup or a mid-transition data gap shouldn't nuke
# someone's records), but is tracked in departed_politicians starting the
# first time they're observed missing. Once DEPARTED_GRACE_PERIOD_DAYS have
# elapsed continuously since first being observed missing,
# _mark_and_purge_departed_politicians() removes them (and their committee
# memberships, and every trade attributed to them) from the database
# entirely. If a tracked politician reappears in the current directory
# before the grace period elapses (e.g. re-elected in a special election),
# they're simply removed from departed_politicians and nothing is purged.
# ---------------------------------------------------------------------------

DEPARTED_GRACE_PERIOD_DAYS = 30


def _mark_and_purge_departed_politicians(current_bioguide_ids):
    """Reconciles the politicians table against `current_bioguide_ids` (the
    set of bioguide_ids present in the legislator directory as of this
    refresh):

    - Anyone in the politicians table but NOT in `current_bioguide_ids` is
      recorded in departed_politicians (if not already tracked there), with
      detected_at set to now.
    - Anyone already tracked in departed_politicians who IS back in
      `current_bioguide_ids` is untracked (they've returned to office).
    - Anyone tracked in departed_politicians for >= DEPARTED_GRACE_PERIOD_DAYS
      is permanently purged: their row in politicians, all rows in
      politician_committees, and all rows in trades attributed to their
      bioguide_id are deleted, along with their departed_politicians entry.

    Returns a dict summary: {"newly_tracked": int, "reinstated": int,
    "purged": [{"bioguide_id":, "full_name":}, ...]}.

    Only called when the current legislator directory was actually fetched
    successfully this run -- an empty/unavailable directory must never be
    treated as "everyone left office".
    """
    now = datetime.now(timezone.utc)
    summary: Dict[str, Any] = {"newly_tracked": 0, "reinstated": 0, "purged": []}

    with db.get_conn() as conn:
        existing_politicians = {
            row["bioguide_id"]: row["full_name"]
            for row in conn.execute("SELECT bioguide_id, full_name FROM politicians")
        }
        tracked = {
            row["bioguide_id"]: row["detected_at"]
            for row in conn.execute("SELECT bioguide_id, detected_at FROM departed_politicians")
        }

        # Newly missing this run -> start tracking.
        newly_missing = [
            bio for bio in existing_politicians
            if bio not in current_bioguide_ids and bio not in tracked
        ]
        if newly_missing:
            now_iso = now.isoformat()
            conn.executemany(
                "INSERT INTO departed_politicians (bioguide_id, full_name, detected_at) "
                "VALUES (?, ?, ?) ON CONFLICT(bioguide_id) DO NOTHING",
                [(bio, existing_politicians[bio], now_iso) for bio in newly_missing],
            )
            summary["newly_tracked"] = len(newly_missing)

        # Back in the current directory -> stop tracking (they returned to office).
        reinstated = [bio for bio in tracked if bio in current_bioguide_ids]
        if reinstated:
            conn.executemany(
                "DELETE FROM departed_politicians WHERE bioguide_id = ?",
                [(bio,) for bio in reinstated],
            )
            summary["reinstated"] = len(reinstated)

        # Anyone tracked long enough (and still gone) gets permanently purged.
        to_purge = []
        for bio, detected_at in tracked.items():
            if bio in current_bioguide_ids:
                continue  # just reinstated above
            try:
                detected_dt = datetime.fromisoformat(detected_at)
            except (ValueError, TypeError):
                continue
            age_days = (now - detected_dt).total_seconds() / 86400
            if age_days >= DEPARTED_GRACE_PERIOD_DAYS:
                to_purge.append(bio)

        for bio in to_purge:
            full_name = existing_politicians.get(bio)
            conn.execute("DELETE FROM politician_committees WHERE bioguide_id = ?", (bio,))
            conn.execute("DELETE FROM trades WHERE bioguide_id = ?", (bio,))
            conn.execute("DELETE FROM politicians WHERE bioguide_id = ?", (bio,))
            conn.execute("DELETE FROM departed_politicians WHERE bioguide_id = ?", (bio,))
            summary["purged"].append({"bioguide_id": bio, "full_name": full_name})

    return summary


def refresh_data(progress_cb=None, since_date=None, cancel_check=None, tracker=None):
    """Orchestrates a full refresh of all data. Returns a dict summary.

    progress_cb, if given, is called with a short status string after each
    step so the UI can show live progress.

    since_date, if given (an ISO 'YYYY-MM-DD' string), overrides the default
    12-month trade lookback window with a custom, further-back start date
    (see DEFAULT_REFRESH_LOOKBACK_DAYS / _resolve_refresh_cutoff). It's
    clamped to the TRADE_HISTORY_YEARS retention cap and can never be more
    recent than the default 12-month cutoff. Ignored/absent means "use the
    default 12-month window", which is what both the plain 'Refresh Data'
    button click and the automatic background scheduler use.

    cancel_check, if given, is a zero-arg callable returning True once the
    user has clicked "Stop Refresh" (see app.py's /api/refresh/stop and the
    Refresh Data dropdown menu). It's checked between each major step below
    (legislators, committees, committee membership, trade pipeline) and
    passed through to run_pipeline, which checks it between individual
    filings -- raising pipeline.errors.RefreshCancelled to unwind cleanly.
    Each step's database writes only happen after that step's own
    fetch+parse work completes, so stopping between steps never leaves
    partial/inconsistent data for the step that was interrupted.

    tracker, if given (see pipeline/progress.py), is passed through to
    run_pipeline to accumulate "filings discovered"/"filings processed"
    counts as the House Clerk/Senate eFD collectors work, driving the
    refresh progress bar/ETA shown in the UI (see /api/refresh/status).
    """

    def report(msg):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    trade_cutoff = _resolve_refresh_cutoff(since_date)

    summary: Dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "trade_cutoff": trade_cutoff,
        "steps": [],
        "errors": [],
    }

    db.init_db()
    check_cancelled(cancel_check)

    # 1. Legislators (current members shown in the UI). By default this
    # comes from the free community YAML directory. If the user has
    # configured an optional api.data.gov API key (see Settings in the UI),
    # we prefer the official Congress.gov API instead -- but if that key is
    # missing, invalid, or the API call fails for any reason, we transparently
    # fall back to the community YAML source so the app keeps working either way.
    #
    # Every step below uses _fetch_yaml_cached / _fetch_json_cached, which
    # transparently skips re-downloading a source if the server confirms
    # (via ETag/Last-Modified or a content-hash comparison) that nothing has
    # changed since the last refresh -- and, when unchanged, we also skip the
    # (comparatively expensive) database rewrite for that source, so a
    # refresh where nothing has changed anywhere is very fast.
    bioguide_lookup_by_name = {}
    legislators_loaded = False
    # Populated with the current directory's bioguide_ids (below) only when
    # that directory was actually fetched successfully this run -- stays
    # None if both the Congress.gov API and the community YAML source fail,
    # which must never be mistaken for "everyone left office" by the
    # departed-politician reconciliation step (1c) below.
    current_bioguide_ids = None
    api_key = get_congress_api_key()
    if api_key:
        try:
            report("Checking legislator directory via Congress.gov API (optional source)...")
            normalized = _fetch_legislators_congress_gov(api_key)
            if not normalized:
                raise RuntimeError("Congress.gov API returned no members")
            # api.congress.gov has no ETag support, so change detection here
            # is a straight content-hash comparison rather than a conditional
            # GET -- it still avoids the (more expensive) DB rewrite when
            # nothing has changed, even though the API calls themselves
            # (a handful of small paginated requests) still happen each time.
            canonical = json.dumps(normalized, sort_keys=True).encode()
            new_hash = hashlib.sha256(canonical).hexdigest()
            cache_key = "congress_gov_members"
            prior = _get_cache_meta(cache_key)
            changed = (not prior) or new_hash != prior.get("content_hash")
            _save_cache_meta(cache_key, CONGRESS_GOV_MEMBER_LIST_URL, None, None, new_hash, changed)

            if changed or _count_rows("politicians") == 0:
                _load_legislators(normalized, bioguide_lookup_by_name)
                summary["steps"].append(f"Loaded {len(normalized)} legislators (Congress.gov API, updated)")
                report(f"Loaded {len(normalized)} legislators (Congress.gov API)")
            else:
                _build_name_index(normalized, bioguide_lookup_by_name)
                summary["steps"].append("Legislator directory unchanged (Congress.gov API)")
                report("Legislator directory unchanged since last check (Congress.gov API)")
            db.set_meta("legislator_source", "Congress.gov API")
            legislators_loaded = True
            current_bioguide_ids = {n["bioguide"] for n in normalized if n and n.get("bioguide")}
        except Exception as e:
            summary["errors"].append(f"Congress.gov API: {e}")
            report(f"Congress.gov API unavailable ({e}); falling back to community legislator directory...")

    if not legislators_loaded:
        try:
            report("Checking legislator directory (community source)...")
            legislators_yaml, changed = _fetch_yaml_cached(LEGISLATORS_URL)
            normalized = [n for n in (_normalize_from_yaml_person(p) for p in legislators_yaml) if n]
            if changed or _count_rows("politicians") == 0:
                _load_legislators(normalized, bioguide_lookup_by_name)
                summary["steps"].append(f"Loaded {len(normalized)} legislators (community source, updated)")
                report(f"Loaded {len(normalized)} legislators")
            else:
                _build_name_index(normalized, bioguide_lookup_by_name)
                summary["steps"].append("Legislator directory unchanged (community source)")
                report("Legislator directory unchanged since last check -- using cached data")
            db.set_meta("legislator_source", "Community directory (congress-legislators)")
            current_bioguide_ids = {n["bioguide"] for n in normalized if n and n.get("bioguide")}
        except Exception as e:
            summary["errors"].append(f"Legislators: {e}")
            report(f"Failed to load legislators: {e}")

    # 1c. Reconcile the politicians table against the current legislator
    # directory fetched above: anyone newly appearing was already added by
    # _load_legislators()'s upsert; anyone who has dropped out of the
    # directory (retired, resigned, lost re-election, etc.) starts a
    # DEPARTED_GRACE_PERIOD_DAYS grace period, after which they (their
    # committee memberships and trades included) are purged entirely -- see
    # _mark_and_purge_departed_politicians. Only runs when we actually have
    # a current directory for this run (current_bioguide_ids set above) --
    # if both the Congress.gov API and the community YAML source failed,
    # we must not treat that as "everyone left office".
    if current_bioguide_ids:
        try:
            departed_summary = _mark_and_purge_departed_politicians(current_bioguide_ids)
            summary["departed_politicians"] = departed_summary
            if departed_summary["newly_tracked"]:
                report(
                    f"{departed_summary['newly_tracked']} politician(s) no longer in the current "
                    f"directory -- starting {DEPARTED_GRACE_PERIOD_DAYS}-day removal grace period"
                )
            if departed_summary["reinstated"]:
                report(f"{departed_summary['reinstated']} previously-departed politician(s) are back in office")
            if departed_summary["purged"]:
                purged_names = ", ".join(p["full_name"] or p["bioguide_id"] for p in departed_summary["purged"])
                summary["steps"].append(
                    f"Purged {len(departed_summary['purged'])} politician(s) no longer in office "
                    f"after {DEPARTED_GRACE_PERIOD_DAYS} days: {purged_names}"
                )
                report(f"Purged {len(departed_summary['purged'])} politician(s) no longer in office: {purged_names}")
        except Exception as e:
            summary["errors"].append(f"Departed politician cleanup: {e}")
            report(f"Failed to reconcile departed politicians: {e}")

    check_cancelled(cancel_check)

    # 1b. Historical legislators -- trade disclosures go back to 2012-2013 and
    # include many since-retired members. We only use this to improve name
    # matching for trade attribution; historical members are NOT added to the
    # politicians table/UI (which is scoped to current officeholders). There's
    # no DB write for this step at all, so "changed" only affects the log
    # message -- but the ~9MB download itself is still skipped when unchanged.
    try:
        report("Checking historical legislator directory (for trade matching)...")
        historical, changed = _fetch_yaml_cached(LEGISLATORS_HISTORICAL_URL)
        _index_historical_names(historical, bioguide_lookup_by_name)
        summary["steps"].append(
            f"Indexed {len(historical)} historical legislator names" + ("" if changed else " (unchanged, used cache)")
        )
        report(f"Indexed {len(historical)} historical legislator names")
    except Exception as e:
        summary["errors"].append(f"Historical legislators: {e}")
        report(f"Failed to load historical legislators: {e}")

    check_cancelled(cancel_check)

    # 2. Committees
    committee_id_by_thomas = {}
    try:
        report("Checking committee list...")
        committees, changed = _fetch_yaml_cached(COMMITTEES_URL)
        if changed or _count_rows("committees") == 0:
            committee_id_by_thomas = _load_committees(committees)
            summary["steps"].append(f"Loaded {len(committees)} committees (updated)")
            report(f"Loaded {len(committees)} committees")
        else:
            committee_id_by_thomas = _committee_id_map_from_db()
            summary["steps"].append("Committee list unchanged")
            report("Committee list unchanged since last check -- using cached data")
    except Exception as e:
        summary["errors"].append(f"Committees: {e}")
        report(f"Failed to load committees: {e}")

    check_cancelled(cancel_check)

    # 3. Committee membership
    try:
        report("Checking committee membership...")
        membership, changed = _fetch_yaml_cached(COMMITTEE_MEMBERSHIP_URL)
        if changed or _count_rows("politician_committees") == 0:
            count = _load_committee_membership(membership, committee_id_by_thomas)
            summary["steps"].append(f"Loaded {count} committee assignments (updated)")
            report(f"Loaded {count} committee assignments")
        else:
            summary["steps"].append("Committee membership unchanged")
            report("Committee membership unchanged since last check -- using cached data")
    except Exception as e:
        summary["errors"].append(f"Committee membership: {e}")
        report(f"Failed to load committee membership: {e}")

    # 4/5. Trade disclosures, via the resilient multi-source pipeline (see
    # backend/pipeline/): tries the user's custom API source first (if
    # configured/enabled), then the House Clerk bulk ZIP + Senate eFD search
    # (primary, most-authoritative sources), falling back automatically to
    # the House/Senate Stock Watcher JSON dumps if a primary source is
    # unreachable or its format can't be parsed. Every filing is
    # content-hashed so only new/changed filings are re-downloaded/
    # re-parsed (see pipeline/dedup.py), parse failures and format changes
    # are logged and alertable (see pipeline/monitoring.py and
    # /api/pipeline/status), and network calls retry transient failures
    # with backoff (see pipeline/http_client.py).
    try:
        from .pipeline.orchestrator import run_pipeline  # see the module-level NOTE above

        report("Checking congressional trade disclosures (House Clerk / Senate eFD)...")
        custom_sources = [s for s in get_custom_api_sources() if s.get("enabled")]
        pipeline_summary = run_pipeline(
            bioguide_lookup_by_name, trade_cutoff, progress_cb=report,
            custom_api_sources=custom_sources, cancel_check=cancel_check, tracker=tracker,
        )
        summary["pipeline"] = pipeline_summary
        summary["steps"].append(
            f"Trade pipeline loaded/updated {pipeline_summary['total_trades_loaded']} trades"
            + (f" (fallback used: {', '.join(pipeline_summary['fallbacks_used'])})"
               if pipeline_summary.get("fallbacks_used") else "")
        )
        if pipeline_summary.get("stale_sources"):
            stale_names = ", ".join(s["source"] for s in pipeline_summary["stale_sources"])
            summary["errors"].append(f"Stale data sources: {stale_names}")
            report(f"Warning: stale data sources detected: {stale_names}")
    except RefreshCancelled:
        raise
    except Exception as e:
        summary["errors"].append(f"Trade pipeline: {e}")
        report(f"Failed to run trade disclosure pipeline: {e}")

    # 6. Enforce the 10-year retention window (see TRADE_HISTORY_YEARS). Runs
    # every refresh regardless of whether anything above was re-downloaded,
    # so it also cleans up any pre-existing data from before this limit
    # existed, not just newly-loaded rows. This is intentionally independent
    # of trade_cutoff above -- a narrower refresh window never deletes older
    # data that's still within the 10-year retention cap.
    purged = _purge_old_trades()
    if purged:
        summary["steps"].append(f"Purged {purged} trades older than {TRADE_HISTORY_YEARS} years")
        report(f"Purged {purged} trades older than {TRADE_HISTORY_YEARS} years")

    # 7. Resolve tickers for trades that disclosed only an asset NAME (the
    # paper checkbox PTR form has no ticker column -- see
    # backend/ticker_resolve.py) by matching names against the ticker-
    # bearing e-filed trades already loaded above. Best-effort and strictly
    # fill-blanks-only; runs after loading so this refresh's new rows are
    # covered too.
    try:
        from . import ticker_resolve

        with db.get_conn() as conn:
            tr = ticker_resolve.resolve_missing_tickers(conn, progress_cb=report)
        if tr["updated_rows"]:
            summary["steps"].append(
                f"Resolved tickers for {tr['updated_rows']} trades from asset names "
                f"({tr['resolved_names']} distinct names)"
            )
            report(f"Resolved tickers for {tr['updated_rows']} name-only trades")
        summary["tickers_resolved"] = tr
    except Exception as e:
        summary["errors"].append(f"Ticker resolution: {e}")

    db.set_meta("last_updated", datetime.now(timezone.utc).isoformat())
    db.set_meta("last_refresh_summary", json.dumps(summary))
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary


def _current_term(person):
    terms = person.get("terms") or []
    return terms[-1] if terms else {}


def _normalize_from_yaml_person(person):
    """Normalizes one entry from the community congress-legislators YAML into
    the common legislator dict shape shared with the Congress.gov API path."""
    bio = person.get("id", {}).get("bioguide")
    if not bio:
        return None
    name = person.get("name", {})
    first = name.get("first", "")
    last = name.get("last", "")
    full_name = f"{first} {last}".strip()
    term = _current_term(person)
    party = term.get("party", "")
    state = term.get("state", "")
    chamber = "sen" if term.get("type") == "sen" else "rep"
    district = str(term.get("district")) if term.get("district") is not None else None
    photo_url = PHOTO_URL_TEMPLATE.format(bioguide_id=bio)
    return {
        "bioguide": bio,
        "first": first,
        "last": last,
        "full_name": full_name,
        "party": party,
        "state": state,
        "chamber": chamber,
        "district": district,
        "photo_url": photo_url,
        "nickname": name.get("nickname"),
    }


def _normalize_from_congress_member(member):
    """Normalizes one entry from the Congress.gov API's /v3/member list
    response into the same common legislator dict shape used above."""
    bio = member.get("bioguideId")
    if not bio:
        return None
    raw_name = member.get("name", "")  # typically "Last, First" (may include a suffix)
    parts = [p.strip() for p in raw_name.split(",") if p.strip()]
    if len(parts) >= 2:
        last, first = parts[0], parts[1]
    else:
        last, first = raw_name.strip(), ""
    full_name = f"{first} {last}".strip()

    party_name = (member.get("partyName") or "").strip()
    party_lower = party_name.lower()
    if party_lower.startswith("democrat"):
        party = "Democrat"
    elif party_lower.startswith("republican"):
        party = "Republican"
    else:
        party = party_name or "Independent"

    state = state_name_to_code(member.get("state", ""))

    terms = ((member.get("terms") or {}).get("item")) or []
    last_term = terms[-1] if terms else {}
    chamber_raw = (last_term.get("chamber") or "").lower()
    chamber = "sen" if "senate" in chamber_raw else "rep"

    district = member.get("district")
    district = str(district) if district is not None else None

    photo_url = (member.get("depiction") or {}).get("imageUrl") or PHOTO_URL_TEMPLATE.format(bioguide_id=bio)

    return {
        "bioguide": bio,
        "first": first,
        "last": last,
        "full_name": full_name,
        "party": party,
        "state": state,
        "chamber": chamber,
        "district": district,
        "photo_url": photo_url,
        "nickname": None,
    }


def _fetch_legislators_congress_gov(api_key, max_pages=10):
    """Paginates through the Congress.gov API's current-member directory and
    returns a list of normalized legislator dicts. Raises on any HTTP error
    (the caller falls back to the community YAML source in that case)."""
    normalized = []
    url = CONGRESS_GOV_MEMBER_LIST_URL
    params = {
        "api_key": api_key,
        "currentMember": "true",
        "limit": 250,
        "format": "json",
    }
    for _ in range(max_pages):
        resp = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        for member in data.get("members", []):
            norm = _normalize_from_congress_member(member)
            if norm:
                normalized.append(norm)

        next_url = (data.get("pagination") or {}).get("next")
        if not next_url:
            break
        # The 'next' link Congress.gov returns omits the api_key -- re-attach it.
        url = next_url if "api_key=" in next_url else next_url + f"&api_key={api_key}"
        params = None  # already encoded into `url`

    return normalized


def _build_name_index(normalized_legislators, bioguide_lookup_by_name):
    """Populates the in-memory last-name-indexed lookup used by
    _resolve_bioguide, WITHOUT writing anything to the database. This needs
    to run on every refresh regardless of whether the legislator directory
    itself changed, since it's required to attribute this run's (possibly
    new) trade disclosures to a bioguide_id."""
    for n in normalized_legislators:
        if not n or not n.get("bioguide"):
            continue
        bio = n["bioguide"]
        last_tokens = _clean_name_tokens(n["last"])
        if last_tokens:
            last_key = last_tokens[-1]
            first_tokens = set(_clean_name_tokens(n["first"]))
            nickname = n.get("nickname")
            if nickname:
                first_tokens |= set(_clean_name_tokens(nickname))
            bioguide_lookup_by_name.setdefault(last_key, []).append((first_tokens, bio))


def _load_legislators(normalized_legislators, bioguide_lookup_by_name):
    """Builds the name index (see _build_name_index) AND writes the
    politicians table. Source-agnostic -- works the same whether the data
    came from the YAML directory or the Congress.gov API. Call this only
    when the legislator directory has actually changed (or on first run);
    otherwise call _build_name_index directly to skip the redundant DB
    write."""
    _build_name_index(normalized_legislators, bioguide_lookup_by_name)

    rows = []
    for n in normalized_legislators:
        if not n or not n.get("bioguide"):
            continue
        bio = n["bioguide"]
        rows.append(
            (bio, n["first"], n["last"], n["full_name"], n["party"], n["state"],
             n["chamber"], n["district"], n["photo_url"])
        )

    with db.get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO politicians
                (bioguide_id, first_name, last_name, full_name, party, state, chamber, district, photo_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bioguide_id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                full_name=excluded.full_name,
                party=excluded.party,
                state=excluded.state,
                chamber=excluded.chamber,
                district=excluded.district,
                photo_url=excluded.photo_url
            """,
            rows,
        )


def _index_historical_names(historical, bioguide_lookup_by_name):
    """Adds name -> bioguide_id entries for former members of Congress so that
    older trade disclosures (data goes back to ~2012) can still be attributed
    to a bioguide_id, without adding them to the politicians table shown in
    the UI. Existing (current-member) entries take precedence on collision."""
    for person in historical:
        bio = person.get("id", {}).get("bioguide")
        if not bio:
            continue
        name = person.get("name", {})
        first = name.get("first", "")
        last = name.get("last", "")
        last_tokens = _clean_name_tokens(last)
        if not last_tokens:
            continue
        last_key = last_tokens[-1]
        first_tokens = set(_clean_name_tokens(first))
        nickname = name.get("nickname")
        if nickname:
            first_tokens |= set(_clean_name_tokens(nickname))
        bioguide_lookup_by_name.setdefault(last_key, []).append((first_tokens, bio))


def _load_committees(committees):
    """Loads both parent committees and their subcommittees. Returns a dict
    mapping thomas_id -> internal integer committee id."""
    committee_id_by_thomas = {}
    rows = []

    def chamber_for(thomas_id, ctype):
        if ctype:
            return ctype
        if thomas_id.startswith("H"):
            return "house"
        if thomas_id.startswith("S"):
            return "senate"
        return "joint"

    for c in committees:
        thomas_id = c.get("thomas_id")
        if not thomas_id:
            continue
        name = c.get("name", "")
        ctype = c.get("type")
        chamber = chamber_for(thomas_id, ctype)
        sectors = ",".join(get_sectors_for_thomas_id(thomas_id))
        rows.append((thomas_id, name, chamber, sectors))

        for sub in c.get("subcommittees", []) or []:
            sub_thomas = f"{thomas_id}{sub.get('thomas_id', '')}"
            sub_name = f"{name} - {sub.get('name', '')}"
            sub_sectors = ",".join(get_sectors_for_thomas_id(thomas_id))
            rows.append((sub_thomas, sub_name, chamber, sub_sectors))

    with db.get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO committees (thomas_id, name, chamber, sectors)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(thomas_id) DO UPDATE SET
                name=excluded.name,
                chamber=excluded.chamber,
                sectors=excluded.sectors
            """,
            rows,
        )
        for row in conn.execute("SELECT id, thomas_id FROM committees"):
            committee_id_by_thomas[row["thomas_id"]] = row["id"]

    return committee_id_by_thomas


def _load_committee_membership(membership, committee_id_by_thomas):
    rows = []
    for thomas_id, members in membership.items():
        committee_id = committee_id_by_thomas.get(thomas_id)
        if not committee_id:
            continue
        for m in members:
            bio = m.get("bioguide")
            if not bio:
                continue
            role = m.get("title", "")
            rows.append((bio, committee_id, role))

    with db.get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO politician_committees (bioguide_id, committee_id, role)
            VALUES (?, ?, ?)
            ON CONFLICT(bioguide_id, committee_id) DO UPDATE SET role=excluded.role
            """,
            rows,
        )
    return len(rows)


# Note: House/Senate trade loading used to happen here directly against
# the House/Senate Stock Watcher JSON dumps (_load_house_trades /
# _load_senate_trades / _replace_trades_for_chamber). That logic has been
# superseded by the resilient multi-source pipeline in backend/pipeline/
# (see refresh_data's step 4/5 above, which now calls
# pipeline.orchestrator.run_pipeline) -- House Clerk bulk ZIPs and Senate
# eFD search are now the primary sources, with the same Stock Watcher JSON
# dumps kept on as an automatic fallback (see pipeline/secondary_sources.py)
# rather than the first-choice source.
