"""
Ingestion + publish job for the "Lite" app's data feed.

Run on a schedule (see .github/workflows/publish-data.yml) rather than by
end users -- this does the exact same work the full app's own "Refresh
Data" already does (backend/data_fetch.py's refresh_data(), the same
pipeline that downloads/parses/OCRs House Clerk + Senate eFD filings), just
centrally instead of once per user. The result is gzipped and published to
a single, fixed GitHub Release (tag RELEASE_TAG) that never changes name --
the Lite app (backend/snapshot_download.py) always just asks "what's on
that tag right now", no version-comparison logic needed on either side.

Critically, this ALWAYS starts by downloading whatever is currently
published and restoring it as this run's starting database, before calling
refresh_data(). Without that, every run would start from a completely
empty database with no processed_filings history, and dedup.should_process
would treat every single filing (including every OCR-needing scanned one)
as new every single hour -- turning a cheap incremental top-up into a full
re-ingestion of years of history on every scheduled run. Restoring the
previous snapshot first is what makes this incremental, matching exactly
how a long-running desktop install stays incremental across restarts.
"""
import gzip
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from backend import data_fetch, db  # noqa: E402
from backend.version import GITHUB_REPO  # noqa: E402

RELEASE_TAG = "latest-data"
DB_ASSET_NAME = "politicians.db.gz"
CHECKSUM_ASSET_NAME = "politicians.db.gz.sha256"

GITHUB_API = "https://api.github.com"
GITHUB_UPLOADS = "https://uploads.github.com"
REQUEST_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120


def _auth_headers():
    # GITHUB_TOKEN is provided automatically by GitHub Actions for every
    # workflow run -- no manually-created secret needed (see the workflow's
    # `permissions: contents: write`, which is what allows this token to
    # publish releases rather than just read the repo).
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_release_by_tag():
    resp = requests.get(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}",
        headers=_auth_headers(), timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def restore_previous_snapshot(db_path):
    """Downloads the currently-published DB (if any) and decompresses it to
    db_path. Returns True if an existing snapshot was restored, False if
    this is the very first publish (nothing to restore yet)."""
    release = _get_release_by_tag()
    if not release:
        print("No existing 'latest-data' release -- starting from an empty database.")
        return False
    asset = next((a for a in release.get("assets", []) if a["name"] == DB_ASSET_NAME), None)
    if not asset:
        print("Existing release has no DB asset -- starting from an empty database.")
        return False
    resp = requests.get(asset["browser_download_url"], timeout=DOWNLOAD_TIMEOUT)
    resp.raise_for_status()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open(db_path, "wb") as f:
        f.write(gzip.decompress(resp.content))
    print(f"Restored previous snapshot ({len(resp.content):,} bytes compressed) to {db_path}")
    return True


def _content_fingerprint():
    """A hash of the actual trade/politician DATA (not the raw .db file
    bytes) -- SQLite's on-disk layout can differ slightly between two
    otherwise-identical databases (page ordering, freelist state, etc.),
    which would make a raw-file hash comparison report "changed" even when
    nothing meaningfully did. Hashing a deterministic dump of the rows that
    actually matter avoids that false positive."""
    h = hashlib.sha256()
    with db.get_conn() as conn:
        for row in conn.execute(
            "SELECT id, bioguide_id, ticker, asset_description, transaction_type, "
            "transaction_date, disclosure_date, amount_range, data_source, external_id "
            "FROM trades ORDER BY id"
        ):
            h.update("|".join("" if v is None else str(v) for v in row).encode("utf-8"))
            h.update(b"\n")
        for row in conn.execute("SELECT bioguide_id, full_name, party, state FROM politicians ORDER BY bioguide_id"):
            h.update("|".join("" if v is None else str(v) for v in row).encode("utf-8"))
            h.update(b"\n")
    return h.hexdigest()


def _sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def publish_snapshot(db_path):
    """Gzips db_path and publishes it (+ its checksum) to the fixed
    'latest-data' release, replacing any existing same-named assets --
    GitHub rejects a re-upload under a name that's already attached to the
    release, so old copies are deleted first."""
    gz_path = db_path.with_name(db_path.name + ".gz")
    with open(db_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    checksum = _sha256_of_file(gz_path)

    release = _get_release_by_tag()
    if release is None:
        resp = requests.post(
            f"{GITHUB_API}/repos/{GITHUB_REPO}/releases",
            headers=_auth_headers(),
            json={
                "tag_name": RELEASE_TAG,
                "name": "Latest data snapshot",
                "body": (
                    "Automatically published by .github/workflows/publish-data.yml. "
                    "This is a data feed for the Lite app, not a software release -- "
                    "see the actual versioned releases for the app itself."
                ),
                "prerelease": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        release = resp.json()

    release_id = release["id"]
    for asset in release.get("assets", []):
        if asset["name"] in (DB_ASSET_NAME, CHECKSUM_ASSET_NAME):
            requests.delete(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/releases/assets/{asset['id']}",
                headers=_auth_headers(), timeout=REQUEST_TIMEOUT,
            ).raise_for_status()

    with open(gz_path, "rb") as f:
        resp = requests.post(
            f"{GITHUB_UPLOADS}/repos/{GITHUB_REPO}/releases/{release_id}/assets?name={DB_ASSET_NAME}",
            headers={**_auth_headers(), "Content-Type": "application/gzip"},
            data=f.read(), timeout=DOWNLOAD_TIMEOUT,
        )
        resp.raise_for_status()

    resp = requests.post(
        f"{GITHUB_UPLOADS}/repos/{GITHUB_REPO}/releases/{release_id}/assets?name={CHECKSUM_ASSET_NAME}",
        headers={**_auth_headers(), "Content-Type": "text/plain"},
        data=checksum.encode("utf-8"), timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    print(f"Published {gz_path.name} ({os.path.getsize(gz_path):,} bytes gzipped), sha256={checksum}")


def main():
    db_path = Path(db.get_data_dir()) / "politicians.db"
    restore_previous_snapshot(db_path)

    db.init_db()
    previous_fingerprint = _content_fingerprint()

    db.set_meta("schema_version", str(db.SCHEMA_VERSION))
    summary = data_fetch.refresh_data(progress_cb=print, since_date=data_fetch.min_refresh_since_date())
    print(json.dumps(summary, indent=2, default=str))

    new_fingerprint = _content_fingerprint()
    if new_fingerprint == previous_fingerprint:
        print("No changes since last published snapshot -- skipping publish.")
        return

    publish_snapshot(db_path)


if __name__ == "__main__":
    main()
