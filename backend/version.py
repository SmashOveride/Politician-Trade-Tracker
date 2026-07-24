"""
Single source of truth for this app's version number, and the GitHub repo
it checks against for updates.

Bump APP_VERSION here as part of every release, and add a matching entry
to CHANGELOG.md describing what changed -- see "Versioning & releasing
updates" in README.md for the full release checklist. This value is:

- Surfaced to the UI via /api/meta (shown in the footer).
- Compared against the latest published GitHub Release by
  backend/update_check.py to power the "Update App" / "Update Available"
  button in Settings.
- Read by packaging/macos.spec so the macOS .app bundle's version metadata
  always matches without needing to update it in two places.

Follows semantic versioning (MAJOR.MINOR.PATCH):
- MAJOR: incompatible/breaking changes (rare for this app).
- MINOR: new features, backwards-compatible (e.g. a new data source, a new
  optional integration).
- PATCH: bug fixes, small tweaks, no new features.
"""

APP_VERSION = "1.2.0"

# owner/repo on GitHub this app checks for updates against (via GitHub's
# public Releases API) and which the "Update App" button links to.
GITHUB_REPO = "SmashOveride/Politician-Trade-Tracker"
