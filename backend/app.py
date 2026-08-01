"""
Flask REST API for the Politician Trades desktop app.

This runs entirely on localhost (127.0.0.1), normally on a fixed default
port (see backend/launcher.py) so the app's URL is stable and safe to
bookmark in a browser across restarts. No data ever leaves the user's
machine except the outbound requests made by data_fetch.py to public data
sources.
"""

import os
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from . import asset_quality, db
from .data_fetch import default_refresh_cutoff, min_refresh_since_date, pipeline_available, refresh_data
from .pipeline.errors import RefreshCancelled
from .pipeline.progress import ProgressTracker
from .snapshot_download import sync_snapshot
from .settings import (
    DEFAULT_API_SOURCES,
    add_custom_api_source,
    get_auto_refresh_minutes,
    get_congress_api_key,
    get_custom_api_sources,
    get_enabled_source_ids,
    load_settings,
    mask_custom_sources_for_display,
    mask_key,
    remove_custom_api_source,
    save_settings,
    set_custom_api_source_enabled,
    set_enabled_source_ids,
)
from .ticker_sectors import get_sectors_for_ticker
from .market_data import get_index_series, get_price_history, has_cached_price_history
from .update_check import check_for_update
from .version import APP_VERSION

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
CORS(app)


@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


# App identity signature used by backend/launcher.py to detect whether an
# already-running process on a given port is genuinely this app (as opposed
# to some unrelated program that happens to be using the same port).
APP_SIGNATURE = "politician-trades-tracker"


@app.route("/api/ping")
def ping():
    return jsonify({"app": APP_SIGNATURE})

_refresh_lock = threading.Lock()
_refresh_status: Dict[str, Any] = {"running": False, "last_message": None, "stopping": False}
_refresh_tracker: ProgressTracker | None = None
_scheduler_started = False

# Set while a refresh is running; checked cooperatively at cancellation
# points throughout data_fetch.refresh_data() / the pipeline (see
# pipeline/errors.py) so a "Stop Refresh" click unwinds the current refresh
# promptly rather than waiting for it to finish on its own.
_refresh_cancel_event = threading.Event()


def _start_refresh_job(since_date=None):
    """Kicks off a refresh in a background thread, unless one is already
    running. Shared by both the manual 'Refresh Data' button (see
    trigger_refresh) and the automatic background scheduler below, so both
    paths report status/progress identically.

    since_date, if given (an ISO 'YYYY-MM-DD' string), overrides the default
    12-month trade lookback window with a custom, further-back start date
    (see the UI's date picker next to the Refresh Data button). Ignored by
    the automatic background scheduler, which always uses the default
    window."""
    if _refresh_lock.locked():
        return False

    global _refresh_tracker
    _refresh_cancel_event.clear()
    _refresh_status["stopping"] = False
    _refresh_tracker = ProgressTracker()

    def run():
        with _refresh_lock:
            _refresh_status["running"] = True
            try:
                def progress(msg):
                    _refresh_status["last_message"] = msg

                # The Lite build never bundles the live parsing pipeline's
                # dependencies (pdfplumber/lxml/pytesseract/etc -- see
                # data_fetch.pipeline_available and backend/snapshot_download.py),
                # so it downloads a pre-built snapshot instead of parsing
                # anything itself. The full build keeps doing exactly what
                # it always has -- pipeline_available() is unconditionally
                # True there, so this branch is unreachable and behavior is
                # unchanged. Which path runs is decided purely by which
                # dependencies are actually present, not a separate mode flag.
                if pipeline_available():
                    summary = refresh_data(
                        progress_cb=progress, since_date=since_date,
                        cancel_check=_refresh_cancel_event.is_set, tracker=_refresh_tracker,
                    )
                else:
                    progress("Checking for an updated data snapshot...")
                    summary = sync_snapshot()
                    if summary.get("errors"):
                        progress(f"Snapshot update failed: {'; '.join(summary['errors'])}")
                    else:
                        progress(summary["steps"][-1] if summary.get("steps") else "Up to date")
                _refresh_status["last_message"] = "Refresh complete"
                _refresh_status["last_summary"] = summary
                try:
                    _generate_all_notifications()
                except Exception:
                    pass
            except RefreshCancelled:
                _refresh_status["last_message"] = "Refresh stopped"
            except Exception as e:
                _refresh_status["last_message"] = f"Refresh failed: {e}"
            finally:
                _refresh_status["running"] = False
                _refresh_status["stopping"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


def _stop_refresh_job():
    """Requests that the currently-running refresh (if any) stop as soon as
    it reaches its next cancellation checkpoint (see pipeline/errors.py) --
    typically within a few seconds. Returns True if a refresh was actually
    running to stop, False if there was nothing to do. Whatever data was
    already loaded/committed before the stop takes effect is kept -- this
    never rolls back already-written rows."""
    if not _refresh_status["running"]:
        return False
    _refresh_status["stopping"] = True
    _refresh_status["last_message"] = "Stopping refresh…"
    _refresh_cancel_event.set()
    return True


# How often the background scheduler checks whether an automatic refresh is
# due. This is independent of (and much more frequent than) the actual
# refresh interval itself -- it's just a lightweight poll, not a refresh.
_AUTO_REFRESH_CHECK_SECONDS = 300


def _auto_refresh_loop():
    """Runs for the lifetime of the app. On startup, refreshes immediately
    if there's no data yet or the cached data is already older than the
    configured interval (so a first run, or reopening the app after a long
    time away, gets fresh disclosures right away without the user having to
    click anything). After that, wakes up periodically and triggers another
    refresh whenever the configured interval has elapsed. Thanks to the
    conditional-request caching in data_fetch.py, a refresh where nothing
    has actually changed upstream is cheap, so checking fairly often is
    safe. Disabled entirely if the user sets the interval to 0 in Settings."""
    time.sleep(5)  # let the server finish binding before the first check
    while True:
        try:
            interval_minutes = get_auto_refresh_minutes()
            if interval_minutes > 0 and not _refresh_status["running"]:
                last_updated = db.get_meta("last_updated")
                due = True
                if last_updated:
                    try:
                        last_dt = datetime.fromisoformat(last_updated)
                        due = (datetime.now(timezone.utc) - last_dt) >= timedelta(minutes=interval_minutes)
                    except ValueError:
                        due = True
                if due:
                    _start_refresh_job()
        except Exception:
            pass
        time.sleep(_AUTO_REFRESH_CHECK_SECONDS)


def _start_scheduler_once():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_auto_refresh_loop, daemon=True).start()


def create_app():
    db.init_db()
    try:
        _generate_all_notifications()
    except Exception:
        pass
    _start_scheduler_once()
    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _politician_row_to_dict(row, committees_by_bioguide=None):
    d = dict(row)
    if committees_by_bioguide is not None:
        d["committees"] = committees_by_bioguide.get(row["bioguide_id"], [])
    return d


def _fetch_committees_by_bioguide(conn, bioguide_ids=None):
    query = """
        SELECT pc.bioguide_id, c.thomas_id, c.name, c.chamber, c.sectors, pc.role
        FROM politician_committees pc
        JOIN committees c ON c.id = pc.committee_id
    """
    params = ()
    if bioguide_ids:
        placeholders = ",".join("?" for _ in bioguide_ids)
        query += f" WHERE pc.bioguide_id IN ({placeholders})"
        params = tuple(bioguide_ids)

    result = defaultdict(list)
    for row in conn.execute(query, params):
        result[row["bioguide_id"]].append(
            {
                "thomas_id": row["thomas_id"],
                "name": row["name"],
                "chamber": row["chamber"],
                "sectors": [s for s in (row["sectors"] or "").split(",") if s],
                "role": row["role"],
            }
        )
    return result


