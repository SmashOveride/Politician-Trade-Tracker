"""
Logging and alerting for the data collection pipeline.

Two complementary layers:
1. A rotating log file (data/logs/pipeline.log) via the standard `logging`
   module -- a durable, greppable record of every parse failure, format
   mismatch, and fallback, including full tracebacks.
2. The pipeline_events / pipeline_source_status DB tables -- a compact,
   queryable summary surfaced via /api/pipeline/status in the app, so the UI
   can show "Senate eFD: last succeeded 3 days ago" or "House Clerk PDF
   format changed -- 12 filings could not be parsed" without needing to
   parse the log file.
"""

import logging
import logging.handlers
import os
from datetime import datetime, timedelta, timezone

from .. import db

_logger = None


def get_logger():
    """Returns the shared pipeline logger, configured on first use to write
    to data/logs/pipeline.log (rotated at 5MB, keeping 3 backups) as well as
    stderr, so pipeline runs are visible both in a persistent file and in
    the console during development."""
    global _logger
    if _logger is not None:
        return _logger

    logs_dir = os.path.join(db.get_data_dir(), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, "pipeline.log")

    logger = logging.getLogger("politician_trades.pipeline")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        )
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    _logger = logger
    return _logger


def _record_event(source, level, event, message, filing_id=None):
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pipeline_events (ts, source, level, event, message, filing_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now, source, level, event, message, filing_id),
        )


def log_parse_failure(source, filing_id, error, exc_info=None):
    """Records a single filing's parse failure -- both to the log file
    (with a traceback, if given) and to pipeline_events (for the UI)."""
    get_logger().error(
        "Parse failure source=%s filing_id=%s error=%s", source, filing_id, error,
        exc_info=exc_info,
    )
    _record_event(source, "error", "parse_failure", str(error), filing_id=filing_id)


def log_unrecognized_format(source, detail):
    """Records that a source's data no longer matches the format this
    pipeline's parser was built against (e.g. a changed column header, an
    HTML structure the eFD scraper doesn't recognize). This is intentionally
    louder than a single filing's parse failure -- it usually means every
    filing from that source will fail until the parser is updated."""
    get_logger().error("Unrecognized format source=%s detail=%s", source, detail)
    _record_event(source, "error", "unrecognized_format", str(detail))


def log_fallback_used(source, reason):
    """Records that a source's primary fetch path failed and the pipeline
    fell back to a secondary source."""
    get_logger().warning("Falling back for source=%s reason=%s", source, reason)
    _record_event(source, "warning", "fallback_used", str(reason))


def log_info(source, message):
    get_logger().info("%s: %s", source, message)
    _record_event(source, "info", "info", message)


def mark_attempt(source):
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pipeline_source_status (source, last_attempt_at)
            VALUES (?, ?)
            ON CONFLICT(source) DO UPDATE SET last_attempt_at=excluded.last_attempt_at
            """,
            (source, now),
        )


def mark_success(source):
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pipeline_source_status (source, last_attempt_at, last_success_at, consecutive_failures)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(source) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,
                last_success_at=excluded.last_success_at,
                last_error=NULL,
                consecutive_failures=0
            """,
            (source, now, now),
        )


def mark_failure(source, error):
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM pipeline_source_status WHERE source = ?", (source,)
        ).fetchone()
        failures = (row["consecutive_failures"] if row else 0) + 1
        conn.execute(
            """
            INSERT INTO pipeline_source_status (source, last_attempt_at, last_error, consecutive_failures)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,
                last_error=excluded.last_error,
                consecutive_failures=excluded.consecutive_failures
            """,
            (source, now, str(error), failures),
        )


# How long a source can go without a successful run before it's considered
# "stale" and surfaced as a warning in /api/pipeline/status. Chosen to be
# generously longer than the automatic refresh interval (default: hourly)
# so a single missed run doesn't cry wolf -- this is about a source that's
# been silently broken for a while.
STALE_THRESHOLD_HOURS = 48


def is_source_stale(source):
    """Pure (no logging/db-write side effects) check: returns (is_stale,
    last_success_at) for `source`, based on whether it has succeeded within
    STALE_THRESHOLD_HOURS. Safe to call as often as needed (e.g. from a
    read-only status API) without spamming pipeline_events."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT last_success_at, last_attempt_at FROM pipeline_source_status WHERE source = ?",
            (source,),
        ).fetchone()

    if not row or not row["last_attempt_at"]:
        return False, None  # never even attempted yet -- nothing to alert on

    last_success_at = row["last_success_at"]
    is_stale = True
    if last_success_at:
        try:
            last_dt = datetime.fromisoformat(last_success_at)
            is_stale = (datetime.now(timezone.utc) - last_dt) >= timedelta(hours=STALE_THRESHOLD_HOURS)
        except ValueError:
            is_stale = True

    return is_stale, last_success_at


def check_staleness(source):
    """Same check as is_source_stale(), but also records a 'stale_data'
    pipeline_event + log line when the source is stale. Intended to be
    called once per pipeline run (see orchestrator.py), not from read-only
    status queries -- use is_source_stale() for those."""
    is_stale, last_success_at = is_source_stale(source)
    if is_stale:
        detail = (
            f"no successful refresh since {last_success_at}" if last_success_at
            else "has never completed successfully"
        )
        get_logger().warning("Stale data source=%s detail=%s", source, detail)
        _record_event(source, "warning", "stale_data", detail)

    return is_stale, last_success_at


def recent_events(limit=100):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pipeline_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def all_source_status():
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM pipeline_source_status").fetchall()
        return [dict(r) for r in rows]
