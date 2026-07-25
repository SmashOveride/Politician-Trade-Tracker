# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`).

The current version lives in `backend/version.py` (`APP_VERSION`) -- see
"Versioning & releasing updates" in README.md for the full release checklist.

## [1.3.1] - 2026-07-25
### Fixed
- Lite's data-snapshot staleness check compared a GitHub release's
  `published_at` field, which freezes to whenever the release was first
  created and never updates on later asset re-uploads -- since the
  publish job reuses the same fixed release forever rather than creating
  a new one each time, every Lite install would have silently believed
  it was always up to date after its very first sync, no matter how much
  newer data got published afterward. Now compares each asset's own
  `updated_at`, which genuinely changes on re-upload. v1.3.0's Lite builds
  shipped with this bug; v1.3.1 is the first Lite build where auto-refresh
  actually pulls newer snapshots as expected.

## [1.3.0] - 2026-07-25
### Added
- **Politician Trade Tracker Lite**: a second, ~35MB build (versus the full
  build's ~238MB) that never bundles the live parsing/OCR pipeline
  (pdfplumber, lxml, pytesseract, PIL, pypdfium2, Tesseract itself) --
  instead it downloads a pre-built, pre-OCR'd database snapshot. Which
  code path runs falls out naturally from whether those dependencies are
  actually present (`backend/data_fetch.py`'s `pipeline_available()`), not
  a separate mode flag, so the full build's behavior is unchanged.
- Automatic hourly data-snapshot publishing: `scripts/publish_snapshot.py`
  runs the same pipeline the full app already uses, then publishes the
  result (gzipped, checksummed, schema-versioned) to a fixed `latest-data`
  GitHub Release. Runs on a schedule via
  `.github/workflows/publish-data.yml`, on GitHub's own infrastructure --
  free for this public repo, nothing needs to stay running on anyone's
  machine. `backend/snapshot_download.py` is the client side: verifies the
  download's checksum and schema version before atomically adopting it.
- macOS builds (full + Lite) via `.github/workflows/build-macos.yml`,
  using GitHub's real macOS runners -- PyInstaller can't cross-compile, so
  this is what makes a Mac build possible without owning a Mac at all.
  Triggered manually, or automatically on every version tag push, which
  attaches the built `.app` zips directly to that release.

### Changed
- Packaged Windows builds no longer duplicate the entire bundled Tesseract
  package (every DLL) between `_internal/tesseract/` and the top level of
  `_internal` -- a PyInstaller quirk that was pure dead weight, ~150MB of
  it. `windows.spec` now strips the duplicates automatically on every
  build (389MB -> 238MB, confirmed).
- `backend/db.py`'s SQLite connections use a 30-second busy timeout
  (was the 5-second default), since multiple writers can now legitimately
  be active at once (the app's own auto-refresh, plus e.g. the snapshot
  publishing job).

## [1.2.0] - 2026-07-24
### Added
- Left-rail filter panel on Recent Disclosures (date range, trade type,
  party, disclosed-amount bucket, ticker/asset search), all applied
  server-side (`/api/trades/recent`'s new query params) so filtering works
  correctly across the whole dataset, not just the current page.
- First-launch popup (Windows packaged build only, `backend/shortcuts.py`)
  offering to add a Desktop icon and/or Start Menu shortcut for the app --
  a checkbox per location, both pre-checked, shown at most once. Lets
  someone who received a prebuilt copy of the app launch it like any other
  installed program instead of navigating back to its folder every time.
- Best-effort ticker resolution for name-only trades
  (`backend/ticker_resolve.py`, run automatically at the end of every
  refresh): trades recovered from the paper checkbox PTR form disclose an
  asset NAME but no ticker (the form has no ticker column), locking them
  out of ticker-keyed features. Names are now matched against the
  ticker-bearing e-filed trades already in the database (the same
  disclosure-style naming universe) through four strictly-unique-answer
  tiers: exact normalized match, token-subset (OCR junk around a real
  name), de-spaced substring (OCR-glued words, 'PFIZERINC.'), and
  high-cutoff fuzzy ('MICROSGFTCORPORATIONCMN' -> MSFT). Ambiguous names
  are always left blank rather than guessed ('... MOTOR COMPANY' with the
  maker's name garbled resolves to nothing, not to Ford-or-GM). First run
  resolved ~1,550 rows across ~1,200 distinct names. Parenthesized name
  words like "(The)"/"(New)" are no longer misread as tickers (they were
  being extracted as literal tickers 'THE'/'NEW' and then spread to clean
  rows via this same matching), and price-history lookups for profit/loss
  are now capped at 25 live fetches per request with failures cached --
  a paper-form filer's detail page can reference hundreds of resolved
  tickers, which previously meant minutes of serial Yahoo calls on first
  load (measured: >5 minutes; now ~15 seconds cold, faster warm).
- Bundled Tesseract OCR in the packaged Windows build (see
  `packaging/fetch_tesseract_windows.py` and `windows.spec`), so scanned
  House PTR filings get OCR-recovered with nothing for the user to install.
  Running from source, or building for macOS/Linux, still relies on a
  system-installed Tesseract as before.

### Changed
- Recent Disclosures now pages through every disclosed trade (not just a
  7-day window), 50 per page, with Previous/Next, a jump-to-page field, and
  a total record count (`/api/trades/recent`).
- Windows/macOS/Linux packaged builds no longer use UPX compression
  (`upx=False` in every `packaging/*.spec`) -- UPX-packed executables are
  one of the most common antivirus false-positive triggers, since a lot of
  real malware also uses UPX to evade signature scanning; not worth the
  smaller binary size here. The Windows build also now embeds a real
  version-info resource (publisher/product name/version, regenerated from
  `backend/version.py` on every build) rather than shipping with none.
  Signing/checksum guidance for anyone distributing built executables is
  now documented in README.md's new "Antivirus / SmartScreen false
  positives" section.
- Windows packaged build is ~40% smaller (389MB -> 238MB in testing):
  `windows.spec` now automatically strips a PyInstaller quirk that
  duplicated the entire bundled Tesseract OCR package (every DLL, ~150MB)
  to both `_internal/tesseract/` and the top level of `_internal` --
  confirmed the top-level copies were always dead weight (`tesseract.exe`
  only ever needs its own dependencies in its own directory) by running
  real OCR end-to-end against a build with only the top-level copies
  removed.

### Fixed
- Asset names on scanned/OCR'd filings got the same manual-review pass
  originally done for Rep. Khanna extended to every other paper-filer
  politician in the database: garbled OCR text now consistently resolves
  to either a clean ticker-matched company name, a cleaned-up but still
  human-readable name, or an honest "Unreadable -- See Records. P.# Row#"
  pointer to the exact page/row in the source filing -- never left as raw
  OCR noise. Along the way, fixed two real gaps in the underlying
  garbled-text heuristic (`backend/asset_quality.py`'s `is_garbled`): a
  4+-repeated-character run (e.g. "Csnoooooooocnc") and a short repeating
  pattern (e.g. "oscscscsc") both evaded every existing check (mixed case
  dodges the lowercase-ratio rule, scattered vowels dodge the vowel-ratio
  rule) despite being obvious OCR gibberish -- both are now detected, with
  zero false positives confirmed against every already-reviewed name in
  the database.
- A display-time bug (`app.py`'s `_trade_row_to_dict`) was silently
  re-flagging already-cleaned, human-reviewed asset names (e.g. "Realty
  Income Corporation") back to the generic "Unreadable" label on every
  page load: the garbled-text heuristic is designed for raw ALL-CAPS OCR
  text, and Title Case is inherently majority-lowercase by letter count,
  which tripped its own lowercase-ratio check. Now skipped entirely once a
  row's asset description has been through the review pass above.
  Separately, a bogus ticker ("IXNZF") caused by OCR misreading a
  page-watermark/artifact as a stock symbol -- found attached to 1,381
  otherwise-unrelated rows across two different filers -- was cleared out
  and added to the parser's known-non-ticker list so it can't recur.
- Over a third of all House Clerk filings on file (622 of 1,739) had never
  been reprocessed under the parser improvements above -- a one-time sweep
  reprocessed every stale filing app-wide (not just Khanna's), recovering
  filings that had been sitting at 0 usable transactions since before this
  session's OCR/checkbox-form work even started.
- `backend/db.py`'s SQLite connections now use a 30-second busy timeout
  (was the 5-second default) -- matters once multiple writers can be
  active at once (the app's own background auto-refresh plus, e.g., the
  bulk reprocessing above run as several parallel processes), where the
  default was tight enough to occasionally raise "database is locked" on a
  perfectly normal, just-slightly-delayed write.
- PARSER_VERSION v7: scanned *paper* checkbox-grid PTR filings (e.g. every
  one of Rep. Ro Khanna's monthly filings, all of which previously loaded
  zero trades) are now parsed via a new image-analysis fallback
  (`backend/pipeline/checkbox_form.py`): the form's grid columns are
  detected from pixels and validated against the printed template's cell
  proportions, rows are anchored on the OCR'd date columns (with
  fixed-pitch slot recovery for rows whose dates OCR'd to garbage), and
  the Purchase/Sale/Exchange + amount-bucket checkboxes are read as ink
  density relative to each row's own baseline -- never as text, which is
  unrecoverable on these scans. Verified against a visually-confirmed real
  page (types, partial-sale and capital-gain flags, and amount buckets all
  match). Also fixed upside-down scans: Tesseract's orientation detection
  sometimes picks the wrong direction along the right axis (confirmed: a
  whole real filing 180 degrees off, OCR-ing as mirrored gibberish), so
  pages whose mean OCR confidence is poor are retried rotated 180 and the
  higher-confidence read wins. Net effect on Rep. Khanna alone: two sample
  filings went from 0 recovered transactions to 204 and 215; several other
  previously-unparseable scanned filings now recover rows too (honestly
  marked type-unknown/no-amount where their layout isn't the checkbox
  form). The paper form has no ticker column, so these trades genuinely
  have no ticker; asset names are best-effort OCR and the Records button
  links each row's official filing PDF.
- PARSER_VERSION v8: OCR now jointly searches page orientation (0/180) and
  Tesseract segmentation mode (PSM 6/3) whenever a page reads poorly,
  instead of deciding them one stage at a time -- the staged logic could
  lock in a wrong 180-flip on a sparse page (confirmed on a real filing
  page with 4 filled rows atop an empty grid) and never revisit it, losing
  the page's transactions entirely even though the correct
  orientation+mode combination read the actual company names fine.
- Running from source on Windows now uses the staged vendor Tesseract at
  `packaging/vendor/tesseract-windows/` (when present) instead of requiring
  a system install -- previously a source checkout on a machine with no
  system Tesseract silently ran with OCR unavailable even though a fully
  working bundled copy sat in the repo, so a whole refresh "re-parsed" the
  scanned filings in seconds by skipping OCR entirely.
- House Clerk OCR fallback (`house_clerk.py`'s PARSER_VERSION bumped to v4)
  is now robust to noisy scans instead of silently recovering zero
  transactions from an otherwise-readable filing: visual-line grouping now
  tolerates OCR's per-word position jitter instead of exact rounding,
  column headers wrapped across multiple lines are accumulated instead of
  requiring them all on one line, and a missing "Notification Date" column
  (when OCR never recognizes that specific header cell) no longer blocks
  every transaction on the filing.
- PARSER_VERSION v5/v6: fixed filings scanned with the whole page rotated
  90 degrees (confirmed on real filings, e.g. Rep. Khanna's) -- `ocr.py`
  now detects and corrects page rotation (via Tesseract's OSD) before the
  main OCR pass. Also switched Tesseract's page segmentation mode to
  `--psm 6`, since the default mode badly under-reads dense grid-lined PTR
  tables (confirmed: 34 words extracted vs. 260 with `--psm 6` on the same
  page) -- this alone nearly doubled real transactions recovered on an
  already-working scanned filing (7 -> 12). Dropped "owner" as a required
  header anchor, since some forms label that column "SP/DC/JT" and never
  contain the literal word "owner" at all. Loosened the transaction/
  notification date pattern to accept non-zero-padded dates ("2/24/2024",
  not just "02/24/2024"), which some filings (and some OCR passes) produce.
  Net effect on the originally-investigated batch of scanned filings: one
  now recovers real transactions where it previously recovered none, two
  progress further (a header is now found) but still need work on
  extracting a date from a noisy multi-word OCR'd cell rather than an exact
  match, and the remainder are degraded enough (illegible header text, or
  scan quality issues beyond rotation) to need dedicated image
  preprocessing (deskew/contrast/upscaling) or content-based column
  inference -- tracked as follow-up work, not resolved here.
- Turning pages in Recent Disclosures no longer blocks on live Yahoo Finance
  lookups for every not-yet-cached ticker on the page -- profit/loss there
  now only uses already-cached price data (`_annotate_realized_pnl`'s new
  `allow_network` flag), so page loads are consistently fast regardless of
  page depth.

## [1.1.0] - 2026-07-17
### Added
- Optional OCR fallback (via Tesseract) for House PTR disclosures filed as
  scanned images with no text layer, so their trades aren't silently skipped.
  See `backend/pipeline/ocr.py` and the "Optional OCR support" section of
  README.md.
- "Update App" button in Settings that checks GitHub for a newer published
  release, switching to "Update Available" when one is found
  (`backend/update_check.py`, `/api/version/check`).
- App version number, shown in the footer and used to power the update
  check (`backend/version.py`).
- Congress.gov API key wired in for the optional legislator-directory
  source.

## [1.0.0] - 2026-07-16
### Added
- Initial release: congressional trade disclosure tracking (House Clerk +
  Senate eFD primary sources, Stock Watcher JSON fallback), politician
  directory, portfolio performance estimates, notifications, and
  standalone desktop packaging for Windows/macOS/Linux.