def _trade_row_to_dict(row):
    d = dict(row)
    # Substitute an honest "Unreadable" label for the raw OCR text on rows
    # where it's actually gibberish (see asset_quality.is_garbled) -- the
    # DB keeps the real OCR text untouched for later manual review/ticker
    # matching (see ticker_resolve.py), this only affects what's displayed.
    # Gated on ocr_sourced so this can never touch natively parsed e-filed
    # text (which is essentially never gibberish, but can look unusual
    # enough -- e.g. "WIX.COM LTD CMN" -- to false-positive the heuristic),
    # and skipped once a ticker's been resolved (a row with a known ticker
    # is meaningfully identified regardless of how rough its raw text is).
    # Also skipped once asset_description already starts with "Unreadable"
    # -- e.g. a manually reviewed row stamped "Unreadable. See record. P.3
    # Row4" -- since that normal-sentence-case text would otherwise trip
    # is_garbled()'s own lowercase-ratio check and get silently overwritten
    # with the generic label, destroying the specific page/row pointer.
    #
    # And skipped entirely once asset_description_reviewed is set: that
    # column means this row's text has already been finalized by a review
    # pass (see the Khanna asset-column cleanup) into clean, Title-Cased
    # presentation text ("Walmart Inc") rather than the raw ALL-CAPS OCR
    # text is_garbled() was designed to judge. Title Case is inherently
    # majority-lowercase by letter count, which trips is_garbled()'s own
    # lowercase-ratio check on nearly every real name -- confirmed on real
    # data: without this guard, 608 already-cleaned Khanna rows ("Realty
    # Income Corporation", "Ge Healthcare Technologies Inc", etc.) were
    # silently getting overwritten back to the generic label on every page
    # load, undoing the entire review pass at display time even though the
    # correct text was sitting right there in the database.
    desc = d.get("asset_description") or ""
    if (
        d.get("ocr_sourced")
        and not d.get("ticker")
        and not d.get("asset_description_reviewed")
        and not desc.startswith("Unreadable")
        and asset_quality.is_garbled(desc)
    ):
        d["asset_description"] = asset_quality.UNREADABLE_ASSET_LABEL
    return d


def _annotate_committee_conflicts(conn, trades):
    """Attaches `conflict_sectors` (list) and `conflict_flag` (bool) to each
    trade dict in place, based on whether the trade's ticker falls into a
    sector regulated/overseen by any committee the trading politician sits
    on. Trades with no resolved politician or no ticker-sector mapping are
    never flagged -- we don't guess, to avoid false positives. Returns the
    same list for convenience."""
    bioguide_ids = list({t["bioguide_id"] for t in trades if t.get("bioguide_id")})
    sectors_by_bioguide = defaultdict(set)
    if bioguide_ids:
        committees_by_bioguide = _fetch_committees_by_bioguide(conn, bioguide_ids)
        for bio, committees in committees_by_bioguide.items():
            for c in committees:
                sectors_by_bioguide[bio].update(c.get("sectors") or [])

    for t in trades:
        ticker_sectors = set(get_sectors_for_ticker(t.get("ticker")))
        politician_sectors = sectors_by_bioguide.get(t.get("bioguide_id"), set())
        overlap = sorted(ticker_sectors & politician_sectors)
        t["conflict_sectors"] = overlap
        t["conflict_flag"] = bool(overlap)
    return trades


def _nearest_close_on_or_before(price_series, date_str):
    """Returns the closing price from `price_series` ({date_str: close}) on
    `date_str` itself, or the most recent prior trading day if that exact
    date isn't a trading day (weekend/holiday). Returns None if there's no
    price on or before that date in the series at all."""
    if not price_series or not date_str:
        return None
    if date_str in price_series:
        return price_series[date_str]
    candidates = [d for d in price_series if d <= date_str]
    if not candidates:
        return None
    return price_series[max(candidates)]


def _annotate_realized_pnl(conn, trades, allow_network=True):
    """Attaches `profit_loss` (estimated $ gain/loss, may be negative) and
    `profit_loss_pct` to every sale ('sale' or 'sale_partial') trade dict in
    `trades`, in place. Left as None on a sale when there's nothing to
    compare it against, and left off purchase/exchange trades entirely.

    allow_network=False restricts price lookups to whatever's already
    cached (see market_data.get_price_history), so a page of trades touching
    several not-yet-cached tickers doesn't block on live Yahoo requests --
    used by /api/trades/recent, which pages through the entire dataset and
    can hit a fresh ticker on every page. Sales for a not-yet-cached ticker
    just show no profit/loss until that ticker's history gets fetched
    elsewhere (e.g. viewing its stock detail page, or the portfolio chart).

    Method: for each politician + ticker pair that has at least one sale
    among `trades`, this pulls that politician's *complete* trade history
    for that ticker from the database (not just whatever subset of trades
    is currently being displayed) and FIFO-matches each sale, oldest-first,
    against that politician's own prior disclosed purchases of the same
    ticker -- i.e. "the shares this database's records say they most likely
    still held" at the time of the sale. Each disclosed amount range's
    midpoint stands in for a dollar-denominated share count, since exact
    share counts are never disclosed.

    The matched purchase and sale dates are then looked up against real
    historical closing prices (see market_data.get_price_history) to
    compute an actual market-based percentage return, which is applied to
    the matched dollar amount to estimate a profit/loss in dollar terms.
    This is necessarily an estimate -- it assumes the disclosed dollar
    amount behaved like a single lot purchased/sold at that day's closing
    price, which is the best that can be done without disclosed share
    counts or intraday execution prices. Trades with no resolved politician,
    no ticker, or no matching prior purchase (e.g. stock held before this
    dataset's disclosure history begins) are left as None -- never guessed.
    Returns the same list for convenience.
    """
    sale_types = ("sale", "sale_partial")
    relevant_keys = {
        (t.get("bioguide_id") or t.get("politician_name"), t.get("ticker"))
        for t in trades
        if t.get("transaction_type") in sale_types and t.get("ticker")
        and (t.get("bioguide_id") or t.get("politician_name"))
    }
    if not relevant_keys:
        return trades

    # Budget how many tickers a single request may fetch price history for
    # over the network. Politicians who file the paper PTR form can have
    # hundreds of distinct (name-resolved) tickers on one detail page --
    # fetching them all live serially took the page from seconds to
    # minutes. Cached tickers (including cached failures, see
    # market_data.get_price_history) are always used; uncached ones beyond
    # the budget just show no P/L this visit and get picked up on a later
    # one as the cache warms.
    MAX_LIVE_PRICE_FETCHES = 25
    tickers = {ticker for (_key, ticker) in relevant_keys}
    live_budget = MAX_LIVE_PRICE_FETCHES if allow_network else 0
    price_history_by_ticker = {}
    for ticker in tickers:
        if has_cached_price_history(ticker) or live_budget <= 0:
            price_history_by_ticker[ticker] = get_price_history(ticker, allow_network=False)
        else:
            live_budget -= 1
            price_history_by_ticker[ticker] = get_price_history(ticker)

    # Pull each relevant politician+ticker pair's *complete* trade history
    # (not just the currently-displayed subset) so FIFO matching is correct
    # even when `trades` is itself already filtered (e.g. one stock's page,
    # or a date-filtered /api/trades query).
    history_by_key = defaultdict(list)
    for bioguide_id_or_name, ticker in relevant_keys:
        rows = conn.execute(
            """
            SELECT id, bioguide_id, politician_name, ticker, transaction_type,
                   transaction_date, amount_min, amount_max
            FROM trades
            WHERE ticker = ? AND (bioguide_id = ? OR (bioguide_id IS NULL AND politician_name = ?))
            ORDER BY transaction_date ASC, id ASC
            """,
            (ticker, bioguide_id_or_name, bioguide_id_or_name),
        ).fetchall()
        history_by_key[(bioguide_id_or_name, ticker)] = [dict(r) for r in rows]

    # trade id -> computed result, so we can apply results back onto the
    # caller's (possibly differently-shaped/filtered) trade dicts by id.
    results_by_trade_id = {}

    for key, history in history_by_key.items():
        _ticker = key[1]
        price_series = price_history_by_ticker.get(_ticker)
        lots = deque()  # FIFO queue of {"date": purchase_date, "amount": remaining $}
        for t in history:
            ttype = t.get("transaction_type")
            mid = _amount_midpoint(t)
            if ttype == "purchase":
                if mid:
                    lots.append({"date": t.get("transaction_date"), "amount": mid})
            elif ttype in sale_types:
                remaining = mid
                cost_basis = 0.0
                proceeds = 0.0
                sell_price = _nearest_close_on_or_before(price_series, t.get("transaction_date"))
                while remaining > 1e-9 and lots:
                    lot = lots[0]
                    take = min(lot["amount"], remaining)
                    buy_price = _nearest_close_on_or_before(price_series, lot["date"])
                    if buy_price and sell_price:
                        shares = take / buy_price
                        cost_basis += take
                        proceeds += shares * sell_price
                    lot["amount"] -= take
                    remaining -= take
                    if lot["amount"] <= 1e-9:
                        lots.popleft()
                if cost_basis > 0:
                    profit_loss = proceeds - cost_basis
                    results_by_trade_id[t["id"]] = {
                        "profit_loss": round(profit_loss, 2),
                        "profit_loss_pct": round((profit_loss / cost_basis) * 100, 2),
                    }
                else:
                    results_by_trade_id[t["id"]] = {"profit_loss": None, "profit_loss_pct": None}

    for t in trades:
        if t.get("transaction_type") in sale_types:
            result = results_by_trade_id.get(t.get("id"), {"profit_loss": None, "profit_loss_pct": None})
            t["profit_loss"] = result["profit_loss"]
            t["profit_loss_pct"] = result["profit_loss_pct"]

    return trades


