"""
Filing-level dedup/checksum bookkeeping (processed_filings table). Lets the
pipeline skip re-downloading and re-parsing an individual filing (a House
PTR PDF, a Senate eFD PTR report) that has already been successfully
processed and hasn't changed upstream since -- while still re-processing a
previously *failed* filing (in case a bug fix or upstream correction now
makes it parseable) or a filing whose content hash has changed (e.g. an
amended disclosure).
"""

import hashlib

from .. import db


def content_hash(raw_bytes):
    return hashlib.sha256(raw_bytes).hexdigest()


def get_processed(source, filing_id):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM processed_filings WHERE source = ? AND filing_id = ?",
            (source, filing_id),
        ).fetchone()
        return dict(row) if row else None


def should_process(source, filing_id, new_hash, parser_version=None):
    """Returns True if this filing needs to be (re-)parsed: either it's
    never been seen before, its content has changed since last time, the
    last attempt at it failed (worth retrying on a subsequent run), or the
    parser's own version has changed since it was last successfully parsed
    (worth re-parsing so a bug fix -- e.g. a corrected ticker-extraction
    regex -- actually gets applied to already-loaded filings, not just new
    ones). `parser_version`, if omitted, skips that last check for callers
    that don't track a version (there are none currently, but this keeps
    the parameter optional/backwards-compatible)"""
    prior = get_processed(source, filing_id)
    if not prior:
        return True
    if prior.get("status") != "ok":
        return True
    if prior.get("content_hash") != new_hash:
        return True
    if parser_version and prior.get("parser_version") != parser_version:
        return True
    return False


def record_result(source, filing_id, content_hash_value, parser_version, status, trade_count, error, parsed_at):
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO processed_filings
                (source, filing_id, content_hash, parser_version, status, trade_count, error, parsed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, filing_id) DO UPDATE SET
                content_hash=excluded.content_hash,
                parser_version=excluded.parser_version,
                status=excluded.status,
                trade_count=excluded.trade_count,
                error=excluded.error,
                parsed_at=excluded.parsed_at
            """,
            (source, filing_id, content_hash_value, parser_version, status, trade_count, error, parsed_at),
        )


def count_processed(source, status=None):
    with db.get_conn() as conn:
        if status:
            return conn.execute(
                "SELECT COUNT(*) c FROM processed_filings WHERE source = ? AND status = ?",
                (source, status),
            ).fetchone()["c"]
        return conn.execute(
            "SELECT COUNT(*) c FROM processed_filings WHERE source = ?", (source,)
        ).fetchone()["c"]
