"""
SQLite database layer for the Politician Trades app.

The database is a single file stored next to the executable (or in the
project folder during development) so the whole app is portable and
requires no external database server.
"""

import os
import sqlite3
from contextlib import contextmanager

# Where the .db file lives. When packaged with PyInstaller we want it to sit
# next to the executable so users can see/back up their data; during normal
# `python run.py` development it just lives in the project's data/ folder.
def get_data_dir():
    import sys

    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        # On macOS the frozen executable lives inside
        # PoliticianTradesTracker.app/Contents/MacOS/, several levels deep
        # inside the (normally opaque) app bundle. Walk up to the folder
        # containing the .app itself so data/ is a plain, visible sibling
        # folder there instead -- matching the Windows/Linux folder builds,
        # where data/ sits directly next to the visible executable.
        macos_contents_dir = os.path.join("Contents", "MacOS")
        if base.replace("/", os.sep).endswith(macos_contents_dir):
            app_bundle_dir = os.path.dirname(os.path.dirname(base))
            if app_bundle_dir.endswith(".app"):
                base = os.path.dirname(app_bundle_dir)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(base, "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


DB_PATH = os.path.join(get_data_dir(), "politicians.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS politicians (
    bioguide_id     TEXT PRIMARY KEY,
    first_name      TEXT,
    last_name       TEXT,
    full_name       TEXT NOT NULL,
    party           TEXT,
    state           TEXT,
    chamber         TEXT,          -- 'rep' or 'sen'
    district        TEXT,
    photo_url       TEXT
);

CREATE TABLE IF NOT EXISTS committees (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thomas_id       TEXT UNIQUE,
    name            TEXT NOT NULL,
    chamber         TEXT,
    sectors         TEXT           -- comma separated tags, e.g. "Banking,Insurance"
);

CREATE TABLE IF NOT EXISTS politician_committees (
    bioguide_id     TEXT,
    committee_id    INTEGER,
    role            TEXT,
    PRIMARY KEY (bioguide_id, committee_id),
    FOREIGN KEY (bioguide_id) REFERENCES politicians(bioguide_id),
    FOREIGN KEY (committee_id) REFERENCES committees(id)
);

-- Note: bioguide_id here is intentionally NOT a strict foreign key. Trade
-- disclosures span back to ~2012 and are frequently attributed to former
-- members of Congress who are no longer in legislators-current.yaml (and are
-- therefore not rows in the politicians table). We still resolve/store their
-- bioguide_id (matched via legislators-historical.yaml) for accurate stock
-- grouping and dedup, but it may not join to a politicians row.
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    bioguide_id         TEXT,
    politician_name     TEXT NOT NULL,
    ticker              TEXT,
    asset_description   TEXT,
    transaction_type    TEXT,       -- 'purchase', 'sale', 'sale_partial', 'exchange'
    transaction_date    TEXT,       -- ISO yyyy-mm-dd
    disclosure_date     TEXT,
    amount_range        TEXT,
    amount_min          REAL,
    amount_max          REAL,
    chamber             TEXT,       -- 'house' or 'senate'
    source_url          TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_bioguide ON trades(bioguide_id);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(transaction_date);
CREATE INDEX IF NOT EXISTS idx_trades_disclosure_date ON trades(disclosure_date);
CREATE INDEX IF NOT EXISTS idx_trades_name ON trades(politician_name);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- Tracks politicians who have dropped out of the current legislator
-- directory (see data_fetch._mark_and_purge_departed_politicians), starting
-- a grace period before they're permanently purged. detected_at is set the
-- first time a politician is observed missing from the current legislators
-- list and is never reset by later refreshes (ON CONFLICT DO NOTHING) --
-- unless they reappear (e.g. re-elected) before the grace period elapses,
-- in which case their row here is deleted.
CREATE TABLE IF NOT EXISTS departed_politicians (
    bioguide_id     TEXT PRIMARY KEY,
    full_name       TEXT,
    detected_at     TEXT NOT NULL
);

-- Tracks the last-seen HTTP caching headers (and a content hash fallback)
-- for each remote data source URL, so refreshes can skip re-downloading
-- and re-processing data that hasn't actually changed since last time.
CREATE TABLE IF NOT EXISTS source_cache (
    source_key      TEXT PRIMARY KEY,
    url             TEXT,
    etag            TEXT,
    last_modified   TEXT,
    content_hash    TEXT,
    last_checked_at TEXT,
    last_changed_at TEXT
);

-- A user's saved "notify me" preference for one politician + one stock,
-- set from the per-row Notify button on a trades table. `politician_key`
-- is bioguide_id when known, otherwise politician_name (trades attributed
-- to former members often have no bioguide_id) -- this is what the
-- UNIQUE constraint is keyed on, so saving again for the same
-- politician + ticker updates the existing preference instead of
-- duplicating it. Multiple same-stock buy or sell disclosures filed for
-- the same politician at the same transaction timestamp are treated as a
-- single logical transaction for notification purposes, which falls out
-- naturally here since the preference is per politician + ticker, not per
-- individual trade row.
--
-- NOTE: this only stores the preference. No alert-sending/trigger logic
-- reads this table yet.
CREATE TABLE IF NOT EXISTS notification_preferences (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    politician_key      TEXT NOT NULL,
    bioguide_id         TEXT,
    politician_name     TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    direction           TEXT NOT NULL DEFAULT 'either',  -- 'buy', 'sell', or 'either'
    amount_comparison   TEXT NOT NULL DEFAULT 'above',   -- 'above' or 'below'
    amount_threshold    REAL NOT NULL DEFAULT 0,
    created_at          TEXT,
    updated_at          TEXT,
    UNIQUE (politician_key, ticker)
);

-- One row per trade that actually matched a saved notification_preference
-- (see app.py's _generate_notifications, run after every refresh and after
-- saving a preference). UNIQUE(preference_id, trade_id) makes generation
-- idempotent -- re-scanning the same preference/trade combination never
-- creates a duplicate notification. `is_read` drives the header bell's
-- unread badge/count; it's cleared in bulk when the notifications dropdown
-- is closed (see /api/notifications/mark_read).
CREATE TABLE IF NOT EXISTS notifications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    preference_id        INTEGER NOT NULL,
    trade_id             INTEGER NOT NULL,
    bioguide_id          TEXT,
    politician_name      TEXT NOT NULL,
    ticker               TEXT NOT NULL,
    transaction_type     TEXT,
    amount_range         TEXT,
    transaction_date     TEXT,
    message              TEXT NOT NULL,
    -- Estimated realized profit/loss for a sale notification (see app.py's
    -- _annotate_realized_pnl); NULL for buy notifications, or a sale with no
    -- prior disclosed purchase of that ticker by that politician to compare
    -- against.
    profit_loss          REAL,
    profit_loss_pct       REAL,
    is_read              INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT,
    UNIQUE (preference_id, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON notifications(is_read);

-- ---------------------------------------------------------------------------
-- Data collection pipeline bookkeeping (see backend/pipeline/).
--
-- processed_filings: one row per individual filing (a House PTR PDF, a
-- Senate eFD PTR report, etc.) that the pipeline has successfully or
-- unsuccessfully attempted to parse. UNIQUE(source, filing_id) plus a
-- content_hash comparison is what lets a re-run skip re-downloading/
-- re-parsing filings that have already been processed and haven't changed
-- upstream (a filing can occasionally be amended, which changes its hash
-- and triggers reprocessing).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS processed_filings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,      -- e.g. 'house_clerk', 'senate_efd'
    filing_id       TEXT NOT NULL,      -- DocID / report UUID from the source
    content_hash    TEXT,               -- sha256 of the raw filing bytes
    parser_version  TEXT,               -- parser/schema version that produced this result
    status          TEXT NOT NULL,      -- 'ok' | 'failed' | 'unrecognized_format'
    trade_count     INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    parsed_at       TEXT NOT NULL,
    UNIQUE (source, filing_id)
);

CREATE INDEX IF NOT EXISTS idx_processed_filings_source ON processed_filings(source);

-- pipeline_events: an append-only log of notable pipeline occurrences --
-- parsing failures, unrecognized/changed source formats, stale-data
-- warnings, and fallback-to-secondary-source events -- surfaced via
-- /api/pipeline/status in addition to the rotating log file on disk.
CREATE TABLE IF NOT EXISTS pipeline_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    source          TEXT NOT NULL,
    level           TEXT NOT NULL,      -- 'info' | 'warning' | 'error'
    event           TEXT NOT NULL,      -- short code, e.g. 'parse_failure', 'unrecognized_format', 'stale_data', 'fallback_used'
    message         TEXT,
    filing_id       TEXT
);