def _annotate_party(conn, trades):
    """Attaches `party` to each trade dict in place, by looking up the
    trading politician's party from the politicians table. Left as None for
    trades that never resolved to a current-officeholder bioguide_id (e.g.
    retired/historical members) -- the frontend simply doesn't show a party
    indicator in that case rather than guessing."""
    bioguide_ids = list({t["bioguide_id"] for t in trades if t.get("bioguide_id")})
    party_by_bioguide = {}
    if bioguide_ids:
        placeholders = ",".join("?" for _ in bioguide_ids)
        for row in conn.execute(
            f"SELECT bioguide_id, party FROM politicians WHERE bioguide_id IN ({placeholders})",
            bioguide_ids,
        ):
            party_by_bioguide[row["bioguide_id"]] = row["party"]

    for t in trades:
        t["party"] = party_by_bioguide.get(t.get("bioguide_id"))
    return trades


def _amount_midpoint(row):
    amin = row["amount_min"]
    amax = row["amount_max"]
    if amin is None and amax is None:
        return 0
    if amin is None:
        return amax
    if amax is None:
        return amin
    return (amin + amax) / 2.0


RANGE_TO_MONTHS = {
    "1m": 1,
    "3m": 3,
    "6m": 6,
    "1y": 12,
    "5y": 60,
}


def _range_start_date(range_key):
    today = datetime.utcnow().date()
    if range_key == "ytd":
        return today.replace(month=1, day=1)
    months = RANGE_TO_MONTHS.get(range_key, 12)
    return today - timedelta(days=months * 31)


# ---------------------------------------------------------------------------
# Politicians
# ---------------------------------------------------------------------------

@app.route("/api/politicians")
def list_politicians():
    party = request.args.get("party")
    state = request.args.get("state")
    chamber = request.args.get("chamber")
    search = request.args.get("search")

    query = "SELECT * FROM politicians WHERE 1=1"
    params = []
    if party:
        query += " AND party = ?"
        params.append(party)
    if state:
        query += " AND state = ?"
        params.append(state)
    if chamber:
        query += " AND chamber = ?"
        params.append(chamber)
    if search:
        query += " AND full_name LIKE ?"
        params.append(f"%{search}%")
    query += " ORDER BY last_name, first_name"

    with db.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        bioguide_ids = [r["bioguide_id"] for r in rows]
        committees_by_bioguide = _fetch_committees_by_bioguide(conn, bioguide_ids)

        volume_rows = conn.execute(
            """
            SELECT bioguide_id,
                   COUNT(*) as trade_count,
                   SUM((COALESCE(amount_min,0)+COALESCE(amount_max,0))/2.0) as total_volume
            FROM trades
            WHERE bioguide_id IS NOT NULL
            GROUP BY bioguide_id
            """
        ).fetchall()
        volume_by_bioguide = {
            r["bioguide_id"]: {"trade_count": r["trade_count"], "total_volume": r["total_volume"] or 0}
            for r in volume_rows
        }

    politicians = []
    for row in rows:
        d = _politician_row_to_dict(row, committees_by_bioguide)
        stats = volume_by_bioguide.get(row["bioguide_id"], {"trade_count": 0, "total_volume": 0})
        d.update(stats)
        politicians.append(d)

    return jsonify(politicians)


@app.route("/api/politicians/<bioguide_id>")
def get_politician(bioguide_id):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM politicians WHERE bioguide_id = ?", (bioguide_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404

        committees_by_bioguide = _fetch_committees_by_bioguide(conn, [bioguide_id])
        d = _politician_row_to_dict(row, committees_by_bioguide)

        trades = conn.execute(
            """
            SELECT * FROM trades WHERE bioguide_id = ?
            ORDER BY transaction_date DESC
            """,
            (bioguide_id,),
        ).fetchall()
        d["trades"] = [_trade_row_to_dict(t) for t in trades]
        _annotate_committee_conflicts(conn, d["trades"])
        _annotate_realized_pnl(conn, d["trades"])

        total_volume = sum(_amount_midpoint(t) for t in trades)
        d["total_volume"] = total_volume
        d["trade_count"] = len(trades)
        d["purchase_count"] = sum(1 for t in trades if t["transaction_type"] == "purchase")
        d["sale_count"] = sum(
            1 for t in trades if t["transaction_type"] in ("sale", "sale_partial")
        )

    return jsonify(d)


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

@app.route("/api/trades")
def list_trades():
    ticker = request.args.get("ticker")
    bioguide_id = request.args.get("bioguide_id")
    ttype = request.args.get("type")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    search = request.args.get("search")

    query = "SELECT * FROM trades WHERE 1=1"
    params = []
    if ticker:
        query += " AND ticker = ?"
        params.append(ticker.upper())
    if bioguide_id:
        query += " AND bioguide_id = ?"
        params.append(bioguide_id)
    if ttype:
        query += " AND transaction_type = ?"
        params.append(ttype)
    if start_date:
        query += " AND transaction_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND transaction_date <= ?"
        params.append(end_date)
    if search:
        query += " AND (politician_name LIKE ? OR ticker LIKE ? OR asset_description LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like, like])
    query += " ORDER BY transaction_date DESC LIMIT 2000"

    with db.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        trades = [_trade_row_to_dict(r) for r in rows]
        _annotate_committee_conflicts(conn, trades)
        _annotate_realized_pnl(conn, trades)

    return jsonify(trades)


DEFAULT_RECENT_TRADES_PAGE_SIZE = 50
MAX_RECENT_TRADES_PAGE_SIZE = 200

# Standard STOCK Act disclosure amount buckets, identified by their floor
# dollar value -- parse_amount_range always extracts that floor into
# amount_min exactly, even when the bucket's display text (amount_range) has
# minor OCR garbling, so filtering on amount_min is robust where matching
# amount_range text wouldn't be. Kept in sync by hand with the equivalent
# list in frontend/app.js (AMOUNT_BUCKETS).
RECENT_TRADES_AMOUNT_BUCKET_FLOORS = {
    1001, 15001, 50001, 100001, 250001, 500001, 1000001, 5000001, 25000001, 50000000,
}


