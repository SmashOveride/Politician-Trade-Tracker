# Congressional Stock Trading Tracker

A standalone desktop app that aggregates **publicly available** disclosures of
US politicians' stock trades, committee assignments, and related data into
one searchable, filterable window. Runs entirely on your own machine --
no hosted server, no account, no installer, no admin rights required.

![status](https://img.shields.io/badge/status-working-brightgreen)

## What it does

- **Home screen shows the most recently disclosed trades** (grouped by
  filing date, not the underlying transaction date, since disclosures often
  lag the actual trade by weeks) -- at a glance, who just traded what.
- **Combined Politician Portfolio Performance chart** at the top of the
  home screen: a line chart of all tracked politicians' estimated net
  invested capital over time, colored green/red for overall increase/
  decrease, with a $ / % Change toggle, 1M/3M/6M/YTD/1Y/5Y range buttons,
  optional dashed S&P 500 / NASDAQ comparison lines, and up to 6
  individually-added politician comparison lines. See "About the portfolio
  chart" below for an important note on what this metric actually measures.
- Lists every current member of the US House and Senate: photo, party,
  state, chamber, and committee assignments (with each committee's
  regulated industries/sectors tagged, so you can spot potential
  conflicts of interest).
- Aggregates their disclosed stock trades (purchases, sales, exchanges)
  going back **10 years**, with dollar amount ranges, transaction dates, and
  links back to the original filing.
- Flags stocks that have been disclosed by **more than one** politician,
  sorted most-widely-held first, with each stock's company name, total
  disclosed value, and the full list of (clickable) politicians who
  traded it -- see "Sorting stocks by politician count" below.
- **Party column on the Recent Trades home screen**: a small red "R",
  blue "D", or purple "I" next to each politician's name.
- **Light/dark mode toggle** in the header, remembered across restarts
  (defaults to your OS/browser's preferred color scheme on first launch).
- **Paginated Recent Trades table**: browse 50 trades at a time, with
  Previous/Next buttons above and below the table (plus search and sort,
  which apply across all pages, not just the current one).
- **Reorderable table columns**: drag any column header left/right to put
  columns in whatever order you like. Each table (Recent Trades, a
  politician's trade history, a stock's trade history) remembers its own
  order across restarts. A "Reset Columns" button next to each table's
  search box restores that table's default order at any time.
- **Gold-highlighted rows** flag any trade in a sector overseen by a
  committee the trading politician sits on (e.g. a Financial Services
  Committee member trading a bank stock) -- a common signal used to spot
  potential conflicts of interest. Hover a highlighted row for details.
- **Profit/Loss column and notifications**: every sale shows a green
  ("+") or red ("-") badge with the estimated realized profit or loss and
  percentage return. This is calculated by FIFO-matching each sale against
  that politician's own prior disclosed purchases of the same stock, then
  applying real historical closing prices (via Yahoo Finance) on the
  matched purchase/sale dates -- an estimate, not a guarantee, since exact
  share counts and execution prices are never disclosed. The same
  profit/loss indicator appears in the header's Notifications dropdown for
  any "Notify Me" alert that matched a sale.
- Shows a monthly buy/sell volume chart per politician and per stock,
  with 1M / 3M / 6M / YTD / 1Y / 5Y range toggles.
- One-click "News" button per trade that opens the ticker's **Yahoo
  Finance News** page in a new tab (e.g. `finance.yahoo.com/quote/LHX/news/`).
- A "Refresh Data" button that re-downloads the latest public data on
  demand.
- **Double-click to start, bookmarkable address**: `start.command` (macOS) /
  `start.sh` (Linux) / `start.bat` (Windows) launch the app on a fixed
  local address and open it in your regular web browser, so you can
  bookmark it for one-click access from then on. See "Starting the app"
  below.
- **All downloaded data is cached locally** in a single portable
  `data/politicians.db` file next to wherever you run the app -- close and
  reopen the app and your data is right there instantly, no re-download
  required.
- **Automatic background updates**: in addition to the manual button, the
  app periodically checks for new disclosures on its own (every hour by
  default, configurable in Settings, or fully disable-able) so new filings
  show up without you having to do anything. The app keeps running in the
  background even after you close the browser tab, and the page updates
  itself automatically when new data arrives.
- **Smart, bandwidth-friendly refreshing**: every refresh (manual or
  automatic) uses conditional HTTP requests (ETags/Last-Modified, with a
  content-hash fallback) against each data source, so unchanged sources are
  never re-downloaded or re-processed -- only genuinely new/changed data
  triggers a real update. See "How caching works" below.
- Optional **Settings** panel to plug in a free
  [api.data.gov](https://api.data.gov/signup/) API key, which switches the
  politician directory (name/party/state/photo) over to the official
  Congress.gov API instead of the default community source. Entirely
  optional -- see "Optional Congress.gov data source" below.
- Optional OCR fallback for the small number of House PTR filings that are
  scanned images rather than machine-generated PDFs, so their trades aren't
  silently skipped. Entirely optional -- see "Optional OCR support" below.
- **Update App** button in Settings that checks GitHub for a newer
  published release, switching to "Update Available" when one is found --
  see "Checking for updates" below.

**Important caveat:** all trade data comes from congressional disclosure
filings, which are self-reported, delayed (up to 45 days by law), and only
reported in broad dollar *ranges* rather than exact amounts. "Stocks owned by
multiple politicians" reflects **disclosed transactions**, not verified,
real-time holdings. This app is an aggregation/visualization tool, not
investment advice.

## How caching works

Everything the app downloads is cached to disk in `data/cache/`, alongside
the server's ETag/Last-Modified headers for that download (tracked in the
database's `source_cache` table). On every refresh -- manual or automatic --
each source is checked with a conditional HTTP request first:

- If the server confirms nothing changed (`304 Not Modified`, or, for
  sources that don't support conditional requests, an identical SHA-256
  hash of the downloaded content), the app uses the already-cached copy and
  **skips re-processing it into the database entirely**.
- Only genuinely new/changed data triggers re-parsing and a database write.

This means a refresh where nothing has changed anywhere (the common case)
completes in a couple of seconds instead of the ~30-90 seconds a full
first-time download takes, which is what makes frequent automatic
background checks practical. You can see exactly what happened on the last
refresh in the status messages (e.g. "Senate trades unchanged (8350 on
file)" vs "Loaded 8355 Senate trades (updated)").

## Trade disclosure data collection pipeline

Congressional trade disclosures are collected by a dedicated, resilient
pipeline (`backend/pipeline/`), run as part of every refresh:

1. **Optional custom API source** (Settings > APIs): if you've configured
   and enabled a custom API source (e.g. a paid Finnhub/FMP/Quiver
   Quantitative endpoint), it's tried first -- a ready-made JSON API is
   lighter-weight than downloading and parsing PDFs/HTML. Its response
   shape is auto-detected via a best-effort field-mapping heuristic
   (ticker/symbol, representative/senator/name, transaction date, amount,
   type). If it fails or its shape can't be recognized, the pipeline falls
   back to the bulk sources below.
2. **House Clerk bulk ZIP** (primary, most authoritative): downloads each
   year's Periodic Transaction Report index from
   `disclosures-clerk.house.gov`, then fetches and parses each individual
   filer's PTR PDF (position-based table parsing, not naive text
   regexing, to handle real-world PDF line-wrapping variance).
3. **Senate eFD search** (primary, most authoritative): performs the
   session handshake required by `efdsearch.senate.gov`, searches for
   Periodic Transaction Reports, and parses each report's HTML
   transaction table.
4. **Automatic fallback**: if either primary source is unreachable (site
   down, blocked, maintenance) or its data fails to parse, the pipeline
   automatically falls back to the House/Senate Stock Watcher community
   JSON dumps for that chamber -- the app never simply gives up on a
   chamber's data for the run.

Every source is normalized into one common schema before being written to
the `trades` table, regardless of which source produced it. Additional
resilience built into the pipeline:

- **Format-version detection**: each parser checks that the source's
  structure (the House Clerk index's column header, the PTR PDF's
  transaction table header, the Senate eFD table's column header) still
  matches what the parser expects. A mismatch is treated as an
  unrecognized format (not silently mis-parsed) and logged/alerted rather
  than corrupting the database with misaligned columns.
- **Content-hash based dedup**: every individual filing (a PTR PDF, an eFD
  report page) is content-hashed and tracked in the `processed_filings`
  table, so only new or changed filings are re-downloaded and re-parsed on
  each run -- a previously successful filing is skipped until it changes
  upstream (e.g. an amended disclosure) or a prior parse failure is worth
  retrying.
- **Retries with backoff + caching**: all network calls go through a
  shared HTTP layer (`backend/pipeline/http_client.py`) that automatically
  retries transient failures (connection errors, timeouts, 429/5xx) with
  exponential backoff, and caches responses on disk with conditional
  requests (ETag/Last-Modified, or a content-hash fallback for POST-based
  search APIs) -- the same caching strategy described above, shared across
  both the legacy and pipeline ingestion paths.
- **Parse-failure logging + stale-data alerting**: parse failures,
  unrecognized-format events, and fallback-to-secondary-source events are
  logged to `data/logs/pipeline.log` (rotated at 5MB) and recorded in the
  `pipeline_events` table. Each source's last successful run is tracked in
  `pipeline_source_status`; if a source hasn't succeeded in over 48 hours,
  it's flagged as stale. All of this is surfaced via `GET
  /api/pipeline/status` (sources, stale sources, recent events) for
  monitoring/debugging.

## Trade history retention (10 years) and the Refresh Data window (12 months)

The app keeps a fixed, predictable 10-year window of trade disclosures
(`backend/data_fetch.py`'s `TRADE_HISTORY_YEARS`). The underlying Senate/
House Stock Watcher datasets actually go back further (to ~2012-2013), but
rather than an ever-growing range, ingestion deliberately caps at 10 years
back from today. This is enforced on every refresh (not just when new data
is downloaded), so it also cleans up any older rows left over from before
this limit existed. Rows with a missing/unparsable transaction date are
never discarded, since we can't confirm their age. Adjust
`TRADE_HISTORY_YEARS` in `backend/data_fetch.py` if you'd like a different
retention window.

Separately, clicking **Refresh Data** only fetches/refreshes trades from
roughly the last **12 months** by default (`DEFAULT_REFRESH_LOOKBACK_DAYS`
in `backend/data_fetch.py`) -- this keeps routine refreshes fast, since it's
usually only recent disclosures that have actually changed. To pull older
history (up to the 10-year retention cap above), click the small caret ▾
next to the Refresh Data button, pick a custom start date, and click
**Refresh From Date**. A custom refresh never deletes older data that a
previous deeper refresh already downloaded -- only the automatic 10-year
retention cleanup (above) ever removes old rows.

## About the portfolio chart

The "Combined Politician Portfolio Performance" chart tracks **estimated net
invested capital**, not a verified market valuation. Here's why, and what
that means in practice:

- Disclosure filings report a dollar **range** per transaction (e.g. "$1,001
  - $15,000") -- never a share count, and never an ongoing market price for
  what's still held. There is no free, key-less source for either of those,
  so a true "current market value of all politicians' holdings" figure
  can't be honestly computed.
- What the chart *does* show: a running total of (disclosed purchases minus
  disclosed sales, using the midpoint of each reported range) across all
  tracked politicians -- i.e. whether politicians are collectively putting
  more or less money into the market over time, based on what they've
  actually disclosed. Exchanges are excluded (their net direction isn't
  well-defined). This is fully computed from the app's own trade data, no
  external dependency.
- The main line is colored **green** if the value increased over the
  selected time range, **red** if it decreased.
- The **S&P 500 and NASDAQ** dashed comparison lines use real historical
  index data (Yahoo Finance's public chart endpoint, fetched on demand and
  cached for an hour -- see `backend/market_data.py`). Because an index's
  point value and a dollar figure aren't on the same scale, these (and any
  individual politician comparison lines you add, up to 6 at a time) only
  appear in **% Change** mode, where everything is normalized to percent
  change from the start of the selected range.
- If Yahoo's endpoint is ever unreachable, the S&P 500/NASDAQ checkboxes
  simply won't show data for that line -- the rest of the chart is
  unaffected.

## Starting the app

Double-click **`start.command`** (macOS), **`start.sh`** (Linux), or
**`start.bat`** (Windows) in this folder. That's it -- it works whether or
not you've already built the standalone executable (see below): it prefers
the prebuilt `dist/` app if present, and otherwise runs from source with
Python automatically.

This is safe to double-click any number of times:

- **First time / not currently running**: it starts the app in the
  background and opens your default web browser to it.
- **Already running**: it just opens your browser to the running instance
  -- it never starts a second copy.

The app always listens on the same fixed local address
(`http://127.0.0.1:58732/#/recent`) across restarts, specifically so you can
**bookmark it** in your browser -- after the first launch, you don't need
`start.sh`/`start.bat` at all; just open the bookmark. (If that port happens
to be taken by something else on your machine, the app falls back to any
free port for that session -- rare in practice, and it still works, the
address just won't be stable that one time.)

The app keeps running in the background indefinitely -- including checking
for new disclosures automatically once an hour (see below) -- even after
you close the browser tab. To actually stop it, close the terminal window
it printed its status to (if one is visible), or end the
`PoliticianTradesTracker` / `python run.py` process from your system's task
manager.

> **Linux troubleshooting:** if double-clicking `start.sh` opens it in a
> text editor instead of running it, your file manager may need the
> executable bit set first -- right-click it, check Properties/Permissions
> for "Allow executing file as program", or run `chmod +x start.sh` once
> from a terminal. After that, double-clicking works normally.

> **macOS troubleshooting:** double-clicking a `.command` file normally
> just works (Finder opens it in Terminal automatically). If Gatekeeper
> blocks it as from an "unidentified developer", right-click (or
> Control-click) `start.command` and choose **Open** once, then confirm --
> after that it opens normally on future double-clicks. The same applies to
> the standalone `PoliticianTradesTracker.app` if you build it (see
> below): right-click it and choose **Open** the first time.

## How it works (architecture)

- **Backend**: Python + Flask + SQLite. The whole database is a single
  portable `politicians.db` file created next to wherever you run the app
  (see `data/`), so all downloaded/scraped data persists across restarts --
  closing and reopening the app shows your existing data instantly, with no
  re-download required.
- **Single-instance launcher** (`backend/launcher.py`): on startup, checks a
  fixed local port (and a small lock file in `data/`) to see whether the app
  is already running; if so, it just opens your browser to the existing
  instance instead of starting a second server. Otherwise it starts the
  server and opens your browser to it -- a normal, bookmarkable browser tab,
  not a native app window.
- **Automatic background refresh**: a lightweight scheduler thread (see
  `backend/app.py`) checks periodically (every hour by default) whether
  it's time for another refresh, and kicks one off automatically -- including
  immediately on first launch if there's no data yet. Fully configurable
  (including "Off") from the Settings panel.
- **Frontend**: plain HTML/CSS/JS (no React/build step) with a locally
  vendored copy of Chart.js -- works fully offline once data is loaded.
- **Data ingestion happens at runtime**, on your machine, from these public
  sources:
  - [unitedstates/congress-legislators](https://github.com/unitedstates/congress-legislators)
    -- legislator bios, party, state, committees, committee membership
  - [unitedstates/images](https://github.com/unitedstates/images) --
    official member photos
  - [timothycarambat/senate-stock-watcher-data](https://github.com/timothycarambat/senate-stock-watcher-data)
    -- Senate financial disclosure (PTR) trades
  - House trades: tries the original House Stock Watcher public data dump
    first, and falls back to a community-maintained GitHub mirror if
    that's unreachable from your network (data source availability can
    vary; the app degrades gracefully and tells you what happened if a
    source can't be reached).

## Running it (development mode)

You need Python 3.9+ installed.

```bash
cd politician-trades-app
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

This opens your browser to the app the same way `start.command`/`start.sh`/
`start.bat` does (see "Starting the app" above) -- on first launch it
automatically downloads the initial dataset in the background (takes
roughly 30-90 seconds depending on your connection), or you can click
**Refresh Data** yourself at any time.

## Building a standalone, no-install executable

### macOS

```bash
pip install -r requirements.txt
pyinstaller packaging/macos.spec
```

Output: `dist/PoliticianTradesTracker.app`, a standard macOS app bundle.
Copy the whole project folder (including this `dist/` subfolder and
`start.command`) anywhere -- another Mac, an external drive -- and
double-click `start.command` (or open the `.app` directly). No installer,
no admin rights required.

Since the app isn't code-signed/notarized with an Apple Developer
certificate, macOS Gatekeeper will block the first launch as being from an
"unidentified developer" -- right-click (Control-click)
`PoliticianTradesTracker.app` and choose **Open** once, confirm, and it
opens normally on every subsequent launch (including via `start.command`).
Build on an actual Mac -- PyInstaller does not cross-compile, and Intel
vs. Apple Silicon builds are only guaranteed to run on the architecture
they were built on (build on each if you need to support both).

**Don't have a Mac?** `.github/workflows/build-macos.yml` builds both the
full app and Lite (see below) on a real Mac GitHub provisions in the
cloud -- free for this public repo, no Apple hardware needed on your end.
Trigger it manually from the Actions tab, or just push a `vX.Y.Z` tag (see
"Versioning & releasing updates" below) and it attaches the built `.app`
zips to that release automatically.

### Windows

```powershell
pip install -r requirements.txt
pyinstaller packaging\windows.spec
```

Output: `dist\PoliticianTradesTracker\PoliticianTradesTracker.exe`. Zip up
the whole project folder (including this `dist\PoliticianTradesTracker\`
subfolder and `start.bat`) and it's fully portable -- copy it anywhere (USB
drive, another PC) and double-click `start.bat` (or the `.exe` directly).
No installer, no admin rights, nothing added to the registry.

### Linux

```bash
pip install -r requirements.txt
pyinstaller packaging/linux.spec
```

Output: `dist/PoliticianTradesTracker/PoliticianTradesTracker`. The whole
project folder (including this `dist/PoliticianTradesTracker/` subfolder
and `start.sh`) is portable -- copy it anywhere and double-click
`start.sh` (or run the binary directly), no package manager or root access
needed.

Build once on each target OS you want to support (PyInstaller does not
cross-compile).

### Android (Lite only)

Unlike the three builds above, there's no PyInstaller involved -- the
`android/` folder is a separate Chaquopy-based Android Studio/Gradle project
that embeds a real Python interpreter, runs the exact same Flask backend
on-device against Android's private app storage, and displays the existing
`frontend/` in a WebView. Only the **Lite** data path applies (the prebuilt
snapshot download described above, not the live PDF/OCR pipeline) -- see
`android/README.md` for how it reuses `backend/`/`frontend/` without forking
them.

This is distributed as a **sideloaded APK**, not through the Google Play
Store -- same philosophy as the desktop builds (no account, no installer
beyond "allow this one app," runs entirely on your own device).

**Don't have Android Studio?** `.github/workflows/build-android.yml` builds
the APK entirely on GitHub's own Ubuntu runners (which ship a preinstalled
Android SDK), the same way `build-macos.yml` builds macOS binaries on a
GitHub-hosted Mac. Trigger it manually from the Actions tab for an unsigned
debug APK, or push a `vX.Y.Z` tag (see "Versioning & releasing updates"
below) to also build a signed release APK and attach it to that release.

**One-time signing setup**, needed before the first tagged release (there's
no Play App Signing safety net for a sideloaded app, so back this keystore
up somewhere safe -- losing it breaks update continuity for anyone who
already installed a prior version):

```bash
keytool -genkeypair -v -keystore release.keystore -alias politician-trades \
  -keyalg RSA -keysize 2048 -validity 10000
base64 -w0 release.keystore > release.keystore.b64   # macOS: base64 -i release.keystore
```

Add the contents of `release.keystore.b64`, plus the alias and both
passwords you were prompted for, as four GitHub Actions repo secrets:
`ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`,
`ANDROID_KEY_PASSWORD`.

**Installing the APK:** since it's self-signed rather than Play Store
distributed, Android will very likely show a "Play Protect can't verify this
app" (or similar "unknown developer") warning on install -- this is
expected for any sideloaded app, not a sign of a problem, and there's no
equivalent-cost fix short of enrolling in Google Play. Tap through it the
same way you would for any other sideloaded APK you trust the source of.

## Antivirus / SmartScreen false positives

Like most unsigned PyInstaller-built desktop apps, a freshly built
`PoliticianTradesTracker` executable can occasionally get flagged by
antivirus software or blocked by Windows SmartScreen / macOS Gatekeeper --
almost always a false positive caused by *how the binary was built*, not
anything the app actually does (it makes only the outbound HTTPS requests
documented throughout this README, to the public data sources and GitHub
listed above, and never modifies anything outside its own `data/` folder).
A few things are done to keep this as unlikely as possible:

- **No UPX compression.** All three `packaging/*.spec` files build with
  `upx=False`. UPX-packed executables are one of the most common antivirus
  heuristic triggers there is, since a lot of real malware also uses UPX to
  evade signature-based scanning -- a heuristic engine has no way to tell
  "legitimate app that happens to be UPX-packed" from "malware that happens
  to be UPX-packed" apart from the packing itself. The smaller binary size
  UPX would give isn't worth that risk here.
- **Single-folder builds, not `--onefile`.** A `--onefile` build is itself a
  self-extracting archive that unpacks to a temp directory at every launch --
  a pattern real droppers use too, and one some heuristics flag on sight.
  The `dist/PoliticianTradesTracker/` folder build these spec files produce
  avoids that shape entirely.
- **A real Windows version-info resource.** `packaging/windows.spec`
  generates one on every build (from `backend/version.py`'s `APP_VERSION`,
  so it can't drift) embedding a publisher name, product name, and version
  number into the `.exe`. Legitimate Windows software almost always carries
  this; leaving it blank is one more small, free signal heuristics can
  weigh against an unknown binary.
- **Ordinary, well-known dependencies only** (`requirements.txt`): Flask,
  requests, pdfplumber, etc. -- nothing that itself tends to draw scrutiny
  (obfuscators, packers, process-injection or credential-access libraries).

**What actually fixes this, though, is code signing** -- everything above
only reduces heuristic false-positive *risk*, it doesn't give the binary a
verified identity the way a real signature does. If you're distributing
built executables to other people (rather than everyone building their own
from source, which needs none of this):

- **Windows (Authenticode):** buy a code-signing certificate from any CA
  (DigiCert, Sectigo, etc. -- an "EV" certificate builds SmartScreen
  reputation faster than a standard one but costs more), then sign the
  built `.exe` before zipping it up:
  ```powershell
  signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
    /f your-cert.pfx /p your-cert-password ^
    dist\PoliticianTradesTracker\PoliticianTradesTracker.exe
  ```
  The `/tr` timestamp server matters -- it keeps the signature valid after
  the certificate itself eventually expires.
- **macOS (notarization):** with a paid Apple Developer account,
  `codesign` the built `.app` and submit it to Apple's notary service; see
  [Apple's notarization guide](https://developer.apple.com/documentation/security/notarizing_macos_software_before_distribution).
  Until that's done, Gatekeeper will keep blocking first launch as
  "unidentified developer" -- see the troubleshooting note under "Starting
  the app" above for the one-time right-click-Open workaround.
- **Publish checksums with every release.** Even without paying for a
  certificate, publishing each build's SHA-256 alongside it lets anyone
  verify their download wasn't corrupted or tampered with in transit:
  ```bash
  # macOS/Linux
  shasum -a 256 dist/PoliticianTradesTracker/PoliticianTradesTracker
  # Windows (PowerShell)
  Get-FileHash dist\PoliticianTradesTracker\PoliticianTradesTracker.exe -Algorithm SHA256
  ```
  Paste the resulting hashes into the GitHub Release notes (see
  "Versioning & releasing updates" below) next to each attached file.

None of this is needed to just build and run the app for yourself from
source -- it only matters if you're handing a *built binary* to someone
else and want it to show up as trustworthy as possible on their machine.

## Project layout

```
politician-trades-app/
├── start.command           # <-- double-click this on macOS to launch the app
├── start.sh                # <-- double-click this on Linux to launch the app
├── start.bat               # <-- double-click this on Windows to launch the app
├── run.py                 # dev entry point (python run.py)
├── desktop.py              # packaged-app entry point (used by PyInstaller)
├── requirements.txt
├── CHANGELOG.md             # version history -- see "Versioning & releasing updates" below
├── backend/
│   ├── app.py              # Flask REST API + static file serving
│   ├── launcher.py          # single-instance + fixed-port launch logic (desktop only)
│   ├── android_entry.py     # Android-only bootstrap, called from Kotlin via Chaquopy
│   ├── db.py                # SQLite schema + connection helper
│   ├── data_fetch.py        # runtime ingestion orchestration (legislators, committees, trade pipeline)
│   ├── normalize.py         # shared amount/date/name normalization helpers
│   ├── version.py           # APP_VERSION + GitHub repo -- single source of truth, see below
│   ├── update_check.py      # checks GitHub Releases for a newer version (powers "Update App")
│   ├── pipeline/            # congressional trade disclosure data collection pipeline
│   │   ├── house_clerk.py       # primary source: House Clerk bulk ZIP + PTR PDF parser
│   │   ├── senate_efd.py        # primary source: Senate eFD search + PTR HTML parser
│   │   ├── secondary_sources.py # fallback: House/Senate Stock Watcher JSON dumps
│   │   ├── custom_api_source.py # optional user-configured API, tried first if enabled
│   │   ├── ocr.py               # optional OCR fallback for scanned/unreadable PDFs (Tesseract)
│   │   ├── schema.py            # common normalized trade schema
│   │   ├── dedup.py             # content-hash based filing dedup (processed_filings table)
│   │   ├── monitoring.py        # logging + stale-data/parse-failure alerting
│   │   ├── http_client.py       # retries (backoff) + on-disk caching
│   │   ├── loader.py            # writes normalized trades into the trades table
│   │   └── orchestrator.py      # ties the above together (run_pipeline())
│   ├── market_data.py       # on-demand S&P 500/NASDAQ data for the portfolio chart
│   ├── committees_map.py    # curated committee -> industry/sector tags
│   ├── ticker_sectors.py    # curated ticker -> industry/sector tags
│   ├── settings.py          # local settings.json store (optional API key, custom API sources)
│   └── us_states.py         # state name -> abbreviation mapping
├── frontend/
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   └── vendor/chart.min.js  # Chart.js, vendored locally (no CDN)
├── packaging/
│   ├── macos.spec           # PyInstaller spec for macOS
│   ├── windows.spec         # PyInstaller spec for Windows
│   └── linux.spec           # PyInstaller spec for Linux
├── android/                 # Chaquopy/Gradle project for the sideloaded Android (Lite) APK
│   └── app/
│       ├── build.gradle.kts     # Chaquopy config + syncPythonSources task (reuses backend/frontend as-is)
│       └── src/main/kotlin/com/smashoveride/ptt/
│           ├── MainActivity.kt       # WebView shell
│           └── PythonServerService.kt # foreground service hosting the Flask server
└── data/                    # created at runtime (gitignored)
    ├── politicians.db       # the whole app's data -- persists across restarts
    ├── server_info.json     # records which port the running instance is on
    ├── settings.json        # optional API key + auto-refresh interval
    └── cache/               # raw cached copies of each downloaded source
```

## Optional Congress.gov data source

By default the app needs **no signup or API key at all** -- the politician
directory (name, party, state, photo) comes from the free, public
[unitedstates/congress-legislators](https://github.com/unitedstates/congress-legislators)
project.

If you'd rather source that same directory from the official government API,
click **Settings** in the header and paste in a free API key from
[api.data.gov](https://api.data.gov/signup/) (takes about 30 seconds, just an
email address). On your next **Refresh Data**, the app will use the
[Congress.gov API](https://api.congress.gov/) instead. If the key is missing,
invalid, or the request fails for any reason, the app automatically and
silently falls back to the free community source -- it always works either
way. The header shows which source was used on the last refresh.

This setting **only affects the politician directory** (name/party/state/
photo). Committees, committee membership, and all trade disclosure data
always come from the sources listed above regardless of this setting --
Congress.gov has no public API for financial disclosure/trade data, and its
committee-membership data doesn't map cleanly onto the `thomas_id` scheme
used by `committees_map.py`, so that part of the pipeline is unchanged.

Your key is stored locally in `data/settings.json` (gitignored, never sent
anywhere except as your own credential on your own requests to
api.congress.gov) and is never displayed back in full once saved.

## Optional OCR support

The vast majority of House Clerk PTR (Periodic Transaction Report) filings
are machine-generated PDFs with a normal text layer, which `backend/
pipeline/house_clerk.py` parses directly -- no OCR needed. A small number of
older or unusually-filed PTRs are instead **scanned images** with no text
layer at all, and would otherwise be reported as an unrecognized format and
skipped.

If [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) is available,
the app automatically detects it and falls back to OCR for exactly those
filings, so their trades are recovered instead of skipped. This is
**entirely optional**: everything else in the app works fully without it,
and no Tesseract available is treated exactly the same as before this
feature existed -- the filing is simply reported as an unrecognized format
(see `/api/pipeline/status`) and skipped, with no error shown to the user.

**Packaged Windows builds already include Tesseract** (see
`packaging/fetch_tesseract_windows.py` and `windows.spec`) -- nothing to
install, OCR support just works. This only applies to the Windows build;
running from source, or building for macOS/Linux, still relies on a
system-installed Tesseract (see below).

### Installing Tesseract (running from source, or macOS/Linux)

The Python side (the `pytesseract` package in `requirements.txt`) is
already installed with the rest of the app's dependencies -- only the
Tesseract OCR engine itself (a separate system binary) needs installing:

- **Windows:** Install the [UB Mannheim Tesseract build](https://github.com/UB-Mannheim/tesseract/wiki)
  and make sure `tesseract.exe` is on your `PATH` (the installer offers to
  do this for you). Only needed when running from source -- the packaged
  build above already bundles this.
- **macOS:** `brew install tesseract`
- **Linux (Debian/Ubuntu):** `sudo apt install tesseract-ocr`

No restart or configuration is needed afterward -- the app detects
Tesseract automatically the next time it starts, and **Settings** shows
whether OCR support is currently available.

### Rebuilding the Windows package

`packaging/vendor/tesseract-windows/` (gitignored, not committed -- it's a
~150MB binary blob with its own upstream release cycle) needs staging once
before building:

```
python packaging/fetch_tesseract_windows.py   # requires 7-Zip, one-time
pyinstaller packaging/windows.spec
```

If the vendor folder isn't staged yet, the build still succeeds -- just
without bundled OCR, same as running from source without Tesseract
installed.

## Checking for updates

The **Settings** dropdown in the header includes an **Update App** button,
above **APIs**. It periodically (and on every app launch) checks this
project's [GitHub Releases page](https://github.com/SmashOveride/Politician-Trade-Tracker/releases)
for a newer published version than the one currently running:

- If you're already on the latest version, the button reads **Update App**
  and just opens the releases page (so you can browse release notes or
  manually check any time).
- If a newer version has been published, the button switches to
  **Update Available (vX.Y.Z)** and opens the same page, where you can
  download the new build.

This check is entirely read-only: it never downloads or installs anything
automatically. Applying an update means downloading the latest release the
same way you originally downloaded the app, same as any other portable
desktop app. If GitHub is unreachable (offline, rate-limited, etc.), the
button simply stays as **Update App** -- there is no error shown, since a
failed check just means "nothing new found this time," not "something is
wrong." Checks are cached for an hour server-side so opening Settings
repeatedly doesn't hammer GitHub's API.

## Versioning & releasing updates

This app follows [Semantic Versioning](https://semver.org/)
(`MAJOR.MINOR.PATCH`). The current version lives in a single place:
`backend/version.py`'s `APP_VERSION`. It's surfaced in the UI footer, and
compared against GitHub's published releases to power the "Update App" /
"Update Available" button described above.

To cut a new release:

1. Bump `APP_VERSION` in `backend/version.py` -- `packaging/macos.spec`
   reads it directly for `CFBundleShortVersionString`, so there's no
   second place to update in sync by hand.
2. Add a new entry at the top of `CHANGELOG.md` describing what changed,
   following the existing format (`## [X.Y.Z] - YYYY-MM-DD`, with
   `### Added` / `### Changed` / `### Fixed` subsections as needed).
3. Commit those changes, then tag the commit to match
   (`git tag vX.Y.Z && git push --tags`) -- this triggers both
   `build-macos.yml` and `build-android.yml`.
4. Publish a [GitHub Release](https://github.com/SmashOveride/Politician-Trade-Tracker/releases/new)
   from that tag, pasting in the same changelog entry as the release notes.
   Once the release exists, re-run the two workflows above from the Actions
   tab if they finished before you published it (their "attach to release"
   step needs the release to already exist) -- they'll attach the macOS/Lite
   zips and the signed Android APK automatically. Attach the
   Windows/Linux builds by hand, ideally signed (see "Antivirus /
   SmartScreen false positives" above), with each attached file's SHA-256
   checksum pasted into the release notes either way.

Once the release is published, every running copy of the app will notice
it (within an hour, due to caching) via the "Update App" button turning
into "Update Available" -- no separate announcement needed.

## Editing the committee/ticker -> industry mappings

- `backend/committees_map.py` is a small, hand-curated dict mapping each
  committee's stable `thomas_id` to a list of industry/sector tags (e.g. the
  House Financial Services Committee -> Banking, Insurance, Fintech,
  Cryptocurrency).
- `backend/ticker_sectors.py` is the same idea, but for stock tickers (e.g.
  `JPM` -> Banking, Financial Services). This is what powers the gold
  conflict-highlighting -- a trade is only flagged if its ticker is in this
  file **and** overlaps a sector tag from a committee the politician sits
  on. It currently covers ~490 tickers (the ~500 most-traded tickers in the
  real disclosure dataset, together representing roughly two-thirds of all
  disclosed trades on file); anything not listed is simply never flagged
  (no guessing). Deliberately excludes broad-market index ETFs (SPY, QQQ,
  etc.) since a trade in a total-market fund isn't meaningfully "in" any
  one committee's oversight sector.

Both are plain Python dicts and safe to edit/extend -- no other code needs
to change, just add/adjust entries and click Refresh Data (or restart) to
see the change.

## Sorting stocks by politician count

The Stocks page (and the `/api/stocks/sorted` endpoint behind it) is powered
by `sort_stocks_by_politician_count()` in `backend/app.py`. It:

1. Takes the full list of stocks disclosed in the dataset.
2. Counts how many unique politicians disclosed a trade in each one.
3. Sorts them from most to least widely held (ties broken by total
   disclosed dollar value).
4. Returns, per stock: ticker, a best-effort company name (the most
   commonly disclosed asset description for that ticker -- there's no
   separate curated company-name field), the politician count, trade
   count, total disclosed value, and the full list of politicians who
   traded it (each clickable through to their detail page).

By default the UI's Stocks page only shows tickers held by 2+ politicians
(matching its original "owned by more than one politician" framing), but
the function and endpoint support the full unfiltered list too:

```
GET /api/stocks/sorted                          # every ticker, most-held first
GET /api/stocks/sorted?min_politicians=2         # only multi-politician tickers (what the UI uses)
GET /api/stocks/sorted?include_politicians=false # lighter response, omits the per-ticker politician list
GET /api/stocks/sorted?limit=50                  # cap how many rows come back (default 500)
```

## Reordering table columns

Drag any column header left or right in the Recent Trades, politician
detail, or stock detail tables to put columns in whatever order you
prefer. Each of those three tables remembers its own order independently
(stored in your browser's local storage), so it persists across restarts.
Click the "Reset Columns" button next to that table's search box at any
time to restore its default order.

## Known limitations

- Trade-to-politician name matching uses fuzzy last-name/first-name
  matching against both current and historical member lists. It resolves
  correctly for ~99% of disclosures in testing, but a handful of unusual
  name formats may not link to a specific bioguide ID (they'll still show
  up in the raw trades list under their disclosed name).
- The House Clerk and Senate eFD sites are occasionally slow, rate-limited,
  or briefly down for maintenance (the Senate eFD site in particular has
  scheduled maintenance windows). The pipeline automatically falls back to
  the House/Senate Stock Watcher community JSON dumps in that case, and
  reports which source (primary or fallback) was used after each refresh
  -- see "Trade disclosure data collection pipeline" above.
- Disclosure filings only report *ranges* (e.g. "$1,001 - $15,000"), so all
  volume figures use the midpoint of the disclosed range, not an exact
  dollar amount.
- "As soon as they're published" is bounded by how often the House Clerk /
  Senate eFD sites themselves publish new filings (typically within a day
  or so of filing), not literally the moment a member of Congress files a
  PTR. The automatic background check (default: every hour) is how
  quickly this app notices once a new filing is published upstream.
- The automatic background refresh still requires an internet connection
  at check time; if your machine is offline, it simply fails quietly and
  tries again next interval -- your cached data is unaffected either way.
- The party column (R/D/I) only shows for trades that resolved to a
  current officeholder (same matching described above) -- it's simply
  blank for the rare disclosure that didn't resolve to a bioguide ID,
  rather than guessing.
- The "News" button builds a standard Yahoo Finance Historical Data URL
  (`finance.yahoo.com/quote/{TICKER}/history/`) from the trade's ticker and
  date -- it doesn't verify the ticker actually exists on Yahoo Finance, so
  an unusual/delisted ticker may land on an error page there.