CREATE INDEX IF NOT EXISTS idx_pipeline_events_ts ON pipeline_events(ts);

-- pipeline_source_status: last attempt/success bookkeeping per logical data
-- source, used to detect and alert on stale data (a source that hasn't
-- succeeded in longer than its expected refresh cadence).
CREATE TABLE IF NOT EXISTS pipeline_source_status (
    source                  TEXT PRIMARY KEY,
    last_attempt_at         TEXT,
    last_success_at         TEXT,
    last_error              TEXT,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0
);
"""

# (data_source, external_id) is the upsert key the pipeline's loader.py uses
# to update an already-loaded filing's trade rows in place instead of
# duplicating them on a re-run. SQLite treats NULL != NULL in a UNIQUE
# index, so this never conflicts with the many pre-existing rows (loaded by
# the legacy data_fetch.py path) that have NULL data_source/external_id.
# Created as a migration (not inline in SCHEMA) since `trades` already
# existed before these two columns did.
_INDEX_MIGRATIONS = [
    (
        "idx_trades_source_external_id",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_source_external_id "
        "ON trades(data_source, external_id)",
    ),
]

# Columns added after the initial release of a table can't be expressed via
# `CREATE TABLE IF NOT EXISTS` (which only affects brand-new databases), so
# existing databases need an explicit, idempotent ALTER TABLE migration.
# Each entry is (table, column, column_type_and_default_sql).
_COLUMN_MIGRATIONS = [
    ("trades", "data_source", "TEXT"),   # which pipeline source produced this row, e.g. 'house_clerk'
    ("trades", "external_id", "TEXT"),  # source's own filing/report id, for traceability back to the original filing
    # Whether this row came through OCR/image-based parsing rather than a
    # filing's native embedded text (see pipeline/house_clerk.py's used_ocr
    # and pipeline/checkbox_form.py). Only OCR-sourced rows are ever
    # candidates for the "Unreadable -- See Records" display substitution
    # (see asset_quality.py) -- natively parsed text is essentially never
    # OCR gibberish, so gating on this avoids mislabeling a real but
    # unusual-looking native name (e.g. "WIX.COM LTD CMN", which the
    # garbled-text heuristic alone would false-positive on).
    ("trades", "ocr_sourced", "INTEGER DEFAULT 0"),
    # Set when a row's asset_description has been finalized by a human
    # review pass (see backend/asset_quality.py's clean_asset_name and the
    # Khanna asset-column cleanup) -- either a real company name identified
    # from otherwise-garbled OCR text, or an "Unreadable -- See Records.
    # P.x Rowy" pointer for text that couldn't be identified at all. Once
    # set, loader.upsert_trades preserves the existing asset_description on
    # every future re-parse of that filing instead of overwriting it with
    # fresh (and possibly differently-garbled) OCR output -- without this,
    # confirmed on a real refresh: the app's own hourly auto-refresh
    # silently reprocessed filings and reverted hundreds of manually
    # reviewed rows back to raw OCR garbage within hours of finishing the
    # review. purge_stale_filing_rows also exempts reviewed rows from its
    # stale-row cleanup for the same reason.
    ("trades", "asset_description_reviewed", "INTEGER DEFAULT 0"),
    # Estimated profit/loss for a sale notification, computed from this
    # politician's own disclosed trade history and historical market prices
    # (see app.py's _annotate_realized_pnl) -- NULL when the sale never
    # matched a prior disclosed purchase of that ticker by the same
    # politician to compare against.
    ("notifications", "profit_loss", "REAL"),
    ("notifications", "profit_loss_pct", "REAL"),
]


def _ensure_column(conn, table, column, coltype):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


@contextmanager
def get_conn():
    # timeout=30: how long a writer waits on "database is locked" before
    # giving up, rather than sqlite3's 5-second default. Matters once
    # multiple writers can be active at once -- the live app's own
    # auto-refresh thread plus any concurrent background reprocessing
    # script (see scripts/reprocess_stale_filings.py, deliberately run as
    # several parallel processes for the full-politician OCR review) --
    # where the default was tight enough to occasionally raise "database is
    # locked" on a perfectly normal, just-slightly-delayed write.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for table, column, coltype in _COLUMN_MIGRATIONS:
            _ensure_column(conn, table, column, coltype)
        for _name, create_sql in _INDEX_MIGRATIONS:
            conn.execute(create_sql)


def set_meta(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


# Tables wiped by clear_all_data() (Refresh Data dropdown's "Clear All
# Data" button). source_cache/processed_filings/pipeline_events/
# pipeline_source_status are included so a fresh full re-download actually
# happens on the next refresh, rather than every source/filing being
# skipped as "unchanged"/"already processed" against the now-empty trades
# table.
# Order matters: politician_committees has FK references to politicians and
# committees (and get_conn() runs with PRAGMA foreign_keys = ON), so it must
# be cleared first. notifications similarly references notification_preferences
# conceptually (not a strict FK, but cleared first to match) -- everything
# else here has no foreign key relationships.
_CLEAR_DATA_TABLES = (
    "politician_committees",
    "trades",
    "politicians",
    "committees",
    "notifications",
    "notification_preferences",
    "source_cache",
    "processed_filings",
    "pipeline_events",
    "pipeline_source_status",
    "departed_politicians",
)


def clear_all_data():
    """Wipes every downloaded/derived table (politicians, committees,
    trades, notifications, and all pipeline bookkeeping/caches), but keeps
    the schema and the settings.json file (API keys, custom sources,
    enabled sources, auto-refresh interval) untouched -- this is a data
    reset, not a settings reset. Also clears 'last_updated' and
    'last_refresh_summary' from meta so the UI correctly shows no data
    until the next refresh. Used by the Settings > Refresh Data dropdown's
    "Clear All Data" button; safe to call at any time (e.g. before a
    from-scratch refresh) since every table is recreated on demand by the
    normal refresh pipeline."""
    with get_conn() as conn:
        for table in _CLEAR_DATA_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM meta WHERE key IN ('last_updated', 'last_refresh_summary', 'legislator_source')")