@app.route("/api/trades/recent")
def list_recent_trades():
    """Returns every disclosed trade on file, most recently *disclosed*
    first (i.e. newest filings first), paginated. This is deliberately keyed
    off disclosure_date rather than transaction_date -- members of Congress
    often file weeks after the actual trade, so "newest disclosures" is
    what's actually new/actionable information as of today, not the
    underlying (older) trade date. Trades with no disclosure_date on file
    can't be placed in this ordering and are excluded.

    Query params:
      - page (default 1)
      - page_size (default 50, max 200)
      - start_date / end_date: bound transaction_date (not disclosure_date --
        a user filtering "trades in March" means the trade itself, not
        whenever it happened to be filed)
      - type: comma-separated transaction_type values (purchase/sale/
        sale_partial/exchange)
      - party: comma-separated politicians.party values (Democrat/
        Republican/Independent)
      - amount_buckets: comma-separated bucket-floor dollar values (see
        RECENT_TRADES_AMOUNT_BUCKET_FLOORS) -- matched against amount_min
        exactly, so only recognized buckets are ever passed through
      - search: matched against ticker or asset_description
    """
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        page_size = max(1, min(int(request.args.get("page_size", DEFAULT_RECENT_TRADES_PAGE_SIZE)), MAX_RECENT_TRADES_PAGE_SIZE))
    except ValueError:
        page_size = DEFAULT_RECENT_TRADES_PAGE_SIZE

    where = ["disclosure_date IS NOT NULL", "disclosure_date != ''"]
    params: list = []

    start_date = request.args.get("start_date")
    if start_date:
        where.append("transaction_date >= ?")
        params.append(start_date)
    end_date = request.args.get("end_date")
    if end_date:
        where.append("transaction_date <= ?")
        params.append(end_date)

    types = [t.strip() for t in (request.args.get("type") or "").split(",") if t.strip()]
    if types:
        where.append(f"transaction_type IN ({','.join('?' for _ in types)})")
        params.extend(types)

    parties = [p.strip() for p in (request.args.get("party") or "").split(",") if p.strip()]
    if parties:
        where.append(
            f"bioguide_id IN (SELECT bioguide_id FROM politicians WHERE party IN ({','.join('?' for _ in parties)}))"
        )
        params.extend(parties)

    bucket_floors = []
    for raw in (request.args.get("amount_buckets") or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        if val in RECENT_TRADES_AMOUNT_BUCKET_FLOORS:
            bucket_floors.append(val)
    if bucket_floors:
        where.append(f"amount_min IN ({','.join('?' for _ in bucket_floors)})")
        params.extend(bucket_floors)

    search = (request.args.get("search") or "").strip()
    if search:
        where.append("(ticker LIKE ? OR asset_description LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])

    where_sql = " AND ".join(where)

    with db.get_conn() as conn:
        total_row = conn.execute(f"SELECT COUNT(*) as c FROM trades WHERE {where_sql}", params).fetchone()
        total = total_row["c"] if total_row else 0

        # Always reflects the freshest disclosure on file overall, regardless
        # of the current filter selection -- this is "how fresh is the
        # underlying data", not "latest disclosure among filtered results".
        max_row = conn.execute(
            "SELECT MAX(disclosure_date) as max_date FROM trades "
            "WHERE disclosure_date IS NOT NULL AND disclosure_date != ''"
        ).fetchone()
        max_date = max_row["max_date"] if max_row else None

        if total == 0:
            return jsonify(
                {"trades": [], "total": 0, "page": 1, "page_size": page_size, "total_pages": 1, "latest_disclosure_date": max_date}
            )

        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * page_size

        rows = conn.execute(
            f"""
            SELECT * FROM trades
            WHERE {where_sql}
            ORDER BY disclosure_date DESC, transaction_date DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, offset],
        ).fetchall()

        trades = [_trade_row_to_dict(r) for r in rows]
        _annotate_committee_conflicts(conn, trades)
        _annotate_party(conn, trades)
        # Cache-only price lookups here (see _annotate_realized_pnl) -- this
        # endpoint pages through the whole dataset, so blocking on a live
        # Yahoo request for every not-yet-cached ticker made page turns slow.
        _annotate_realized_pnl(conn, trades, allow_network=False)

    return jsonify(
        {
            "trades": trades,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "latest_disclosure_date": max_date,
        }
    )


# ---------------------------------------------------------------------------
# Stocks (tickers owned by multiple politicians, volume charts)
# ---------------------------------------------------------------------------

def sort_stocks_by_politician_count(conn, min_politicians=1, include_politicians=True):
    """Takes the full list of stocks disclosed in the dataset, counts how
    many unique politicians hold/have traded each one, and returns them
    sorted from most to least widely held (ties broken by total disclosed
    dollar volume, descending).

    Returns a list of dicts, each with:
      - ticker
      - company_name: best-effort, the most commonly disclosed asset
        description for that ticker. There's no separate curated
        company-name field -- only whatever text appears on each filing,
        which can vary slightly between filings for the same ticker (e.g.
        "Apple Inc" vs "Apple Inc - Common Stock"), so this picks the most
        frequent one rather than guessing at a canonical name.
      - politician_count: number of distinct politicians who disclosed a
        trade in this ticker
      - trade_count: total number of disclosed trades in this ticker
      - total_value: sum of disclosed transaction amounts (using the
        midpoint of each reported range) across all politicians -- a
        disclosure-based estimate, not a verified current market value
      - politicians: list of {bioguide_id, full_name} who traded it
        (omitted if include_politicians=False), suitable for rendering as
        clickable links to each politician's detail page

    `min_politicians` filters out tickers held by fewer than this many
    distinct politicians (default 1 = every ticker in the dataset).
    """
    rows = conn.execute(
        """
        SELECT ticker,
               COUNT(DISTINCT COALESCE(bioguide_id, politician_name)) as politician_count,
               COUNT(*) as trade_count,
               SUM((COALESCE(amount_min,0)+COALESCE(amount_max,0))/2.0) as total_value
        FROM trades
        WHERE ticker IS NOT NULL AND ticker != ''
        GROUP BY ticker
        HAVING politician_count >= ?
        ORDER BY politician_count DESC, total_value DESC
        """,
        (min_politicians,),
    ).fetchall()

    tickers = [r["ticker"] for r in rows]
    if not tickers:
        return []
    placeholders = ",".join("?" for _ in tickers)

    # Best-effort company name: the most frequently disclosed asset
    # description per ticker.
    name_rows = conn.execute(
        f"""
        SELECT ticker, asset_description, COUNT(*) as c
        FROM trades
        WHERE ticker IN ({placeholders}) AND asset_description IS NOT NULL AND asset_description != ''
        GROUP BY ticker, asset_description
        ORDER BY ticker, c DESC
        """,
        tickers,
    ).fetchall()
    company_name_by_ticker = {}
    for r in name_rows:
        company_name_by_ticker.setdefault(r["ticker"], r["asset_description"])

    politicians_by_ticker = defaultdict(dict)
    if include_politicians:
        combo_rows = conn.execute(
            f"SELECT DISTINCT ticker, bioguide_id, politician_name FROM trades WHERE ticker IN ({placeholders})",
            tickers,
        ).fetchall()
        bio_ids = list({r["bioguide_id"] for r in combo_rows if r["bioguide_id"]})
        name_by_bio = {}
        if bio_ids:
            bio_placeholders = ",".join("?" for _ in bio_ids)
            for pr in conn.execute(
                f"SELECT bioguide_id, full_name FROM politicians WHERE bioguide_id IN ({bio_placeholders})",
                bio_ids,
            ):
                name_by_bio[pr["bioguide_id"]] = pr["full_name"]

        for r in combo_rows:
            identity_key = r["bioguide_id"] or r["politician_name"]
            entry = politicians_by_ticker[r["ticker"]]
            if identity_key not in entry:
                entry[identity_key] = {
                    "bioguide_id": r["bioguide_id"],
                    "full_name": name_by_bio.get(r["bioguide_id"], r["politician_name"]),
                }

    results = []
    for r in rows:
        ticker = r["ticker"]
        entry = {
            "ticker": ticker,
            "company_name": company_name_by_ticker.get(ticker, ""),
            "politician_count": r["politician_count"],
            "trade_count": r["trade_count"],
            "total_value": r["total_value"] or 0,
        }
        if include_politicians:
            entry["politicians"] = sorted(
                politicians_by_ticker.get(ticker, {}).values(), key=lambda p: p["full_name"] or ""
            )
        results.append(entry)
    return results


@app.route("/api/stocks/sorted")
def stocks_sorted_by_politician_count():
    """Full list of stocks in the dataset, sorted from most to least widely
    held (see sort_stocks_by_politician_count). Query params:
      - min_politicians (default 1): only include tickers held by at least
        this many distinct politicians -- e.g. ?min_politicians=2 to match
        the "owned by more than one politician" framing of /api/stocks.
      - limit (default 500)
      - include_politicians (default true): set to false for a lighter
        response when the per-stock politician list isn't needed.
    """
    try:
        min_politicians = max(1, int(request.args.get("min_politicians", 1)))
    except ValueError:
        min_politicians = 1
    try:
        limit = max(1, min(int(request.args.get("limit", 500)), 5000))
    except ValueError:
        limit = 500
    include_politicians = request.args.get("include_politicians", "true").lower() != "false"

    with db.get_conn() as conn:
        results = sort_stocks_by_politician_count(
            conn, min_politicians=min_politicians, include_politicians=include_politicians
        )
    return jsonify(results[:limit])


@app.route("/api/stocks")
def list_stocks():
    """Returns tickers traded by more than one distinct politician, most
    widely-held first. This is disclosure-based, not verified current
    holdings."""
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ticker,
                   COUNT(DISTINCT COALESCE(bioguide_id, politician_name)) as politician_count,
                   COUNT(*) as trade_count,
                   SUM((COALESCE(amount_min,0)+COALESCE(amount_max,0))/2.0) as total_volume
            FROM trades
            WHERE ticker IS NOT NULL AND ticker != ''
            GROUP BY ticker
            HAVING politician_count > 1
            ORDER BY politician_count DESC, total_volume DESC
            LIMIT 500
            """
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/stocks/<ticker>")
def get_stock(ticker):
    ticker = ticker.upper()
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trades WHERE ticker = ? ORDER BY transaction_date DESC
            """,
            (ticker,),
        ).fetchall()
        trades = [_trade_row_to_dict(r) for r in rows]
        _annotate_committee_conflicts(conn, trades)
        _annotate_realized_pnl(conn, trades)

        bioguide_ids = list({t["bioguide_id"] for t in trades if t["bioguide_id"]})
        politicians_by_id = {}
        if bioguide_ids:
            placeholders = ",".join("?" for _ in bioguide_ids)
            for row in conn.execute(
                f"SELECT * FROM politicians WHERE bioguide_id IN ({placeholders})",
                bioguide_ids,
            ):
                politicians_by_id[row["bioguide_id"]] = dict(row)

    politician_summary = defaultdict(
        lambda: {"buy_count": 0, "sell_count": 0, "total_volume": 0.0}
    )
    for t in trades:
        key = t["bioguide_id"] or t["politician_name"]
        summary = politician_summary[key]
        mid = _amount_midpoint(t)
        summary["total_volume"] += mid
        if t["transaction_type"] == "purchase":
            summary["buy_count"] += 1
        elif t["transaction_type"] in ("sale", "sale_partial"):
            summary["sell_count"] += 1
        if not summary.get("politician"):
            summary["politician"] = politicians_by_id.get(t["bioguide_id"]) or {
                "full_name": t["politician_name"],
                "bioguide_id": t["bioguide_id"],
            }

    asset_descriptions = [t["asset_description"] for t in trades if t.get("asset_description")]
    company_name = Counter(asset_descriptions).most_common(1)[0][0] if asset_descriptions else ""

    return jsonify(
        {
            "ticker": ticker,
            "company_name": company_name,
            "trade_count": len(trades),
            "trades": trades,
            "politicians": list(politician_summary.values()),
            "disclaimer": (
                "Based on public disclosure filings only; does not represent "
                "verified current holdings."
            ),
        }
    )


@app.route("/api/stocks/<ticker>/volume")
def get_stock_volume(ticker):
    ticker = ticker.upper()
    range_key = request.args.get("range", "1y")
    start = _range_start_date(range_key)

    with db.get_conn() as conn:
        trades = conn.execute(
            """
            SELECT transaction_date, transaction_type, amount_min, amount_max
            FROM trades
            WHERE ticker = ? AND transaction_date >= ?
            ORDER BY transaction_date ASC
            """,
            (ticker, start.isoformat()),
        ).fetchall()

    buckets = defaultdict(lambda: {"buy": 0.0, "sell": 0.0})
    for t in trades:
        date_str = t["transaction_date"]
        if not date_str or len(date_str) < 7:
            continue
        month_key = date_str[:7]  # YYYY-MM
        mid = _amount_midpoint(t)
        if t["transaction_type"] == "purchase":
            buckets[month_key]["buy"] += mid
        elif t["transaction_type"] in ("sale", "sale_partial"):
            buckets[month_key]["sell"] += mid

    labels = sorted(buckets.keys())
    return jsonify(
        {
            "ticker": ticker,
            "range": range_key,
            "labels": labels,
            "buy": [buckets[l]["buy"] for l in labels],
            "sell": [buckets[l]["sell"] for l in labels],
        }
    )


# ---------------------------------------------------------------------------
# Politician-level volume chart (their trading activity over time)
# ---------------------------------------------------------------------------

@app.route("/api/politicians/<bioguide_id>/volume")
def get_politician_volume(bioguide_id):
    range_key = request.args.get("range", "1y")
    start = _range_start_date(range_key)

    with db.get_conn() as conn:
        trades = conn.execute(
            """
            SELECT transaction_date, transaction_type, amount_min, amount_max
            FROM trades
            WHERE bioguide_id = ? AND transaction_date >= ?
            ORDER BY transaction_date ASC
            """,
            (bioguide_id, start.isoformat()),
        ).fetchall()

    buckets = defaultdict(lambda: {"buy": 0.0, "sell": 0.0})
    for t in trades:
        date_str = t["transaction_date"]
        if not date_str or len(date_str) < 7:
            continue
        month_key = date_str[:7]
        mid = _amount_midpoint(t)
        if t["transaction_type"] == "purchase":
            buckets[month_key]["buy"] += mid
        elif t["transaction_type"] in ("sale", "sale_partial"):
            buckets[month_key]["sell"] += mid

    labels = sorted(buckets.keys())
    return jsonify(
        {
            "bioguide_id": bioguide_id,
            "range": range_key,
            "labels": labels,
            "buy": [buckets[l]["buy"] for l in labels],
            "sell": [buckets[l]["sell"] for l in labels],
        }
    )


# ---------------------------------------------------------------------------
# Combined Politician Portfolio Performance
#
# IMPORTANT HONESTY NOTE: disclosure filings report dollar *ranges* per
# transaction, never share counts or real-time holdings -- there is no way
# to compute a true, verified market value of what politicians currently
# hold without both (a) knowing exact share counts and (b) historical price
# data for every ticker ever traded. Neither is available from public,
# key-less sources. So "combined portfolio value" here means **estimated
# net invested capital**: a running total of (disclosed purchase amounts -
# disclosed sale amounts, using the midpoint of each reported range) across
# all tracked politicians. It's a faithful, fully-computable proxy for
# "are politicians collectively putting more or less money into the
# market", clearly labeled as such in the UI -- not a claim about actual
# portfolio market value.
# ---------------------------------------------------------------------------

PORTFOLIO_RANGES = ("1m", "3m", "6m", "ytd", "1y", "5y")
MAX_COMPARISON_POLITICIANS = 6


def _portfolio_granularity(range_key):
    """Daily granularity for anything up to a year; weekly for 5y, to keep
    the response (and the S&P 500/NASDAQ alignment) a reasonable size --
    matches the interval Yahoo itself uses for those ranges (see
    market_data.RANGE_TO_YAHOO)."""
    return 7 if range_key == "5y" else 1


def _daily_delta_map(conn, bioguide_id=None):
    """Returns {transaction_date: net $ delta} across all trades (or just
    one politician's, if bioguide_id is given), using the midpoint of each
    disclosed amount range. Purchases are positive, sales negative;
    exchanges are excluded (their direction/value isn't well-defined)."""
    query = (
        "SELECT transaction_date, transaction_type, amount_min, amount_max "
        "FROM trades WHERE transaction_date IS NOT NULL AND transaction_date != ''"
    )
    params = []
    if bioguide_id:
        query += " AND bioguide_id = ?"
        params.append(bioguide_id)

    deltas = defaultdict(float)
    for row in conn.execute(query, params):
        mid = _amount_midpoint(row)
        if row["transaction_type"] == "purchase":
            deltas[row["transaction_date"]] += mid
        elif row["transaction_type"] in ("sale", "sale_partial"):
            deltas[row["transaction_date"]] -= mid
    return deltas


def _cumulative_by_date(deltas, earliest, today):
    """Returns {date_str: running_cumulative_total} for every calendar day
    from `earliest` through `today` inclusive -- a true running total, so
    slicing out any later sub-range still reflects the correct cumulative
    value rather than resetting to zero."""
    cumulative = 0.0
    by_date = {}
    d = earliest
    one_day = timedelta(days=1)
    while d <= today:
        ds = d.isoformat()
        if ds in deltas:
            cumulative += deltas[ds]
        by_date[ds] = cumulative
        d += one_day
    return by_date


def _slice_series(cumulative_by_date, range_start, today, step_days):
    labels = []
    values = []
    d = range_start
    one_step = timedelta(days=step_days)
    while d <= today:
        ds = d.isoformat()
        labels.append(ds)
        values.append(round(cumulative_by_date.get(ds, 0.0), 2))
        d += one_step
    return labels, values


def _to_pct_change(values):
    if not values:
        return []
    base = values[0]
    if not base:
        return [0.0 for _ in values]
    return [round(((v - base) / abs(base)) * 100, 2) for v in values]


def _align_index_series(index_series, labels):
    """Aligns a sparse {date: price} index series (trading days only) onto
    `labels` (every calendar day in the visible range), forward-filling
    weekends/holidays with the last known price so it can share an x-axis
    with our daily/weekly portfolio series."""
    if not index_series:
        return None
    sorted_dates = sorted(index_series.keys())
    aligned = []
    last_value = None
    idx = 0
    for label in labels:
        while idx < len(sorted_dates) and sorted_dates[idx] <= label:
            last_value = index_series[sorted_dates[idx]]
            idx += 1
        aligned.append(last_value)
    first_real = next((v for v in aligned if v is not None), None)
    aligned = [v if v is not None else first_real for v in aligned]
    if all(v is None for v in aligned):
        return None
    return aligned


@app.route("/api/portfolio/performance")
def portfolio_performance():
    range_key = request.args.get("range", "1y")
    if range_key not in PORTFOLIO_RANGES:
        range_key = "1y"
    bioguide_ids = request.args.getlist("bioguide_id")[:MAX_COMPARISON_POLITICIANS]

    today = datetime.now(timezone.utc).date()
    range_start = _range_start_date(range_key)
    step_days = _portfolio_granularity(range_key)

    with db.get_conn() as conn:
        combined_deltas = _daily_delta_map(conn)
        earliest_row = conn.execute(
            "SELECT MIN(transaction_date) as d FROM trades WHERE transaction_date IS NOT NULL AND transaction_date != ''"
        ).fetchone()
        earliest_str = earliest_row["d"] if earliest_row and earliest_row["d"] else today.isoformat()
        earliest = datetime.strptime(earliest_str, "%Y-%m-%d").date()
        series_start = min(earliest, range_start)

        combined_cumulative = _cumulative_by_date(combined_deltas, series_start, today)
        labels, combined_values = _slice_series(combined_cumulative, range_start, today, step_days)
        combined_pct = _to_pct_change(combined_values)
        trend = "flat"
        if len(combined_values) >= 2:
            trend = "up" if combined_values[-1] >= combined_values[0] else "down"

        politicians = {}
        if bioguide_ids:
            name_rows = conn.execute(
                f"SELECT bioguide_id, full_name FROM politicians WHERE bioguide_id IN "
                f"({','.join('?' for _ in bioguide_ids)})",
                bioguide_ids,
            ).fetchall()
            names_by_id = {r["bioguide_id"]: r["full_name"] for r in name_rows}

            for bio in bioguide_ids:
                p_deltas = _daily_delta_map(conn, bioguide_id=bio)
                p_cumulative = _cumulative_by_date(p_deltas, series_start, today)
                _, p_values = _slice_series(p_cumulative, range_start, today, step_days)
                politicians[bio] = {
                    "name": names_by_id.get(bio, bio),
                    "value": p_values,
                    "pct_change": _to_pct_change(p_values),
                }

    benchmarks = {}
    for key in ("sp500", "nasdaq"):
        raw_series = get_index_series(key, range_key)
        aligned = _align_index_series(raw_series, labels) if raw_series else None
        if aligned:
            benchmarks[key] = {"pct_change": _to_pct_change(aligned)}

    return jsonify(
        {
            "range": range_key,
            "labels": labels,
            "combined": {
                "value": combined_values,
                "pct_change": combined_pct,
                "trend": trend,
            },
            "benchmarks": benchmarks,
            "politicians": politicians,
            "disclaimer": (
                "Estimated net invested capital based on disclosed transaction amounts "
                "(purchases minus sales, using the midpoint of each reported range) -- "
                "not a verified real-time market valuation of current holdings."
            ),
        }
    )


# ---------------------------------------------------------------------------
# Note: the "News" button is now handled entirely client-side (it opens the
# ticker's Yahoo Finance News page directly in the browser -- see
# frontend/app.js's callNews()), since the app runs in a real browser tab
# now rather than a native window. No backend route is needed for it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Notification preferences (per politician + stock "Notify" button)
#
# This only stores the user's saved preference (direction + amount
# threshold). No alert-sending/trigger logic is implemented yet -- nothing
# reads this table to actually watch for matching trades and fire a
# notification.
# ---------------------------------------------------------------------------

VALID_NOTIFICATION_DIRECTIONS = {"buy", "sell", "either"}
VALID_NOTIFICATION_COMPARISONS = {"above", "below"}


def _politician_key(bioguide_id, politician_name):
    """Key used to identify "the same politician" for a notification
    preference. Prefers bioguide_id (stable across name variants), but
    falls back to the disclosed name for trades that never resolved to a
    current/historical officeholder record."""
    return (bioguide_id or "").strip() or (politician_name or "").strip()


def _notification_pref_row_to_dict(row):
    return dict(row)


@app.route("/api/notification-preferences")
def list_notification_preferences():
    """Returns all saved notification preferences, or those matching
    ?bioguide_id=&politician_name=&ticker= if provided (used by the
    frontend to check whether a given trade row already has one saved, so
    the Notify button/modal can reflect existing state)."""
    bioguide_id = request.args.get("bioguide_id")
    politician_name = request.args.get("politician_name")
    ticker = request.args.get("ticker")

    with db.get_conn() as conn:
        if ticker and (bioguide_id or politician_name):
            key = _politician_key(bioguide_id, politician_name)
            row = conn.execute(
                "SELECT * FROM notification_preferences WHERE politician_key = ? AND ticker = ?",
                (key, ticker.upper()),
            ).fetchone()
            return jsonify([_notification_pref_row_to_dict(row)] if row else [])

        rows = conn.execute(
            "SELECT * FROM notification_preferences ORDER BY updated_at DESC"
        ).fetchall()
        return jsonify([_notification_pref_row_to_dict(r) for r in rows])


@app.route("/api/notification-preferences", methods=["POST"])
def save_notification_preference():
    """Creates or updates (upsert, keyed on politician + ticker) a saved
    notification preference. Body: bioguide_id (optional), politician_name,
    ticker, direction ('buy'/'sell'/'either'), amount_comparison
    ('above'/'below'), amount_threshold (number >= 0)."""
    body = request.get_json(force=True, silent=True) or {}

    politician_name = (body.get("politician_name") or "").strip()
    bioguide_id = (body.get("bioguide_id") or "").strip() or None
    ticker = (body.get("ticker") or "").strip().upper()
    direction = (body.get("direction") or "either").strip().lower()
    amount_comparison = (body.get("amount_comparison") or "above").strip().lower()
    amount_threshold = body.get("amount_threshold", 0)

    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    if not politician_name:
        return jsonify({"error": "politician_name is required"}), 400
    if direction not in VALID_NOTIFICATION_DIRECTIONS:
        return jsonify({"error": f"direction must be one of {sorted(VALID_NOTIFICATION_DIRECTIONS)}"}), 400
    if amount_comparison not in VALID_NOTIFICATION_COMPARISONS:
        return jsonify({"error": f"amount_comparison must be one of {sorted(VALID_NOTIFICATION_COMPARISONS)}"}), 400
    try:
        amount_threshold = max(0.0, float(amount_threshold))
    except (TypeError, ValueError):
        return jsonify({"error": "amount_threshold must be a number"}), 400

    key = _politician_key(bioguide_id, politician_name)
    now = datetime.now(timezone.utc).isoformat()

    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO notification_preferences (
                politician_key, bioguide_id, politician_name, ticker,
                direction, amount_comparison, amount_threshold,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(politician_key, ticker) DO UPDATE SET
                bioguide_id = excluded.bioguide_id,
                politician_name = excluded.politician_name,
                direction = excluded.direction,
                amount_comparison = excluded.amount_comparison,
                amount_threshold = excluded.amount_threshold,
                updated_at = excluded.updated_at
            """,
            (key, bioguide_id, politician_name, ticker, direction, amount_comparison, amount_threshold, now, now),
        )
        row = conn.execute(
            "SELECT * FROM notification_preferences WHERE politician_key = ? AND ticker = ?",
            (key, ticker),
        ).fetchone()
        _generate_notifications_for_preference(conn, row)

    return jsonify(_notification_pref_row_to_dict(row))


@app.route("/api/notification-preferences/<int:pref_id>", methods=["DELETE"])
def delete_notification_preference(pref_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM notification_preferences WHERE id = ?", (pref_id,))
        conn.execute("DELETE FROM notifications WHERE preference_id = ?", (pref_id,))
    return jsonify({"status": "deleted", "id": pref_id})


# ---------------------------------------------------------------------------
# Notifications (trades that matched a saved notification_preference)
#
# _generate_all_notifications() scans every saved preference against the
# trades table and records a notifications row for each matching trade it
# hasn't already recorded (idempotent thanks to the UNIQUE(preference_id,
# trade_id) constraint). It's run once at startup and again after every
# completed refresh (see create_app / _start_refresh_job), and also right
# after a preference is saved so a match against already-loaded trades
# shows up immediately rather than waiting for the next refresh.
# ---------------------------------------------------------------------------

def _notification_direction_matches(direction, transaction_type):
    if direction == "either":
        return True
    if direction == "buy":
        return transaction_type == "purchase"
    if direction == "sell":
        return transaction_type in ("sale", "sale_partial")
    return False


def _notification_amount_matches(amount_comparison, amount_threshold, trade_row):
    mid = _amount_midpoint(trade_row)
    if amount_comparison == "above":
        return mid >= amount_threshold
    return mid <= amount_threshold


def _generate_notifications_for_preference(conn, pref):
    """Finds trades matching a single saved preference and inserts a
    notifications row for any that don't already have one. Returns the
    number of newly-created notifications."""
    bioguide_id = pref["bioguide_id"]
    politician_name = pref["politician_name"]
    ticker = pref["ticker"]

    if bioguide_id:
        trades = conn.execute(
            "SELECT * FROM trades WHERE bioguide_id = ? AND ticker = ?",
            (bioguide_id, ticker),
        ).fetchall()
    else:
        trades = conn.execute(
            "SELECT * FROM trades WHERE bioguide_id IS NULL AND politician_name = ? AND ticker = ?",
            (politician_name, ticker),
        ).fetchall()

    trade_dicts = [_trade_row_to_dict(t) for t in trades]
    _annotate_realized_pnl(conn, trade_dicts)
    pnl_by_trade_id = {t["id"]: t for t in trade_dicts}

    created = 0
    now = datetime.now(timezone.utc).isoformat()
    for t in trades:
        if not _notification_direction_matches(pref["direction"], t["transaction_type"]):
            continue
        if not _notification_amount_matches(pref["amount_comparison"], pref["amount_threshold"], t):
            continue

        verb = {"purchase": "bought", "sale": "sold", "sale_partial": "partially sold"}.get(
            t["transaction_type"], "traded"
        )
        message = f"{politician_name} {verb} {ticker}" + (
            f" ({t['amount_range']})" if t["amount_range"] else ""
        )

        pnl = pnl_by_trade_id.get(t["id"], {})
        profit_loss = pnl.get("profit_loss")
        profit_loss_pct = pnl.get("profit_loss_pct")
        if profit_loss is not None:
            verdict = "profit" if profit_loss >= 0 else "loss"
            message += f" -- est. {verdict} of {abs(profit_loss):,.0f} ({abs(profit_loss_pct):.1f}%)"

        cur = conn.execute(
            """
            INSERT INTO notifications (
                preference_id, trade_id, bioguide_id, politician_name, ticker,
                transaction_type, amount_range, transaction_date, message,
                profit_loss, profit_loss_pct, is_read, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(preference_id, trade_id) DO NOTHING
            """,
            (
                pref["id"],
                t["id"],
                bioguide_id,
                politician_name,
                ticker,
                t["transaction_type"],
                t["amount_range"],
                t["transaction_date"],
                message,
                profit_loss,
                profit_loss_pct,
                now,
            ),
        )
        if cur.rowcount:
            created += 1
    return created


def _generate_all_notifications():
    """Scans every saved notification preference against the trades table.
    Safe to call any number of times (e.g. app startup, after every refresh,
    after saving a preference) -- already-recorded matches are never
    duplicated."""
    with db.get_conn() as conn:
        prefs = conn.execute("SELECT * FROM notification_preferences").fetchall()
        total = 0
        for pref in prefs:
            total += _generate_notifications_for_preference(conn, pref)
        return total


def _notification_row_to_dict(row):
    return dict(row)


@app.route("/api/notifications")
def list_notifications():
    """Returns all generated notifications, most recent first, for the
    header bell dropdown."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return jsonify([_notification_row_to_dict(r) for r in rows])


@app.route("/api/notifications/unread_count")
def notifications_unread_count():
    with db.get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE is_read = 0"
        ).fetchone()["c"]
    return jsonify({"unread_count": count})


@app.route("/api/notifications/mark_read", methods=["POST"])
def mark_notifications_read():
    """Marks all notifications as read (called when the notifications
    dropdown is closed, e.g. by clicking outside of it), clearing the
    header bell's unread badge/count."""
    with db.get_conn() as conn:
        conn.execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Refresh / meta
# ---------------------------------------------------------------------------

@app.route("/api/meta")
def get_meta():
    with db.get_conn() as conn:
        total_politicians = conn.execute("SELECT COUNT(*) c FROM politicians").fetchone()["c"]
        total_trades = conn.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
    return jsonify(
        {
            "last_updated": db.get_meta("last_updated"),
            "total_politicians": total_politicians,
            "total_trades": total_trades,
            "refreshing": _refresh_status["running"],
            "last_message": _refresh_status["last_message"],
            "legislator_source": db.get_meta("legislator_source"),
            "app_version": APP_VERSION,
            # Lets the UI's custom start-date picker (next to "Refresh Data")
            # default to, and not go more recent than, the normal 12-month
            # window, while bounding how far back a custom date can go to the
            # overall retention cap (see data_fetch.py's TRADE_HISTORY_YEARS /
            # DEFAULT_REFRESH_LOOKBACK_DAYS).
            "default_refresh_since_date": default_refresh_cutoff(),
            "min_refresh_since_date": min_refresh_since_date(),
        }
    )


# ---------------------------------------------------------------------------
# Data collection pipeline status (House Clerk / Senate eFD sources, parse
# failures, format-version alerts, stale-source alerts -- see backend/
# pipeline/monitoring.py). Read-only; the pipeline itself only runs as part
# of refresh_data (see data_fetch.py).
# ---------------------------------------------------------------------------

@app.route("/api/pipeline/status")
def pipeline_status():
    from .pipeline import monitoring as pipeline_monitoring

    sources = pipeline_monitoring.all_source_status()
    stale = []
    for row in sources:
        is_stale, _ = pipeline_monitoring.is_source_stale(row["source"])
        if is_stale:
            stale.append(row["source"])
    return jsonify(
        {
            "sources": sources,
            "stale_sources": stale,
            "recent_events": pipeline_monitoring.recent_events(limit=50),
        }
    )


# ---------------------------------------------------------------------------
# App version / update check (see backend/version.py, backend/update_check.py)
#
# Entirely read-only and best-effort: this only checks GitHub's public
# Releases API for a newer published version than the one currently
# running, and never downloads or installs anything itself -- the
# "Update Available" button in Settings just opens the releases page in
# the user's browser, same as the user could do manually.
# ---------------------------------------------------------------------------

@app.route("/api/version/check")
def version_check():
    return jsonify(check_for_update())


# ---------------------------------------------------------------------------
# Settings (optional Congress.gov / api.data.gov API key)
# ---------------------------------------------------------------------------

@app.route("/api/settings")
def get_settings():
    """Never returns the full API key -- only whether one is configured and a
    masked preview, so the frontend can display status without exposing the
    secret back over (loopback-only, but still) HTTP."""
    try:
        # The Lite build never bundles this module at all (it hard-imports
        # pdfplumber itself, in turn excluded -- see packaging/windows_lite.spec),
        # so OCR is simply never available there, same as pipeline_available()
        # being False for the same underlying reason.
        from .pipeline import ocr as pipeline_ocr

        ocr_available = pipeline_ocr.is_available()
    except ImportError:
        ocr_available = False

    key = get_congress_api_key()
    return jsonify(
        {
            "congress_gov_api_key_set": bool(key),
            "congress_gov_api_key_masked": mask_key(key),
            "legislator_source": db.get_meta("legislator_source"),
            "auto_refresh_minutes": get_auto_refresh_minutes(),
            "ocr_available": ocr_available,
        }
    )


@app.route("/api/settings", methods=["POST"])
def update_settings():
    body = request.get_json(force=True, silent=True) or {}
    updates = {}
    response = {"status": "saved"}

    if "congress_gov_api_key" in body:
        new_key = (body.get("congress_gov_api_key") or "").strip()
        updates["congress_gov_api_key"] = new_key
        response["congress_gov_api_key_set"] = bool(new_key)

    if "auto_refresh_minutes" in body:
        try:
            minutes = max(0, int(body.get("auto_refresh_minutes")))
        except (TypeError, ValueError):
            return jsonify({"error": "auto_refresh_minutes must be an integer"}), 400
        updates["auto_refresh_minutes"] = minutes
        response["auto_refresh_minutes"] = minutes

    if not updates:
        return jsonify({"error": "no recognized settings provided"}), 400

    save_settings(updates)
    return jsonify(response)


@app.route("/api/settings/congress_gov_api_key", methods=["DELETE"])
def clear_congress_api_key():
    save_settings({"congress_gov_api_key": ""})
    return jsonify({"status": "cleared"})


# ---------------------------------------------------------------------------
# Trade disclosure data source registry (APIs panel)
#
# NOTE: This only lets the user record which sources are enabled (and
# register custom sources with an endpoint URL / API key) -- it does not
# fetch or parse data from any of them yet. See backend/settings.py for
# notes on the intended resilient multi-source architecture.
# ---------------------------------------------------------------------------

@app.route("/api/sources")
def list_sources():
    enabled_ids = set(get_enabled_source_ids())
    builtin = [
        {**source, "enabled": source["id"] in enabled_ids, "builtin": True}
        for source in DEFAULT_API_SOURCES
    ]
    custom = [
        {**source, "builtin": False}
        for source in mask_custom_sources_for_display(get_custom_api_sources())
    ]
    return jsonify({"builtin_sources": builtin, "custom_sources": custom})


@app.route("/api/sources/<source_id>", methods=["PATCH"])
def update_source(source_id):
    body = request.get_json(force=True, silent=True) or {}
    if "enabled" not in body:
        return jsonify({"error": "enabled is required"}), 400
    enabled = bool(body.get("enabled"))

    builtin_ids = {s["id"] for s in DEFAULT_API_SOURCES}
    if source_id in builtin_ids:
        current = set(get_enabled_source_ids())
        if enabled:
            current.add(source_id)
        else:
            current.discard(source_id)
        set_enabled_source_ids(current)
    else:
        custom_ids = {s["id"] for s in get_custom_api_sources()}
        if source_id not in custom_ids:
            return jsonify({"error": "unknown source id"}), 404
        set_custom_api_source_enabled(source_id, enabled)

    return jsonify({"status": "saved", "id": source_id, "enabled": enabled})


@app.route("/api/sources/custom", methods=["POST"])
def create_custom_source():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    endpoint_url = (body.get("endpoint_url") or "").strip()
    api_key = (body.get("api_key") or "").strip()

    if not name or not endpoint_url:
        return jsonify({"error": "name and endpoint_url are required"}), 400

    source = add_custom_api_source(name, endpoint_url, api_key)
    return jsonify(mask_custom_sources_for_display([source])[0])


@app.route("/api/sources/custom/<source_id>", methods=["DELETE"])
def delete_custom_source(source_id):
    remove_custom_api_source(source_id)
    return jsonify({"status": "deleted", "id": source_id})


@app.route("/api/refresh", methods=["POST"])
def trigger_refresh():
    """Kicks off a manual refresh. Accepts an optional JSON body with a
    `since_date` (ISO 'YYYY-MM-DD') to override the default 12-month trade
    lookback window with a custom, further-back start date -- see the date
    picker next to the 'Refresh Data' button in the UI. Invalid or missing
    values simply fall back to the default 12-month window."""
    body = request.get_json(force=True, silent=True) or {}
    since_date = (body.get("since_date") or "").strip() or None
    if not _start_refresh_job(since_date=since_date):
        return jsonify({"status": "already_running"}), 409
    return jsonify({"status": "started"})


@app.route("/api/refresh/status")
def refresh_status():
    progress = _refresh_tracker.snapshot() if _refresh_tracker else None
    return jsonify(
        {
            "refreshing": _refresh_status["running"],
            "stopping": _refresh_status.get("stopping", False),
            "last_message": _refresh_status.get("last_message"),
            "progress": progress,
        }
    )


@app.route("/api/refresh/stop", methods=["POST"])
def stop_refresh():
    """Requests that the currently-running refresh stop as soon as possible
    -- see the Refresh Data dropdown's "Stop Refresh" item. No-ops (with a
    clear status) if nothing is currently running. Whatever was already
    loaded before the stop takes effect is kept."""
    if not _stop_refresh_job():
        return jsonify({"status": "not_running"}), 409
    return jsonify({"status": "stopping"})


@app.route("/api/data/clear", methods=["POST"])
def clear_all_data():
    """Wipes all downloaded/derived data (politicians, committees, trades,
    notifications, and pipeline bookkeeping/caches) -- see the Refresh Data
    dropdown's "Clear All Data" button. Refuses to run while a refresh is in
    progress (stop it first) to avoid racing with in-flight database
    writes. Settings (API keys, custom sources, auto-refresh interval) are
    left untouched."""
    if _refresh_status["running"]:
        return jsonify({"error": "Stop the current refresh before clearing data"}), 409
    db.clear_all_data()
    return jsonify({"status": "cleared"})


@app.route("/api/server/restart", methods=["POST"])
def restart_server():
    # Both this and shutdown_server() below end with os._exit(0) (see
    # launcher.request_restart/request_shutdown), which on Android would
    # kill the whole app process, not just the Python server -- there's no
    # separate OS process to relaunch. Refuse instead of doing that.
    if os.environ.get("POLITICIAN_TRADES_ANDROID"):
        return jsonify({"error": "Not supported on Android"}), 404

    from .launcher import request_restart

    request_restart()
    return jsonify({"status": "restarting"})


@app.route("/api/server/shutdown", methods=["POST"])
def shutdown_server():
    if os.environ.get("POLITICIAN_TRADES_ANDROID"):
        return jsonify({"error": "Not supported on Android"}), 404

    from .launcher import request_shutdown

    request_shutdown()
    return jsonify({"status": "shutting_down"})


@app.route("/api/filters/options")
def filter_options():
    with db.get_conn() as conn:
        parties = [r["party"] for r in conn.execute(
            "SELECT DISTINCT party FROM politicians WHERE party IS NOT NULL ORDER BY party"
        )]
        states = [r["state"] for r in conn.execute(
            "SELECT DISTINCT state FROM politicians WHERE state IS NOT NULL ORDER BY state"
        )]
    return jsonify({"parties": parties, "states": states, "chambers": ["rep", "sen"]})


# ---------------------------------------------------------------------------
# Global header search (politicians + stocks)
# ---------------------------------------------------------------------------

SEARCH_RESULT_LIMIT = 8


@app.route("/api/search")
def global_search():
    """Powers the header search box: matches both politicians (by name) and
    stocks (by ticker or company/asset name) for the same query, so the box
    isn't scoped to only one kind of result. Returns up to
    SEARCH_RESULT_LIMIT of each, cheapest/most-relevant first (exact ticker
    match pinned to the top of the stocks list). Empty/missing `q` returns
    both lists empty rather than erroring or returning everything."""
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"query": query, "politicians": [], "stocks": []})

    like = f"%{query}%"
    with db.get_conn() as conn:
        politician_rows = conn.execute(
            """
            SELECT bioguide_id, full_name, party, state, chamber, photo_url
            FROM politicians
            WHERE full_name LIKE ?
            ORDER BY full_name
            LIMIT ?
            """,
            (like, SEARCH_RESULT_LIMIT),
        ).fetchall()

        # Matches a ticker prefix (e.g. "AAP" -> AAPL) or a substring anywhere
        # in the most commonly disclosed asset description for that ticker
        # (e.g. "apple" -> AAPL's "Apple Inc" description). Exact ticker
        # matches are ranked first, then ticker-prefix matches, then
        # description matches -- all secondarily ordered by how widely held
        # the stock is, so the most relevant/well-known match comes first.
        stock_rows = conn.execute(
            """
            SELECT ticker,
                   COUNT(DISTINCT COALESCE(bioguide_id, politician_name)) as politician_count,
                   COUNT(*) as trade_count
            FROM trades
            WHERE ticker IS NOT NULL AND ticker != ''
              AND (ticker LIKE ? OR asset_description LIKE ?)
            GROUP BY ticker
            ORDER BY
                CASE WHEN ticker = ? THEN 0 WHEN ticker LIKE ? THEN 1 ELSE 2 END,
                politician_count DESC,
                trade_count DESC
            LIMIT ?
            """,
            (like, like, query.upper(), f"{query.upper()}%", SEARCH_RESULT_LIMIT),
        ).fetchall()

    tickers = [r["ticker"] for r in stock_rows]
    company_name_by_ticker = {}
    if tickers:
        with db.get_conn() as conn:
            placeholders = ",".join("?" for _ in tickers)
            name_rows = conn.execute(
                f"""
                SELECT ticker, asset_description, COUNT(*) as c
                FROM trades
                WHERE ticker IN ({placeholders}) AND asset_description IS NOT NULL AND asset_description != ''
                GROUP BY ticker, asset_description
                ORDER BY ticker, c DESC
                """,
                tickers,
            ).fetchall()
            for r in name_rows:
                company_name_by_ticker.setdefault(r["ticker"], r["asset_description"])

    return jsonify(
        {
            "query": query,
            "politicians": [dict(r) for r in politician_rows],
            "stocks": [
                {
                    "ticker": r["ticker"],
                    "company_name": company_name_by_ticker.get(r["ticker"], ""),
                    "politician_count": r["politician_count"],
                    "trade_count": r["trade_count"],
                }
                for r in stock_rows
            ],
        }
    )
