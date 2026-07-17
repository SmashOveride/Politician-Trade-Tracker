"""
Shared HTTP layer for the data collection pipeline: retries with exponential
backoff for transient network/server errors, plus on-disk caching (ETag /
Last-Modified conditional requests, with a SHA-256 content-hash fallback for
endpoints that don't support conditional GET, e.g. POST-based search APIs).

Reuses the same data/cache/ directory and source_cache table that
data_fetch.py uses, so cache state is consistent across both the legacy
JSON-dump ingestion path and this pipeline.
"""

import hashlib
import os
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .. import db

DEFAULT_TIMEOUT = 30

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PoliticianTradesApp/1.0; +https://github.com)",
}


def build_session(total_retries=4, backoff_factor=1.5):
    """Returns a requests.Session configured to automatically retry
    transient failures (connection errors, read timeouts, 429, and 5xx
    responses) with exponential backoff, so a flaky network or a
    momentarily-overloaded server doesn't fail an entire pipeline run."""
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        status=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _cache_dir():
    d = os.path.join(db.get_data_dir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_file_path(cache_key):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in cache_key)
    # Cache keys in this module can be long (POST bodies folded in) --
    # collapse to a fixed-length, collision-resistant name.
    digest = hashlib.sha256(safe.encode()).hexdigest()[:24]
    return os.path.join(_cache_dir(), f"{digest}.raw")


def _get_cache_meta(cache_key):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM source_cache WHERE source_key = ?", (cache_key,)
        ).fetchone()
        return dict(row) if row else None


def _save_cache_meta(cache_key, url, etag, last_modified, content_hash, changed):
    now = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT last_changed_at FROM source_cache WHERE source_key = ?", (cache_key,)
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
            (cache_key, url, etag, last_modified, content_hash, now, last_changed_at),
        )


def fetch_with_cache(
    url,
    session=None,
    method="GET",
    cache_key=None,
    data=None,
    json_body=None,
    headers=None,
    timeout=DEFAULT_TIMEOUT,
    use_conditional_headers=True,
):
    """Fetches `url` (GET or POST), using a locally cached copy plus, for
    GET requests, the server's ETag/Last-Modified headers (when supported)
    to avoid re-downloading content that hasn't changed. POST requests (e.g.
    search APIs that don't support conditional GET) fall back to a pure
    content-hash comparison against the previous response body.

    Returns (content_bytes, changed):
    - changed=False means nothing has changed since last time (304, or an
      identical content hash) -- content_bytes is the cached copy.
    - changed=True means new/updated content was fetched.

    `cache_key` defaults to `url`, but callers making several different POST
    requests to the same URL (e.g. paginated search results) should pass a
    distinguishing key (e.g. url + query params) so each is tracked/cached
    independently.
    """
    session = session or build_session()
    cache_key = cache_key or url
    cache_path = _cache_file_path(cache_key)
    prior = _get_cache_meta(cache_key)
    have_cache_file = prior is not None and os.path.exists(cache_path)

    request_headers = dict(headers or {})
    if use_conditional_headers and method.upper() == "GET" and have_cache_file and prior:
        if prior.get("etag"):
            request_headers["If-None-Match"] = prior["etag"]
        if prior.get("last_modified"):
            request_headers["If-Modified-Since"] = prior["last_modified"]

    resp = session.request(
        method, url, data=data, json=json_body, headers=request_headers, timeout=timeout
    )

    if resp.status_code == 304 and have_cache_file and prior:
        with open(cache_path, "rb") as f:
            content = f.read()
        _save_cache_meta(
            cache_key, url, prior.get("etag"), prior.get("last_modified"),
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
        cache_key, url, resp.headers.get("ETag"), resp.headers.get("Last-Modified"),
        new_hash, changed=changed,
    )
    return content, changed
