"""
Checks GitHub for a newer published release of this app than the one
currently running (see backend/version.py for the running version and
target repo), to power the "Update App" / "Update Available" button in
Settings.

This is entirely read-only and best-effort:
- It never downloads or installs anything -- clicking "Update Available"
  just opens the repo's releases page in the user's browser, same as
  manually checking for updates. Applying an update is still a manual
  step (re-download the latest build), same as installing this app the
  first time.
- If GitHub is unreachable, rate-limited, or the repo has no releases yet,
  this fails soft (no update reported) rather than surfacing an error to
  the user -- there is no "offline" state to show, just "no update found
  right now."
- Results are cached briefly (see CACHE_TTL_SECONDS) so repeatedly opening
  the Settings panel doesn't hammer GitHub's API or its low unauthenticated
  rate limit.
"""

import time
from typing import Any, Dict

import requests

from .version import APP_VERSION, GITHUB_REPO

RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases"

REQUEST_TIMEOUT = 6
CACHE_TTL_SECONDS = 60 * 60  # 1 hour -- generous enough to avoid API hammering,
# short enough that a freshly-published release is noticed the same day.

_cache: Dict[str, Any] = {"checked_at": 0.0, "result": None}


def _parse_version(version_string):
    """Parses a 'v1.2.3' or '1.2.3'-style string into a tuple of ints for
    comparison, e.g. (1, 2, 3). Non-numeric trailing parts (e.g. '1.2.3-beta')
    are truncated at the first non-numeric segment. Returns None if nothing
    resembling a version number could be parsed at all."""
    cleaned = version_string.strip().lstrip("vV")
    parts = []
    for segment in cleaned.split("."):
        digits = ""
        for ch in segment:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _is_newer(remote_version_str, local_version_str):
    remote = _parse_version(remote_version_str)
    local = _parse_version(local_version_str)
    if remote is None or local is None:
        return False
    return remote > local


def _fetch_latest_release():
    """Queries GitHub's public Releases API for the latest published
    release. Returns a dict with 'version' (tag name) and 'html_url', or
    None if there's no release yet or the request fails for any reason."""
    try:
        resp = requests.get(
            RELEASES_API_URL,
            headers={"Accept": "application/vnd.github+json"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return None  # repo has no releases published yet
        resp.raise_for_status()
        data = resp.json()
        tag = data.get("tag_name")
        if not tag:
            return None
        return {"version": tag, "html_url": data.get("html_url") or RELEASES_PAGE_URL}
    except Exception:
        return None


def check_for_update(force=False):
    """Returns a dict describing update status:
        {
            "current_version": "1.1.0",
            "latest_version": "1.2.0" | None,
            "update_available": bool,
            "release_url": "https://github.com/.../releases/latest",
        }
    Cached for CACHE_TTL_SECONDS unless `force` is True (e.g. the user
    explicitly clicked a "check now" action, if one is ever added)."""
    now = time.time()
    if not force and _cache["result"] is not None and (now - _cache["checked_at"]) < CACHE_TTL_SECONDS:
        return _cache["result"]

    latest = _fetch_latest_release()
    result = {
        "current_version": APP_VERSION,
        "latest_version": latest["version"] if latest else None,
        "update_available": bool(latest) and _is_newer(latest["version"], APP_VERSION),
        "release_url": latest["html_url"] if latest else RELEASES_PAGE_URL,
    }

    _cache["checked_at"] = now
    _cache["result"] = result
    return result
