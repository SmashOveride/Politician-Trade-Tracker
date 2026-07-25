"""
Client-side half of the "Lite" data feed: downloads the pre-built,
pre-OCR'd database snapshot published by scripts/publish_snapshot.py (via
.github/workflows/publish-data.yml) instead of fetching/parsing/OCR-ing raw
filings locally. This is what lets the Lite build skip bundling Tesseract/
pdfplumber/lxml/PIL/pypdfium2 entirely -- see backend/data_fetch.py's
refresh_data(), which the full (non-Lite) build still uses instead of this.

Mirrors backend/update_check.py's GitHub API query pattern (same repo,
same "ask GitHub what's published, act only if it's newer" shape) but for
a fixed data release rather than the app's own versioned releases.
"""
import gzip
import hashlib
import os
import time
from typing import Any, Dict

import requests

from . import db, settings
from .version import GITHUB_REPO

RELEASE_TAG = "latest-data"
DB_ASSET_NAME = "politicians.db.gz"
CHECKSUM_ASSET_NAME = "politicians.db.gz.sha256"

RELEASE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}"

REQUEST_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 120


def _get_release():
    """Returns the 'latest-data' release's JSON (assets, published_at,
    etc.), or None if it doesn't exist yet (e.g. the publish workflow
    hasn't run for the first time) or GitHub is unreachable. Never raises."""
    try:
        resp = requests.get(
            RELEASE_API_URL,
            headers={"Accept": "application/vnd.github+json"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _last_synced_published_at():
    return settings.load_settings().get("snapshot_last_synced_published_at")


def check_for_update():
    """Returns (release_dict, is_newer) without downloading anything --
    used by the UI to show "update available" before committing to a
    download. is_newer is False if there's no release yet, GitHub is
    unreachable, or it's the same one already synced."""
    release = _get_release()
    if not release:
        return None, False
    return release, release.get("published_at") != _last_synced_published_at()


def sync_snapshot(force=False):
    """Downloads and adopts the latest published snapshot if it's newer
    than the last one this install synced (or unconditionally if
    force=True). Returns a summary dict shaped like data_fetch.refresh_data
    so callers (the auto-refresh scheduler, the Refresh Data button) can
    report it the same way regardless of which refresh path ran.

    Verifies the download's SHA-256 against the published checksum before
    touching anything, and refuses a snapshot whose schema_version is
    newer than this build's db.SCHEMA_VERSION (an older client encountering
    a schema it doesn't understand) -- an older *snapshot* is always fine,
    db.init_db()'s own migrations bring it up to date after the swap.
    """
    summary: Dict[str, Any] = {"started_at": time.time(), "steps": [], "errors": []}

    release, is_newer = check_for_update()
    if release is None:
        summary["errors"].append("No published data snapshot found (or GitHub unreachable).")
        return summary
    if not force and not is_newer:
        summary["steps"].append("Already up to date with the latest published snapshot.")
        return summary

    assets_by_name = {a["name"]: a for a in release.get("assets", [])}
    db_asset = assets_by_name.get(DB_ASSET_NAME)
    checksum_asset = assets_by_name.get(CHECKSUM_ASSET_NAME)
    if not db_asset or not checksum_asset:
        summary["errors"].append("Published release is missing expected files.")
        return summary

    try:
        checksum_resp = requests.get(checksum_asset["browser_download_url"], timeout=REQUEST_TIMEOUT)
        checksum_resp.raise_for_status()
        expected_checksum = checksum_resp.text.strip()

        db_resp = requests.get(db_asset["browser_download_url"], timeout=DOWNLOAD_TIMEOUT)
        db_resp.raise_for_status()
        gz_bytes = db_resp.content
    except Exception as e:
        summary["errors"].append(f"Download failed: {e}")
        return summary

    actual_checksum = hashlib.sha256(gz_bytes).hexdigest()
    if actual_checksum != expected_checksum:
        summary["errors"].append(
            f"Downloaded snapshot failed checksum verification (expected {expected_checksum[:12]}..., "
            f"got {actual_checksum[:12]}...) -- discarding it rather than risking a corrupted database."
        )
        return summary

    try:
        new_db_bytes = gzip.decompress(gz_bytes)
    except Exception as e:
        summary["errors"].append(f"Downloaded snapshot isn't valid gzip data: {e}")
        return summary

    data_dir = db.get_data_dir()
    temp_path = os.path.join(data_dir, "politicians.db.download")
    final_path = os.path.join(data_dir, "politicians.db")
    with open(temp_path, "wb") as f:
        f.write(new_db_bytes)

    try:
        remote_schema_version = _peek_schema_version(temp_path)
        if remote_schema_version is not None and remote_schema_version > db.SCHEMA_VERSION:
            summary["errors"].append(
                f"Published snapshot's schema (v{remote_schema_version}) is newer than this app build "
                f"understands (v{db.SCHEMA_VERSION}) -- skipping to avoid misreading it. Update the app."
            )
            return summary

        # os.replace is atomic (and, unlike os.rename, works on Windows even
        # when final_path already exists) -- there's never a moment where
        # final_path is missing or half-written.
        os.replace(temp_path, final_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    db.init_db()  # applies any pending column migrations to the newly-adopted snapshot
    settings.save_settings({"snapshot_last_synced_published_at": release.get("published_at")})

    summary["steps"].append(
        f"Downloaded and adopted data snapshot published {release.get('published_at')} "
        f"({len(gz_bytes):,} bytes)."
    )
    return summary


def _peek_schema_version(sqlite_path):
    """Reads schema_version from a snapshot's meta table without going
    through the shared db.get_conn() (which always points at the live,
    not-yet-replaced database) -- opens the just-downloaded file directly,
    read-only, closing it immediately after. Returns None if unreadable or
    the key is missing (treated as compatible -- older snapshots predating
    this versioning entirely are still just the schema this code already
    knows how to migrate)."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            return int(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()
    except Exception:
        return None
