/* =========================================================================
 * Politician Trades Tracker — frontend application logic
 * -------------------------------------------------------------------------
 * Vanilla ES6+, zero build step. Served as a static file by the local
 * Flask backend and rendered in the user's regular web browser (see
 * backend/launcher.py for how the app opens itself on a fixed,
 * bookmarkable local address).
 *
 * Sections in this file:
 *   1. Global state
 *   1b. Theme (light/dark mode)
 *   2. Generic utility helpers (fetch, debounce, formatting, escaping)
 *   3. Hash-based router
 *   4. Header / global search / refresh handling
 *   4b. Recent Trades view (home / landing page)
 *   5. Politicians list view
 *   6. Politician detail view
 *   7. Stocks list view
 *   8. Stock detail view
 *   9. Shared "trades table" component (search + sort + News button)
 *  10. Chart.js helpers (volume chart + time range buttons)
 *  11. Notify modal (per politician + stock alert preference)
 *  12. News button handler / toast
 *  13. App bootstrap
 * ========================================================================= */

/* ---------------------------------------------------------------------
 * 1. Global state
 * ------------------------------------------------------------------- */

// Current filter selections for the Politicians view. Kept outside the
// DOM so the global search box and the in-view filters can stay in sync.
const filterState = {
  party: '',
  state: '',
  chamber: '',
  search: '',
};

// Current filter selections for the Recent Trades view's left-rail facet
// panel (see renderRecentFiltersPanel). Kept outside the DOM, like
// filterState above, so pagination and filter-change handlers can both read
// the live selection without re-scraping every checkbox on every request.
const recentFiltersState = {
  startDate: '',
  endDate: '',
  types: [], // transaction_type values: purchase, sale, sale_partial, exchange
  parties: [], // Democrat, Republican, Independent
  amountBuckets: [], // bucket-floor dollar values, see AMOUNT_BUCKETS
  search: '',
};

// Standard STOCK Act disclosure amount buckets. `floor` must match a value
// in backend/app.py's RECENT_TRADES_AMOUNT_BUCKET_FLOORS exactly -- kept in
// sync by hand since amount_min is stored as that bucket's floor dollar
// value regardless of any OCR garbling in the bucket's display text.
const AMOUNT_BUCKETS = [
  { floor: 1001, label: '$1,001 – $15,000' },
  { floor: 15001, label: '$15,001 – $50,000' },
  { floor: 50001, label: '$50,001 – $100,000' },
  { floor: 100001, label: '$100,001 – $250,000' },
  { floor: 250001, label: '$250,001 – $500,000' },
  { floor: 500001, label: '$500,001 – $1,000,000' },
  { floor: 1000001, label: '$1,000,001 – $5,000,000' },
  { floor: 5000001, label: '$5,000,001 – $25,000,000' },
  { floor: 25000001, label: '$25,000,001 – $50,000,000' },
  { floor: 50000000, label: 'Over $50,000,000' },
];

// Client-side sort mode for the Politicians view's card grid (the list
// itself always comes back from the API in alphabetical order -- see
// list_politicians()'s "ORDER BY last_name, first_name" -- so re-sorting
// by trade_count is just a local array sort, no extra request needed).
// Cycles: name -> most-active-first -> least-active-first -> back to name.
const POLITICIANS_SORT_CYCLE = ['name', 'active_desc', 'active_asc'];
const POLITICIANS_SORT_LABELS = {
  name: 'Sort by Trading Activity',
  active_desc: 'Trading Activity: Most Active ▼',
  active_asc: 'Trading Activity: Least Active ▲',
};
let politiciansSortMode = 'name';

function sortPoliticiansList(list) {
  if (politiciansSortMode === 'name') return list; // already alphabetical from the API
  const sorted = list.slice();
  const dir = politiciansSortMode === 'active_desc' ? -1 : 1;
  sorted.sort((a, b) => dir * ((a.trade_count ?? 0) - (b.trade_count ?? 0)));
  return sorted;
}

// Chart.js instance currently on screen. Only one chart is ever visible
// at a time (politician detail OR stock detail), so we keep a single
// reference and destroy it before drawing a new one to avoid leaks.
let currentChartInstance = null;

// Separate instance for the Combined Politician Portfolio Performance chart
// on the Recent Trades home view (a different chart from the one above).
let portfolioChartInstance = null;

// Handle for the setInterval() used while polling /api/refresh/status.
let refreshPollTimer = null;

// Bounds for the custom "refresh since" date picker (see the caret dropdown
// next to the Refresh Data button), populated from /api/meta. The default
// is the normal 12-month lookback window; the min is the app's overall
// 10-year retention cap (see backend/data_fetch.py).
let refreshSinceDefaultDate = null;
let refreshSinceMinDate = null;

// The custom start date the user picked (ISO 'YYYY-MM-DD'), or null to use
// the default 12-month window. Cleared back to null after a successful
// custom refresh so subsequent plain clicks on "Refresh Data" go back to
// the default behavior.
let refreshSinceDate = null;

// Most recent /api/search response, kept so keyboard/click handlers on the
// header search dropdown don't need to re-fetch to know what's showing.
let globalSearchResultsCache = { politicians: [], stocks: [] };

/* ---------------------------------------------------------------------
 * 1b. Theme (light/dark mode)
 * ------------------------------------------------------------------- */

const THEME_STORAGE_KEY = 'theme';

/** Applies `theme` ('light' or 'dark') to the document and updates the
 * toggle button's icon/label, without touching localStorage (see
 * setTheme() for the version that also persists the choice). */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const icon = document.getElementById('theme-toggle-icon');
  const label = document.getElementById('theme-toggle-label');
  if (icon) icon.textContent = theme === 'dark' ? '☀️' : '🌙';
  if (label) label.textContent = theme === 'dark' ? 'Light Mode' : 'Dark Mode';
}

/** Applies `theme` and remembers it in localStorage for next time. */
function setTheme(theme) {
  applyTheme(theme);
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch (_) {
    /* localStorage unavailable -- theme still applies for this session */
  }
}

/** Reads the saved theme (falling back to the OS/browser's preferred color
 * scheme, then light) and applies it. Called once on startup, before the
 * rest of the UI renders, to avoid a flash of the wrong theme. */
function initTheme() {
  let saved = null;
  try {
    saved = localStorage.getItem(THEME_STORAGE_KEY);
  } catch (_) {
    /* localStorage unavailable -- fall back below */
  }
  if (saved !== 'light' && saved !== 'dark') {
    const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    saved = prefersDark ? 'dark' : 'light';
  }
  applyTheme(saved);
}

/** Flips the current theme and re-renders whatever view is on screen, so
 * any already-drawn Chart.js canvases (which bake their colors in at
 * creation time, not live via CSS) get redrawn with theme-appropriate
 * colors too. */
function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  setTheme(next);
  router();
}

/** Grid/tick colors for Chart.js canvases, which bake colors in at creation
 * time (they don't read CSS variables live), so chart-drawing code needs
 * to ask for the right set explicitly based on the current theme. */
function chartThemeColors() {
  const isDark = document.documentElement.dataset.theme === 'dark';
  return {
    grid: isDark ? 'rgba(255,255,255,0.08)' : '#eef1f5',
    tick: isDark ? '#94a3b8' : '#666',
  };
}

// Handle for the toast auto-hide timeout.
let toastTimer = null;

// Last `last_updated` timestamp we've seen from /api/meta, used by the
// background watcher below to detect when an automatic (scheduled)
// background refresh has completed so the UI can update itself silently,
// without the user needing to click "Refresh Data".
let lastKnownUpdate = null;

/* ---------------------------------------------------------------------
 * 2. Generic utility helpers
 * ------------------------------------------------------------------- */

/** Fetch JSON from a URL, throwing a helpful Error on non-2xx responses. */
async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body && body.error) message = body.error;
    } catch (_) {
      /* response wasn't JSON; keep default message */
    }
    throw new Error(message);
  }
  return res.json();
}

/** Returns a debounced version of `fn` that waits `delay` ms after the last call. */
function debounce(fn, delay) {
  let timer = null;
  return function debounced(...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), delay);
  };
}

/**
 * Simplifies a committee/subcommittee name for display, e.g.
 * "House Committee on Energy and Commerce - Communications and Technology"
 * becomes "Energy and Commerce". Strips any subcommittee suffix (after " - "),
 * the chamber/type prefix ("House/Senate/Joint [Select/Special/Permanent
 * Select] Committee on "), and a leading "the ". Falls back to the original
 * name when it doesn't match the expected pattern (e.g. "Joint Economic
 * Committee").
 */
function simplifyCommitteeName(name) {
  if (!name) return '';
  let base = name.split(' - ')[0].trim();
  base = base.replace(/^(House|Senate|Joint)\s+(Permanent Select|Select|Special)?\s*Committee on\s+/i, '');
  base = base.replace(/^the\s+/i, '');
  return base;
}

/** Escapes a value for safe insertion into innerHTML strings. */
function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Formats a dollar amount compactly, e.g. $1.2M / $45.3K. */
function formatMoney(amount) {
  if (amount === null || amount === undefined || isNaN(amount)) return '—';
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(amount);
}

/** Formats a dollar amount in full, for tooltips. */
function formatMoneyFull(amount) {
  if (amount === null || amount === undefined || isNaN(amount)) return '$0';
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(amount);
}

/** Formats a YYYY-MM-DD date string as "Jan 5, 2024"; passes through if unparsable. */
function formatDate(dateStr) {
  if (!dateStr) return '—';
  const d = new Date(`${dateStr}T00:00:00`);
  if (isNaN(d.getTime())) return escapeHtml(dateStr);
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}

/** Formats a duration in seconds as a short human string, e.g. "45s",
 * "3m 20s", or "1h 5m". Used for the refresh progress bar's ETA. */
function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

/** Turns an ISO timestamp into a friendly relative string like "Updated 2h ago". */
function timeAgo(isoString) {
  if (!isoString) return 'Never updated';
  const then = new Date(isoString);
  if (isNaN(then.getTime())) return 'Unknown';
  const diffSec = Math.round((Date.now() - then.getTime()) / 1000);
  if (diffSec < 5) return 'Updated just now';
  if (diffSec < 60) return `Updated ${diffSec}s ago`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `Updated ${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `Updated ${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  if (diffDay < 30) return `Updated ${diffDay}d ago`;
  return `Updated ${then.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })}`;
}

/** Returns a color-coded party badge, tolerant of full names or single-letter codes. */
function partyBadge(party) {
  const p = (party || '').trim().toLowerCase();
  if (p.startsWith('d')) return `<span class="badge badge-dem">Democrat</span>`;
  if (p.startsWith('r')) return `<span class="badge badge-rep">Republican</span>`;
  if (!p) return `<span class="badge badge-ind">Unknown</span>`;
  return `<span class="badge badge-ind">${escapeHtml(party)}</span>`;
}

/** Normalizes chamber codes ('rep'/'sen'/'house'/'senate') into display labels. */
function chamberLabel(chamber) {
  const c = (chamber || '').trim().toLowerCase();
  if (c === 'rep' || c === 'house') return 'House';
  if (c === 'sen' || c === 'senate') return 'Senate';
  if (!c) return '—';
  return c.charAt(0).toUpperCase() + c.slice(1);
}

/** Renders a small colored profit/loss badge for a sale trade's estimated
 * realized gain/loss (see backend's _annotate_realized_pnl), e.g.
 * "+$1.2K (14.3%)" in green or "-$340 (-8.1%)" in red. Returns an em dash
 * for purchases/exchanges or sales with no matched prior purchase to
 * compare against (profit_loss is null in that case -- never guessed). */
function profitLossBadge(trade) {
  const isSale = ['sale', 'sale_partial'].includes((trade.transaction_type || '').trim().toLowerCase());
  if (!isSale) return '<span class="pnl-na">—</span>';
  const pnl = trade.profit_loss;
  const pnlPct = trade.profit_loss_pct;
  if (pnl === null || pnl === undefined) {
    return '<span class="pnl-na" title="No prior disclosed purchase of this stock by this politician to compare against">—</span>';
  }
  const isProfit = pnl >= 0;
  const sign = isProfit ? '+' : '-';
  const cls = isProfit ? 'pnl-profit' : 'pnl-loss';
  const amountStr = formatMoney(Math.abs(pnl));
  const pctStr = pnlPct === null || pnlPct === undefined ? '' : ` (${sign}${Math.abs(pnlPct).toFixed(1)}%)`;
  return `<span class="badge ${cls}" title="Estimated ${isProfit ? 'profit' : 'loss'} based on this politician's disclosed trade history and historical market prices">${sign}${amountStr}${pctStr}</span>`;
}

/** Renders a color-coded transaction type badge. */
function transactionTypeBadge(type) {
  const t = (type || '').trim().toLowerCase();
  if (t === 'purchase') return `<span class="badge badge-buy">Purchase</span>`;
  if (t === 'sale') return `<span class="badge badge-sell">Sale</span>`;
  if (t === 'sale_partial') return `<span class="badge badge-sell">Partial Sale</span>`;
  if (t === 'exchange') return `<span class="badge badge-other">Exchange</span>`;
  return `<span class="badge badge-other">${escapeHtml(type || 'Unknown')}</span>`;
}

/**
 * Renders a photo <img> with a graceful initials-avatar fallback if the
 * image 404s or photo_url is missing entirely.
 */
function renderAvatar(photoUrl, firstName, lastName) {
  const initials = `${(firstName || '')[0] || ''}${(lastName || '')[0] || ''}`.toUpperCase() || '?';
  const altText = escapeHtml(`${firstName || ''} ${lastName || ''}`.trim() || 'Politician');
  if (photoUrl) {
    return `
      <img class="avatar" src="${escapeHtml(photoUrl)}" alt="${altText} photo"
           onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
      <div class="avatar-fallback" style="display:none">${escapeHtml(initials)}</div>`;
  }
  return `<div class="avatar-fallback">${escapeHtml(initials)}</div>`;
}

/* ---------------------------------------------------------------------
 * 3. Hash-based router
 * ------------------------------------------------------------------- */

/** Splits location.hash (e.g. "#/politicians/A000360") into decoded path segments.
 * Any trailing "?query=string" (e.g. "#/politicians/A000360?trade=123", used by
 * notification links to point at a specific trade row) is stripped from the
 * path first -- see hashQueryParam() to read it back out. */
function parseHash() {
  const raw = (location.hash || '#/recent').replace(/^#\/?/, '').split('?')[0];
  return raw.split('/').filter(Boolean).map((seg) => {
    try {
      return decodeURIComponent(seg);
    } catch (_) {
      return seg;
    }
  });
}

/** Reads a single query-string parameter appended to the current location.hash
 * (e.g. "trade" from "#/politicians/A000360?trade=123"), or null if absent. */
function hashQueryParam(name) {
  const raw = location.hash || '';
  const queryIdx = raw.indexOf('?');
  if (queryIdx === -1) return null;
  return new URLSearchParams(raw.slice(queryIdx + 1)).get(name);
}

/** Navigates to a new hash route, re-running the router even if the hash is unchanged. */
function navigateTo(hash) {
  if (location.hash === hash) {
    router();
  } else {
    location.hash = hash;
  }
}

function hideAllViews() {
  document.querySelectorAll('.view').forEach((el) => el.classList.add('hidden'));
}

function showView(id) {
  document.getElementById(id).classList.remove('hidden');
}

function setActiveNav(section) {
  document.querySelectorAll('.nav-link[data-route]').forEach((btn) => {
    const route = btn.dataset.route.replace('#/', '');
    btn.classList.toggle('active', route === section);
  });
}

/** Main router: reads location.hash and shows/loads the matching view. */
function router() {
  const [section, param] = parseHash();

  hideAllViews();
  setActiveNav(section || 'recent');

  if (!section || section === 'recent') {
    showView('view-recent');
    renderRecentView();
  } else if (section === 'politicians') {
    if (param) {
      showView('view-politician-detail');
      renderPoliticianDetail(param, hashQueryParam('trade'));
    } else {
      showView('view-politicians');
      syncFilterInputs();
      renderPoliticiansView();
    }
  } else if (section === 'stocks') {
    if (param) {
      showView('view-stock-detail');
      renderStockDetail(param);
    } else {
      showView('view-stocks');
      renderStocksView();
    }
  } else {
    // Unknown route — fall back to the default view.
    navigateTo('#/recent');
  }
}

/* ---------------------------------------------------------------------
 * 4. Header / global search / refresh handling
 * ------------------------------------------------------------------- */

function syncFilterInputs() {
  document.getElementById('filter-party').value = filterState.party;
  document.getElementById('filter-state').value = filterState.state;
  document.getElementById('filter-chamber').value = filterState.chamber;
  document.getElementById('filter-search').value = filterState.search;
}

/**
 * Header search dropdown (politicians + stocks). Queries /api/search and
 * renders up to two result groups; clicking a result navigates straight to
 * that politician's or stock's detail page. Distinct from the in-view
 * "Search by name…" filter box on the Politicians view (filterState.search
 * / #filter-search), which only ever filtered politicians and still does.
 */
function globalSearchResultItemHTML(kind, item) {
  if (kind === 'politician') {
    return `
      <button type="button" class="global-search-result" data-kind="politician" data-bioguide="${escapeHtml(
        item.bioguide_id
      )}">
        <span class="global-search-result__title">${escapeHtml(item.full_name)}</span>
        <span class="global-search-result__meta">${escapeHtml(
          [item.party, item.state, chamberLabel(item.chamber)].filter(Boolean).join(' · ')
        )}</span>
      </button>`;
  }
  return `
    <button type="button" class="global-search-result" data-kind="stock" data-ticker="${escapeHtml(item.ticker)}">
      <span class="global-search-result__title"><strong>${escapeHtml(item.ticker)}</strong>${
    item.company_name ? ` &mdash; ${escapeHtml(item.company_name)}` : ''
  }</span>
      <span class="global-search-result__meta">${item.politician_count} politician${
    item.politician_count === 1 ? '' : 's'
  } · ${item.trade_count} trade${item.trade_count === 1 ? '' : 's'}</span>
    </button>`;
}

function renderGlobalSearchResults(results) {
  const panel = document.getElementById('global-search-results');
  const { politicians, stocks } = results;

  if (!politicians.length && !stocks.length) {
    panel.innerHTML = `<div class="global-search-empty">No politicians or stocks match “${escapeHtml(
      results.query
    )}”.</div>`;
    panel.classList.remove('hidden');
    return;
  }

  panel.innerHTML = `
    ${
      politicians.length
        ? `<div class="global-search-group">
            <div class="global-search-group__label">Politicians</div>
            ${politicians.map((p) => globalSearchResultItemHTML('politician', p)).join('')}
          </div>`
        : ''
    }
    ${
      stocks.length
        ? `<div class="global-search-group">
            <div class="global-search-group__label">Stocks</div>
            ${stocks.map((s) => globalSearchResultItemHTML('stock', s)).join('')}
          </div>`
        : ''
    }`;

  panel.querySelectorAll('.global-search-result').forEach((btn) => {
    btn.addEventListener('click', () => {
      closeGlobalSearchResults();
      if (btn.dataset.kind === 'politician') {
        navigateTo(`#/politicians/${encodeURIComponent(btn.dataset.bioguide)}`);
      } else {
        navigateTo(`#/stocks/${encodeURIComponent(btn.dataset.ticker)}`);
      }
    });
  });

  panel.classList.remove('hidden');
}

async function runGlobalSearch(query) {
  const panel = document.getElementById('global-search-results');
  if (!query.trim()) {
    closeGlobalSearchResults();
    return;
  }
  try {
    const results = await fetchJSON(`/api/search?q=${encodeURIComponent(query)}`);
    globalSearchResultsCache = results;
    renderGlobalSearchResults(results);
  } catch (err) {
    panel.innerHTML = `<div class="global-search-empty">Search failed: ${escapeHtml(err.message)}</div>`;
    panel.classList.remove('hidden');
  }
}

function closeGlobalSearchResults() {
  document.getElementById('global-search-results').classList.add('hidden');
}

async function populateFilterOptions() {
  try {
    const opts = await fetchJSON('/api/filters/options');
    const partySel = document.getElementById('filter-party');
    const stateSel = document.getElementById('filter-state');
    const chamberSel = document.getElementById('filter-chamber');

    (opts.parties || []).forEach((p) => partySel.appendChild(new Option(p, p)));
    (opts.states || []).forEach((s) => stateSel.appendChild(new Option(s, s)));
    (opts.chambers || []).forEach((c) => chamberSel.appendChild(new Option(chamberLabel(c), c)));
  } catch (err) {
    console.error('Failed to load filter options:', err);
  }
}

async function updateMeta() {
  try {
    const meta = await fetchJSON('/api/meta');
    document.getElementById('last-updated').textContent = timeAgo(meta.last_updated);
    const sourceEl = document.getElementById('legislator-source');
    sourceEl.textContent = meta.legislator_source ? `Directory: ${meta.legislator_source}` : '';
    if (meta.last_updated) lastKnownUpdate = meta.last_updated;
    if (meta.default_refresh_since_date) refreshSinceDefaultDate = meta.default_refresh_since_date;
    if (meta.min_refresh_since_date) refreshSinceMinDate = meta.min_refresh_since_date;
    syncRefreshSinceDateInput();
    if (meta.app_version) {
      const versionEl = document.getElementById('app-version-footer');
      if (versionEl) versionEl.textContent = `• v${meta.app_version}`;
    }
    if (meta.refreshing) {
      setRefreshingUI(true, meta.last_message);
      pollRefreshStatus();
    }
  } catch (err) {
    document.getElementById('last-updated').textContent = 'Last updated: unknown';
  }
}

/**
 * Cached data is loaded instantly on startup (see updateMeta above -- no
 * manual refresh is ever required to see previously downloaded data). This
 * watcher additionally detects when the automatic background scheduler
 * (see backend/app.py's _auto_refresh_loop) has downloaded new disclosures
 * on its own, so the currently open view updates itself without the user
 * needing to click anything.
 */
function startBackgroundMetaWatcher() {
  setInterval(async () => {
    try {
      const meta = await fetchJSON('/api/meta');
      if (meta.refreshing) {
        if (!refreshPollTimer) {
          setRefreshingUI(true, meta.last_message);
          pollRefreshStatus();
        }
        return;
      }
      if (meta.last_updated && lastKnownUpdate && meta.last_updated !== lastKnownUpdate) {
        lastKnownUpdate = meta.last_updated;
        document.getElementById('last-updated').textContent = timeAgo(meta.last_updated);
        router(); // Silently reload whatever view is currently visible with the new data.
        showToast('New disclosures downloaded automatically.');
      } else if (meta.last_updated) {
        lastKnownUpdate = meta.last_updated;
      }
    } catch (_) {
      /* transient network hiccup -- ignore, the next tick will retry */
    }
  }, 45000);
}

function setRefreshingUI(isRefreshing, message) {
  const btn = document.getElementById('refresh-btn');
  const spinner = document.getElementById('refresh-spinner');
  const label = document.getElementById('refresh-btn-label');
  const resetBtn = document.getElementById('refresh-range-reset-btn');
  const applyBtn = document.getElementById('refresh-range-apply-btn');
  const stopBtn = document.getElementById('refresh-range-stop-btn');
  const clearBtn = document.getElementById('refresh-range-clear-btn');

  btn.disabled = isRefreshing;
  // Note: the range dropdown's caret button itself (refresh-range-btn)
  // stays enabled even while refreshing, so the user can still open it to
  // click "Stop Refresh" -- only the actions that don't make sense mid-
  // refresh (starting another date-range refresh, clearing data out from
  // under an in-progress write) are disabled below.
  if (resetBtn) resetBtn.disabled = isRefreshing;
  if (applyBtn) applyBtn.disabled = isRefreshing;
  if (stopBtn) stopBtn.disabled = !isRefreshing;
  if (clearBtn) clearBtn.disabled = isRefreshing;
  spinner.classList.toggle('hidden', !isRefreshing);
  label.textContent = isRefreshing ? 'Refreshing…' : 'Refresh Data';
  showRefreshBanner(isRefreshing, message);
}

function showRefreshBanner(visible, text, progress) {
  const banner = document.getElementById('refresh-banner');
  const textEl = document.getElementById('refresh-banner-text');
  const statsEl = document.getElementById('refresh-banner-stats');
  const track = document.getElementById('refresh-progress-track');
  const bar = document.getElementById('refresh-progress-bar');

  banner.classList.toggle('hidden', !visible);
  if (visible && text) textEl.textContent = text;

  const hasPercent = visible && progress && typeof progress.percent === 'number';
  track.classList.toggle('hidden', !hasPercent);
  if (hasPercent) {
    const pct = Math.round(progress.percent);
    bar.style.width = `${pct}%`;

    const parts = [`${pct}%`];
    if (typeof progress.eta_seconds === 'number') {
      parts.push(progress.eta_seconds > 0 ? `~${formatDuration(progress.eta_seconds)} left` : 'almost done');
    }
    statsEl.textContent = parts.join(' · ');
    statsEl.classList.remove('hidden');
  } else {
    statsEl.classList.add('hidden');
  }
}

/**
 * Starts a refresh. By default (no argument) this uses the backend's
 * standard 12-month trade lookback window. Passing `sinceDate` (an ISO
 * 'YYYY-MM-DD' string from the custom start-date picker in the header's
 * range dropdown) overrides that with a further-back start date for this
 * refresh only -- see the dropdown next to the "Refresh Data" button.
 */
async function startRefresh(sinceDate) {
  try {
    const res = await fetch('/api/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sinceDate ? { since_date: sinceDate } : {}),
    });
    const body = await res.json().catch(() => ({}));
    if (res.status === 409 || body.status === 'already_running') {
      showToast('A refresh is already running…');
    } else {
      setRefreshingUI(true, sinceDate ? `Starting refresh from ${sinceDate}…` : 'Starting refresh…');
      pollRefreshStatus();
    }
  } catch (err) {
    showToast('Failed to start refresh. Is the backend running?');
  }
}

function pollRefreshStatus() {
  clearInterval(refreshPollTimer);
  refreshPollTimer = setInterval(async () => {
    try {
      const status = await fetchJSON('/api/refresh/status');
      const stopBtn = document.getElementById('refresh-range-stop-btn');
      if (stopBtn) stopBtn.disabled = !status.refreshing || !!status.stopping;
      const bannerText = status.stopping ? 'Stopping refresh…' : (status.last_message || 'Refreshing…');
      showRefreshBanner(status.refreshing, bannerText, status.progress);
      if (!status.refreshing) {
        clearInterval(refreshPollTimer);
        setRefreshingUI(false);
        await updateMeta();
        router(); // Reload whatever view is currently visible with fresh data.
        showToast(status.last_message || 'Refresh complete.');
      }
    } catch (err) {
      clearInterval(refreshPollTimer);
      setRefreshingUI(false);
      showToast('Lost connection while refreshing.');
    }
  }, 2000);
}

/**
 * Stops the currently-running refresh (see the "Stop Refresh" item in the
 * dropdown next to "Refresh Data"). Whatever data was already loaded
 * before the stop takes effect is kept -- this never rolls back
 * already-written rows, it just stops fetching/parsing further filings.
 */
async function stopRefresh() {
  closeRefreshRangeMenu();
  try {
    const res = await fetch('/api/refresh/stop', { method: 'POST' });
    const body = await res.json().catch(() => ({}));
    if (res.status === 409 || body.status === 'not_running') {
      showToast('No refresh is currently running.');
    } else {
      showToast('Stopping refresh…');
    }
  } catch (err) {
    showToast('Failed to stop refresh. Is the backend running?');
  }
}

/**
 * Wipes all downloaded/derived data (politicians, committees, trades,
 * notifications, pipeline caches) after user confirmation -- see the
 * "Clear All Data" item in the dropdown next to "Refresh Data". Settings
 * (API keys, custom sources, auto-refresh interval) are left untouched.
 * Refused by the backend while a refresh is in progress (stop it first).
 */
async function clearAllData() {
  closeRefreshRangeMenu();
  if (!window.confirm(
    'Clear ALL downloaded data (politicians, trades, notifications)? '
    + 'This cannot be undone -- you will need to Refresh Data again afterward.'
  )) return;
  try {
    await fetchJSON('/api/data/clear', { method: 'POST' });
    showToast('All data cleared.');
    await updateMeta();
    router(); // Reload whatever view is currently visible, now showing empty state.
  } catch (err) {
    showToast(`Failed to clear data: ${err.message}`);
  }
}

let apisPanelSourcesCache = null;

function apiSourceRowHTML(source) {
  const checked = source.enabled ? ' checked' : '';
  const nameHtml = source.url
    ? `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener">${escapeHtml(source.name)}</a>`
    : escapeHtml(source.name);
  return `
    <div class="api-source-row" data-source-id="${escapeHtml(source.id)}">
      <label class="source-toggle">
        <input type="checkbox" class="source-toggle-input"${checked} />
        <span class="source-toggle-slider"></span>
      </label>
      <div class="api-source-row__body">
        <div class="api-source-row__name">${nameHtml}</div>
        <div class="api-source-row__desc">${escapeHtml(source.description || '')}</div>
      </div>
    </div>`;
}

function customSourceRowHTML(source) {
  const checked = source.enabled ? ' checked' : '';
  const keyNote = source.api_key_set ? ` · key ${escapeHtml(source.api_key_masked)}` : ' · no key set';
  return `
    <div class="api-source-row" data-source-id="${escapeHtml(source.id)}">
      <label class="source-toggle">
        <input type="checkbox" class="source-toggle-input"${checked} />
        <span class="source-toggle-slider"></span>
      </label>
      <div class="api-source-row__body">
        <div class="api-source-row__name">${escapeHtml(source.name)}</div>
        <div class="api-source-row__desc">${escapeHtml(source.endpoint_url)}${keyNote}</div>
      </div>
      <button type="button" class="api-source-row__remove" title="Remove custom source">&times;</button>
    </div>`;
}

function renderApisPanel() {
  const scroll = document.getElementById('apis-panel-scroll');
  if (!apisPanelSourcesCache) {
    scroll.innerHTML = '<p class="loading-text">Loading sources…</p>';
    return;
  }
  const { builtin_sources: builtinSources, custom_sources: customSources } = apisPanelSourcesCache;

  const categories = [];
  builtinSources.forEach((source) => {
    let bucket = categories.find((c) => c.name === source.category);
    if (!bucket) {
      bucket = { name: source.category, sources: [] };
      categories.push(bucket);
    }
    bucket.sources.push(source);
  });

  let html = categories
    .map(
      (cat) => `
      <h4 class="apis-category-heading">${escapeHtml(cat.name)}</h4>
      ${cat.sources.map(apiSourceRowHTML).join('')}`
    )
    .join('');

  html += `<h4 class="apis-category-heading">Custom Sources</h4>`;
  html += customSources.length
    ? `<div class="custom-source-list">${customSources.map(customSourceRowHTML).join('')}</div>`
    : '<p class="api-source-row__desc">No custom sources added yet.</p>';

  scroll.innerHTML = html;

  scroll.querySelectorAll('.api-source-row').forEach((row) => {
    const sourceId = row.dataset.sourceId;
    const toggle = row.querySelector('.source-toggle-input');
    toggle.addEventListener('change', () => toggleApiSource(sourceId, toggle.checked));
    const removeBtn = row.querySelector('.api-source-row__remove');
    if (removeBtn) removeBtn.addEventListener('click', () => removeCustomSource(sourceId));
  });
}

async function loadApisPanel() {
  const status = document.getElementById('apis-status');
  try {
    apisPanelSourcesCache = await fetchJSON('/api/sources');
    renderApisPanel();
  } catch (err) {
    status.textContent = `Failed to load sources: ${err.message}`;
  }
}

async function toggleApiSource(sourceId, enabled) {
  const status = document.getElementById('apis-status');
  try {
    await fetchJSON(`/api/sources/${encodeURIComponent(sourceId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    status.textContent = '';
  } catch (err) {
    status.textContent = `Failed to update source: ${err.message}`;
    await loadApisPanel(); // revert the toggle visually to the real saved state
  }
}

async function removeCustomSource(sourceId) {
  const status = document.getElementById('apis-status');
  try {
    await fetchJSON(`/api/sources/custom/${encodeURIComponent(sourceId)}`, { method: 'DELETE' });
    await loadApisPanel();
  } catch (err) {
    status.textContent = `Failed to remove source: ${err.message}`;
  }
}

async function addCustomSourceFromModal() {
  const nameInput = document.getElementById('custom-source-name-input');
  const urlInput = document.getElementById('custom-source-url-input');
  const keyInput = document.getElementById('custom-source-key-input');
  const status = document.getElementById('apis-status');

  const name = nameInput.value.trim();
  const endpointUrl = urlInput.value.trim();
  const apiKey = keyInput.value.trim();

  if (!name || !endpointUrl) {
    status.textContent = 'Name and endpoint URL are required.';
    return;
  }

  try {
    await fetchJSON('/api/sources/custom', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, endpoint_url: endpointUrl, api_key: apiKey }),
    });
    nameInput.value = '';
    urlInput.value = '';
    keyInput.value = '';
    status.textContent = `Added ${name}.`;
    await loadApisPanel();
  } catch (err) {
    status.textContent = `Failed to add source: ${err.message}`;
  }
}

function openApisModal() {
  document.getElementById('apis-modal').classList.remove('hidden');
  document.getElementById('apis-status').textContent = '';
  apisPanelSourcesCache = null;
  renderApisPanel();
  loadApisPanel();
}

function closeApisModal() {
  document.getElementById('apis-modal').classList.add('hidden');
}

/**
 * Optional Congress.gov / api.data.gov API key settings modal. This key is
 * entirely optional -- by default the app uses the free community
 * congress-legislators directory with no signup required. If configured,
 * the Congress.gov API is preferred for politician bio/photo data on the
 * next refresh; committees and all trade data are unaffected either way.
 */
async function openSettingsModal() {
  const modal = document.getElementById('settings-modal');
  const input = document.getElementById('congress-api-key-input');
  const autoRefreshSelect = document.getElementById('auto-refresh-select');
  const status = document.getElementById('settings-status');
  const ocrStatus = document.getElementById('ocr-status');
  input.value = '';
  status.textContent = 'Loading current status…';
  ocrStatus.textContent = '';
  modal.classList.remove('hidden');
  try {
    const s = await fetchJSON('/api/settings');
    if (s.congress_gov_api_key_set) {
      input.placeholder = `Currently set (${s.congress_gov_api_key_masked}) — enter a new key to replace it`;
      status.textContent = `A key is configured. Legislator directory source: ${
        s.legislator_source || 'not yet refreshed with this key'
      }`;
    } else {
      input.placeholder = 'Paste your API key…';
      status.textContent = 'No key configured — using the free community directory (this is completely fine).';
    }
    autoRefreshSelect.value = String(s.auto_refresh_minutes ?? 180);
    ocrStatus.textContent = s.ocr_available
      ? 'OCR fallback: available ✓'
      : 'OCR fallback: not installed (optional -- see README.md)';
  } catch (err) {
    status.textContent = 'Could not load current settings.';
  }
}

function closeSettingsModal() {
  document.getElementById('settings-modal').classList.add('hidden');
}

async function saveSettingsFromModal() {
  const input = document.getElementById('congress-api-key-input');
  const autoRefreshSelect = document.getElementById('auto-refresh-select');
  const status = document.getElementById('settings-status');
  const value = input.value.trim();

  const body = { auto_refresh_minutes: parseInt(autoRefreshSelect.value, 10) };
  if (value) body.congress_gov_api_key = value;

  try {
    await fetchJSON('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    showToast(value ? 'Saved. The new key will be used on the next data refresh.' : 'Settings saved.');
    closeSettingsModal();
    updateMeta();
  } catch (err) {
    status.textContent = `Failed to save: ${err.message}`;
  }
}

async function clearSettingsApiKey() {
  const status = document.getElementById('settings-status');
  try {
    await fetchJSON('/api/settings/congress_gov_api_key', { method: 'DELETE' });
    document.getElementById('congress-api-key-input').value = '';
    status.textContent = 'Key cleared. The community directory will be used on the next refresh.';
    updateMeta();
  } catch (err) {
    status.textContent = `Failed to clear key: ${err.message}`;
  }
}

/**
 * "More refresh options" dropdown (the caret button next to "Refresh
 * Data"). Combines three things:
 *   1. A custom "refresh since" date picker, letting the user override the
 *      default 12-month trade lookback window with an earlier start date
 *      for a single refresh (functions in this section).
 *   2. "Stop Refresh" -- cancels a refresh in progress (see stopRefresh()
 *      near pollRefreshStatus() above).
 *   3. "Clear All Data" -- wipes all downloaded data (see clearAllData()
 *      near pollRefreshStatus() above).
 */
function syncRefreshSinceDateInput() {
  const input = document.getElementById('refresh-since-date-input');
  if (!input) return;
  if (refreshSinceMinDate) input.min = refreshSinceMinDate;
  // The picker is only meant for going further back than the 12-month
  // default, so the default date itself is the most recent allowed value.
  if (refreshSinceDefaultDate) input.max = refreshSinceDefaultDate;
  if (!input.value && refreshSinceDefaultDate) input.value = refreshSinceDefaultDate;
}

function toggleRefreshRangeMenu(forceOpen) {
  const menu = document.getElementById('refresh-range-menu');
  const btn = document.getElementById('refresh-range-btn');
  if (!menu || !btn) return;
  const shouldOpen = forceOpen !== undefined ? forceOpen : menu.classList.contains('hidden');
  if (shouldOpen) syncRefreshSinceDateInput();
  menu.classList.toggle('hidden', !shouldOpen);
  btn.setAttribute('aria-expanded', String(shouldOpen));
}

function closeRefreshRangeMenu() {
  toggleRefreshRangeMenu(false);
}

function resetRefreshSinceDate() {
  refreshSinceDate = null;
  const input = document.getElementById('refresh-since-date-input');
  if (input && refreshSinceDefaultDate) input.value = refreshSinceDefaultDate;
  closeRefreshRangeMenu();
  showToast('Will use the default 12-month window on the next refresh.');
}

function applyRefreshSinceDate() {
  const input = document.getElementById('refresh-since-date-input');
  const value = input && input.value;
  if (!value) {
    showToast('Pick a start date first.');
    return;
  }
  refreshSinceDate = value;
  closeRefreshRangeMenu();
  startRefresh(refreshSinceDate);
}

/**
 * Settings dropdown menu (Settings button in the header). Sections, top to
 * bottom: "Update App" (checks GitHub for a newer release), "APIs" (opens
 * the data-source panel below), "Restart Server", "Shut Down Server" (both
 * call the backend /api/server/* endpoints).
 */
function toggleSettingsMenu(forceOpen) {
  const menu = document.getElementById('settings-menu');
  const btn = document.getElementById('settings-btn');
  const shouldOpen = forceOpen !== undefined ? forceOpen : menu.classList.contains('hidden');
  menu.classList.toggle('hidden', !shouldOpen);
  btn.setAttribute('aria-expanded', String(shouldOpen));
}

function closeSettingsMenu() {
  toggleSettingsMenu(false);
}

async function restartServer() {
  closeSettingsMenu();
  if (!window.confirm('Restart the server now? This briefly interrupts the app while it restarts.')) return;
  try {
    await fetchJSON('/api/server/restart', { method: 'POST' });
    showToast('Restarting server…');
  } catch (err) {
    showToast(`Failed to restart: ${err.message}`);
  }
}

async function shutdownServer() {
  closeSettingsMenu();
  if (!window.confirm('Shut down the server now? You will need to relaunch the app to use it again.')) return;
  try {
    await fetchJSON('/api/server/shutdown', { method: 'POST' });
    showToast('Shutting down server…');
  } catch (err) {
    showToast(`Failed to shut down: ${err.message}`);
  }
}

/**
 * "Update App" button (top of the Settings dropdown). Checks GitHub for a
 * newer published release than the one currently running (see
 * backend/update_check.py) -- entirely read-only, never downloads or
 * installs anything itself. While no update is available, the button reads
 * "Update App" and just opens the releases page (so users can always check
 * manually / see release notes). Once a newer version is found, it switches
 * to "Update Available" and stays that way until the app is restarted with
 * the newer version installed.
 */
let latestUpdateInfo = null;

async function checkForAppUpdate() {
  try {
    latestUpdateInfo = await fetchJSON('/api/version/check');
  } catch (err) {
    latestUpdateInfo = null; // offline/rate-limited -- fail silently, try again next cycle
  }
  renderUpdateButton();
}

function renderUpdateButton() {
  const btn = document.getElementById('settings-menu-update');
  if (!btn || !latestUpdateInfo) return;
  if (latestUpdateInfo.update_available) {
    btn.textContent = `Update Available (v${latestUpdateInfo.latest_version})`;
    btn.classList.add('settings-menu-item-update-available');
    btn.title = `A newer version (v${latestUpdateInfo.latest_version}) is available on GitHub`;
  } else {
    btn.textContent = 'Update App';
    btn.classList.remove('settings-menu-item-update-available');
    btn.title = 'Check GitHub for a newer version';
  }
}

function openUpdatePage() {
  closeSettingsMenu();
  const url = (latestUpdateInfo && latestUpdateInfo.release_url) || 'https://github.com/';
  window.open(url, '_blank', 'noopener');
}

/**
 * Notifications dropdown (bell/button in the top nav, to the right of
 * Stocks). Shows every trade that matched a saved "Notify Me" preference
 * (see backend's notification_preferences + notifications tables and
 * _generate_all_notifications). Opening the dropdown loads/renders the
 * list; closing it (by clicking outside, see wireHeaderEvents) marks
 * everything read and clears the unread badge/count.
 */
let notificationsPollTimer = null;
let notificationsCache = [];

function renderNotificationsBadge(unreadCount) {
  const badge = document.getElementById('notifications-badge');
  if (!badge) return;
  badge.textContent = unreadCount > 99 ? '99+' : String(unreadCount);
  badge.classList.toggle('hidden', !unreadCount);
}

async function refreshNotificationsBadge() {
  try {
    const { unread_count } = await fetchJSON('/api/notifications/unread_count');
    renderNotificationsBadge(unread_count);
  } catch (_) {
    /* transient network hiccup -- ignore, the next poll will retry */
  }
}

function startNotificationsPolling() {
  refreshNotificationsBadge();
  clearInterval(notificationsPollTimer);
  notificationsPollTimer = setInterval(refreshNotificationsBadge, 45000);
}

/** Renders the same profit/loss badge used in the trades table (see
 * profitLossBadge) from a notification row's stored profit_loss /
 * profit_loss_pct columns -- empty string for buy notifications or sales
 * with nothing to compare against (never guessed). */
function notificationProfitLossBadge(n) {
  if (n.profit_loss === null || n.profit_loss === undefined) return '';
  const isProfit = n.profit_loss >= 0;
  const sign = isProfit ? '+' : '-';
  const cls = isProfit ? 'pnl-profit' : 'pnl-loss';
  const amountStr = formatMoney(Math.abs(n.profit_loss));
  const pctStr =
    n.profit_loss_pct === null || n.profit_loss_pct === undefined ? '' : ` (${sign}${Math.abs(n.profit_loss_pct).toFixed(1)}%)`;
  return ` <span class="badge ${cls}">${sign}${amountStr}${pctStr}</span>`;
}

function notificationItemHTML(n) {
  const tradeDate = n.transaction_date ? formatDate(n.transaction_date) : '';
  return `
    <button type="button" class="notification-item${n.is_read ? '' : ' notification-unread'}"
      data-bioguide="${escapeHtml(n.bioguide_id || '')}"
      data-politician-name="${escapeHtml(n.politician_name || '')}"
      data-trade-id="${escapeHtml(String(n.trade_id))}">
      <div class="notification-item__message">${escapeHtml(n.message)}${notificationProfitLossBadge(n)}</div>
      <div class="notification-item__meta">${escapeHtml(n.ticker || '')}${tradeDate ? ` &middot; ${escapeHtml(tradeDate)}` : ''}</div>
    </button>`;
}

async function loadAndRenderNotifications() {
  const list = document.getElementById('notifications-list');
  list.innerHTML = '<p class="loading-text">Loading…</p>';
  try {
    notificationsCache = await fetchJSON('/api/notifications');
    if (!notificationsCache.length) {
      list.innerHTML = '<div class="notifications-empty">No notifications yet. Set up a "Notify Me" alert from any trade row to get started.</div>';
      return;
    }
    list.innerHTML = notificationsCache.map(notificationItemHTML).join('');
    list.querySelectorAll('.notification-item').forEach((btn) => {
      btn.addEventListener('click', () => {
        closeNotificationsMenu();
        const bioguideId = btn.dataset.bioguide;
        const tradeId = btn.dataset.tradeId;
        if (!bioguideId) {
          showToast('This trade never resolved to a current officeholder page.');
          return;
        }
        navigateTo(`#/politicians/${encodeURIComponent(bioguideId)}?trade=${encodeURIComponent(tradeId)}`);
      });
    });
  } catch (err) {
    list.innerHTML = `<div class="notifications-empty">Failed to load notifications: ${escapeHtml(err.message)}</div>`;
  }
}

function toggleNotificationsMenu(forceOpen) {
  const menu = document.getElementById('notifications-menu');
  const btn = document.getElementById('notifications-btn');
  const wasOpen = !menu.classList.contains('hidden');
  const shouldOpen = forceOpen !== undefined ? forceOpen : !wasOpen;
  menu.classList.toggle('hidden', !shouldOpen);
  btn.setAttribute('aria-expanded', String(shouldOpen));
  if (shouldOpen) {
    loadAndRenderNotifications();
  } else if (wasOpen) {
    // Transitioned open -> closed (including a click outside the dropdown):
    // clear the unread count, as requested.
    clearUnreadNotifications();
  }
}

function closeNotificationsMenu() {
  toggleNotificationsMenu(false);
}

/** Marks all notifications read server-side and clears the header badge.
 * Called whenever the dropdown closes, including a click outside of it. */
async function clearUnreadNotifications() {
  renderNotificationsBadge(0);
  try {
    await fetchJSON('/api/notifications/mark_read', { method: 'POST' });
  } catch (_) {
    /* transient network hiccup -- the next poll will reconcile the badge */
  }
}

function wireHeaderEvents() {
  document.getElementById('theme-toggle-btn').addEventListener('click', toggleTheme);
  document.getElementById('refresh-btn').addEventListener('click', () => startRefresh());

  document.getElementById('refresh-range-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleRefreshRangeMenu();
  });
  document.getElementById('refresh-range-reset-btn').addEventListener('click', resetRefreshSinceDate);
  document.getElementById('refresh-range-apply-btn').addEventListener('click', applyRefreshSinceDate);
  document.getElementById('refresh-range-stop-btn').addEventListener('click', stopRefresh);
  document.getElementById('refresh-range-clear-btn').addEventListener('click', clearAllData);
  document.addEventListener('click', (e) => {
    const dropdown = document.querySelector('.refresh-range-dropdown');
    if (dropdown && !dropdown.contains(e.target)) closeRefreshRangeMenu();
  });

  document.getElementById('settings-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleSettingsMenu();
  });
  document.getElementById('settings-menu-update').addEventListener('click', openUpdatePage);
  document.getElementById('settings-menu-apis').addEventListener('click', () => {
    closeSettingsMenu();
    openApisModal();
  });
  // The Android build runs the server in-process (killing it would kill the
  // whole app, not just restart a server process) -- see
  // backend/app.py's restart_server()/shutdown_server(), which already
  // refuse these requests there. Hide the controls entirely rather than
  // let them fail silently.
  if (navigator.userAgent.includes('PoliticianTradesAndroid')) {
    document.getElementById('settings-menu-restart')?.remove();
    document.getElementById('settings-menu-shutdown')?.remove();
  } else {
    document.getElementById('settings-menu-restart').addEventListener('click', restartServer);
    document.getElementById('settings-menu-shutdown').addEventListener('click', shutdownServer);
  }
  document.addEventListener('click', (e) => {
    const dropdown = document.querySelector('.settings-dropdown');
    if (dropdown && !dropdown.contains(e.target)) closeSettingsMenu();
  });

  document.getElementById('notifications-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleNotificationsMenu();
  });
  document.addEventListener('click', (e) => {
    const dropdown = document.querySelector('.notifications-dropdown');
    if (dropdown && !dropdown.contains(e.target)) closeNotificationsMenu();
  });

  document.getElementById('settings-cancel-btn').addEventListener('click', closeSettingsModal);
  document.getElementById('settings-save-btn').addEventListener('click', saveSettingsFromModal);
  document.getElementById('settings-clear-btn').addEventListener('click', clearSettingsApiKey);
  document.getElementById('settings-modal').addEventListener('click', (e) => {
    if (e.target.id === 'settings-modal') closeSettingsModal();
  });

  document.getElementById('apis-close-btn').addEventListener('click', closeApisModal);
  document.getElementById('custom-source-add-btn').addEventListener('click', addCustomSourceFromModal);
  document.getElementById('apis-modal').addEventListener('click', (e) => {
    if (e.target.id === 'apis-modal') closeApisModal();
  });

  document.getElementById('notify-cancel-btn').addEventListener('click', closeNotifyModal);
  document.getElementById('notify-save-btn').addEventListener('click', saveNotifyPreference);
  document.getElementById('notify-delete-btn').addEventListener('click', deleteNotifyPreference);
  document.getElementById('notify-modal').addEventListener('click', (e) => {
    if (e.target.id === 'notify-modal') closeNotifyModal();
  });

  document.querySelectorAll('.nav-link[data-route]').forEach((btn) => {
    btn.addEventListener('click', () => navigateTo(btn.dataset.route));
  });

  document.querySelectorAll('.back-btn').forEach((btn) => {
    btn.addEventListener('click', () => navigateTo(btn.dataset.back));
  });

  // Global header search: searches politicians (by name) and stocks (by
  // ticker or company name) from any view, showing results in a dropdown
  // rather than jumping to/filtering the Politicians view -- see
  // runGlobalSearch/renderGlobalSearchResults above.
  const globalSearch = document.getElementById('global-search');
  globalSearch.addEventListener(
    'input',
    debounce(() => runGlobalSearch(globalSearch.value), 300)
  );
  globalSearch.addEventListener('focus', () => {
    if (globalSearch.value.trim()) runGlobalSearch(globalSearch.value);
  });
  document.addEventListener('click', (e) => {
    const wrap = document.querySelector('.global-search-wrap');
    if (wrap && !wrap.contains(e.target)) closeGlobalSearchResults();
  });

  // In-view filters (Politicians view).
  document.getElementById('filter-party').addEventListener('change', (e) => {
    filterState.party = e.target.value;
    renderPoliticiansView();
  });
  document.getElementById('filter-state').addEventListener('change', (e) => {
    filterState.state = e.target.value;
    renderPoliticiansView();
  });
  document.getElementById('filter-chamber').addEventListener('change', (e) => {
    filterState.chamber = e.target.value;
    renderPoliticiansView();
  });
  document.getElementById('filter-search').addEventListener(
    'input',
    debounce((e) => {
      filterState.search = e.target.value;
      syncFilterInputs();
      renderPoliticiansView();
    }, 300)
  );
  document.getElementById('sort-politicians-activity').addEventListener('click', (e) => {
    const nextIndex = (POLITICIANS_SORT_CYCLE.indexOf(politiciansSortMode) + 1) % POLITICIANS_SORT_CYCLE.length;
    politiciansSortMode = POLITICIANS_SORT_CYCLE[nextIndex];
    e.target.textContent = POLITICIANS_SORT_LABELS[politiciansSortMode];
    e.target.classList.toggle('sort-active', politiciansSortMode !== 'name');
    renderPoliticiansView();
  });
}

/* ---------------------------------------------------------------------
 * 4b. Recent Trades view (home / landing page)
 *     Shows the most recently *disclosed* trades (not just most recent
 *     transaction date), since that's what's newly actionable information.
 * ------------------------------------------------------------------- */

const RECENT_TRADES_PAGE_SIZE = 50;

async function renderRecentView() {
  const container = document.getElementById('recent-content');
  const subtitle = document.getElementById('recent-subtitle');

  container.innerHTML = `
    <div id="portfolio-chart-panel"></div>
    <h3 class="section-heading">Recent Disclosures</h3>
    <div class="recent-layout">
      ${renderRecentFiltersPanel()}
      <div id="recent-trades-section" class="recent-trades-main"><p class="loading-text">Loading…</p></div>
    </div>
  `;
  mountPortfolioChart(container.querySelector('#portfolio-chart-panel'));

  const tradesSection = container.querySelector('#recent-trades-section');
  wireRecentFiltersPanel(container, tradesSection, subtitle);
  await loadRecentTradesPage(tradesSection, subtitle, 1);
}

/** Builds the left-rail facet filter panel for the Recent Trades view --
 * date range, trade type, party, disclosed-amount bucket, and a stock/asset
 * search box, in the style of a typical shopping-site filter sidebar.
 * Rendered fresh each time renderRecentView() runs (its inputs are
 * initialized from recentFiltersState so a same-session view switch away
 * and back doesn't silently reset the user's selections). */
function renderRecentFiltersPanel() {
  const typeChecked = (val) => (recentFiltersState.types.includes(val) ? 'checked' : '');
  const partyChecked = (val) => (recentFiltersState.parties.includes(val) ? 'checked' : '');
  const amountChecked = (floor) => (recentFiltersState.amountBuckets.includes(floor) ? 'checked' : '');

  const amountOptions = AMOUNT_BUCKETS.map(
    (b) => `
      <label class="filter-checkbox">
        <input type="checkbox" class="rf-amount" value="${b.floor}" ${amountChecked(b.floor)} />
        ${escapeHtml(b.label)}
      </label>`
  ).join('');

  return `
    <aside class="recent-filters-panel" id="recent-filters-panel">
      <div class="filter-group">
        <h4 class="filter-group__title">Date Range</h4>
        <div class="filter-date-range">
          <input type="date" id="rf-start-date" aria-label="Start date" value="${escapeHtml(recentFiltersState.startDate)}" />
          <span class="filter-date-range__sep">to</span>
          <input type="date" id="rf-end-date" aria-label="End date" value="${escapeHtml(recentFiltersState.endDate)}" />
        </div>
      </div>

      <div class="filter-group">
        <h4 class="filter-group__title">Trade Type</h4>
        <label class="filter-checkbox"><input type="checkbox" class="rf-type" value="purchase" ${typeChecked('purchase')} /> Purchase</label>
        <label class="filter-checkbox"><input type="checkbox" class="rf-type" value="sale,sale_partial" ${typeChecked('sale,sale_partial')} /> Sale</label>
        <label class="filter-checkbox"><input type="checkbox" class="rf-type" value="exchange" ${typeChecked('exchange')} /> Exchange</label>
      </div>

      <div class="filter-group">
        <h4 class="filter-group__title">Party</h4>
        <label class="filter-checkbox"><input type="checkbox" class="rf-party" value="Democrat" ${partyChecked('Democrat')} /> Democrat</label>
        <label class="filter-checkbox"><input type="checkbox" class="rf-party" value="Republican" ${partyChecked('Republican')} /> Republican</label>
        <label class="filter-checkbox"><input type="checkbox" class="rf-party" value="Independent" ${partyChecked('Independent')} /> Independent</label>
      </div>

      <div class="filter-group">
        <h4 class="filter-group__title">Trade Amount</h4>
        ${amountOptions}
      </div>

      <div class="filter-group">
        <h4 class="filter-group__title">Stock / Asset</h4>
        <input type="search" id="rf-search" class="filter-search-input" placeholder="Ticker or company name…"
          aria-label="Search stock or asset name" value="${escapeHtml(recentFiltersState.search)}" />
      </div>

      <div class="filter-actions">
        <button type="button" id="rf-clear" class="btn btn-secondary btn-small">Clear Filters</button>
      </div>
    </aside>`;
}

/** Wires up the filter panel's inputs (see renderRecentFiltersPanel) to
 * update recentFiltersState and reload page 1 of the trades table on every
 * change -- never re-renders the panel itself, so checking a box doesn't
 * disturb the rest of the panel's state or scroll position. */
function wireRecentFiltersPanel(container, tradesSection, subtitle) {
  const reload = () => loadRecentTradesPage(tradesSection, subtitle, 1);

  container.querySelector('#rf-start-date').addEventListener('change', (e) => {
    recentFiltersState.startDate = e.target.value;
    reload();
  });
  container.querySelector('#rf-end-date').addEventListener('change', (e) => {
    recentFiltersState.endDate = e.target.value;
    reload();
  });

  container.querySelectorAll('.rf-type').forEach((el) => {
    el.addEventListener('change', () => {
      recentFiltersState.types = Array.from(container.querySelectorAll('.rf-type:checked')).map((c) => c.value);
      reload();
    });
  });
  container.querySelectorAll('.rf-party').forEach((el) => {
    el.addEventListener('change', () => {
      recentFiltersState.parties = Array.from(container.querySelectorAll('.rf-party:checked')).map((c) => c.value);
      reload();
    });
  });
  container.querySelectorAll('.rf-amount').forEach((el) => {
    el.addEventListener('change', () => {
      recentFiltersState.amountBuckets = Array.from(container.querySelectorAll('.rf-amount:checked')).map((c) =>
        Number(c.value)
      );
      reload();
    });
  });

  container.querySelector('#rf-search').addEventListener(
    'input',
    debounce((e) => {
      recentFiltersState.search = e.target.value;
      reload();
    }, 300)
  );

  container.querySelector('#rf-clear').addEventListener('click', () => {
    recentFiltersState.startDate = '';
    recentFiltersState.endDate = '';
    recentFiltersState.types = [];
    recentFiltersState.parties = [];
    recentFiltersState.amountBuckets = [];
    recentFiltersState.search = '';
    // Full view re-render (rather than just reload()) so every checkbox,
    // date input, and the search box visibly reset to empty too.
    renderRecentView();
  });
}

/** Pushes the filter panel down so its top edge lines up with the actual
 * <table> (not the top of the trades section, which also includes the
 * shared trades-table component's own "Reset Columns" toolbar row above the
 * table itself). Measured from the live DOM rather than a hardcoded pixel
 * offset so it stays correct regardless of font size/theme -- the toolbar
 * row's height isn't a fixed constant we can just hardcode reliably.
 * No-ops below the 860px breakpoint where the panel stacks above the table
 * instead of beside it (see the .recent-layout media query in style.css). */
function alignRecentFiltersPanelWithTable(tradesSection) {
  const panel = document.getElementById('recent-filters-panel');
  const tableWrap = tradesSection.querySelector('.table-wrap');
  if (!panel || !tableWrap) return;
  if (window.innerWidth <= 860) {
    panel.style.marginTop = '';
    return;
  }
  const offset = tableWrap.getBoundingClientRect().top - tradesSection.getBoundingClientRect().top;
  panel.style.marginTop = `${Math.max(0, Math.round(offset))}px`;
}

/** True if any Recent Trades facet filter is currently active. */
function recentFiltersActive() {
  const f = recentFiltersState;
  return !!(f.startDate || f.endDate || f.types.length || f.parties.length || f.amountBuckets.length || f.search);
}

/** Builds the /api/trades/recent query string for the current page and the
 * live recentFiltersState selection. */
function buildRecentTradesQuery(page) {
  const params = new URLSearchParams();
  params.set('page', page);
  params.set('page_size', RECENT_TRADES_PAGE_SIZE);
  const f = recentFiltersState;
  if (f.startDate) params.set('start_date', f.startDate);
  if (f.endDate) params.set('end_date', f.endDate);
  if (f.types.length) params.set('type', f.types.join(','));
  if (f.parties.length) params.set('party', f.parties.join(','));
  if (f.amountBuckets.length) params.set('amount_buckets', f.amountBuckets.join(','));
  if (f.search) params.set('search', f.search);
  return params.toString();
}

/** Fetches and renders one page of the Recent Disclosures table (server-side
 * paginated and filtered, most recently disclosed first -- see
 * /api/trades/recent). Re-invoked by the pagination controls'
 * Previous/Next/page-jump handlers to load a different page, and by the
 * filter panel on every selection change (always resetting to page 1),
 * without re-rendering the whole view. */
async function loadRecentTradesPage(tradesSection, subtitle, page) {
  tradesSection.innerHTML = '<p class="loading-text">Loading…</p>';
  try {
    const data = await fetchJSON(`/api/trades/recent?${buildRecentTradesQuery(page)}`);
    const trades = data.trades || [];

    if (!data.total) {
      subtitle.textContent = 'Most recently disclosed trades';
      tradesSection.innerHTML = recentFiltersActive()
        ? `<div class="empty-state"><strong>No trades match your filters.</strong>Try clearing a filter.</div>`
        : `<div class="empty-state"><strong>No trade data yet.</strong>Click "Refresh Data" above to download the latest disclosures.</div>`;
      return;
    }

    subtitle.textContent = recentFiltersActive()
      ? `${data.total.toLocaleString()} trade${data.total === 1 ? '' : 's'} match your filters`
      : `All disclosed trades, most recently disclosed first (${data.total.toLocaleString()} total)`;

    tradesSection.innerHTML = '<div id="recent-trades-table"></div>';
    mountTradesTable(tradesSection.querySelector('#recent-trades-table'), trades, {
      showPoliticianColumn: true,
      showPartyColumn: true,
      searchable: false,
      defaultSortKey: 'disclosure_date',
      emptyMessage: 'No recent disclosures found.',
      tableId: 'recent-trades',
      pageSize: RECENT_TRADES_PAGE_SIZE,
      serverPagination: {
        page: data.page,
        totalPages: data.total_pages,
        totalRows: data.total,
        onPageChange: (newPage) => loadRecentTradesPage(tradesSection, subtitle, newPage),
      },
    });
    alignRecentFiltersPanelWithTable(tradesSection);
  } catch (err) {
    tradesSection.innerHTML = `<div class="empty-state"><strong>Failed to load recent trades.</strong>${escapeHtml(err.message)}</div>`;
  }
}

/* ---------------------------------------------------------------------
 * 4c. Combined Politician Portfolio Performance chart
 *     Shown at the top of the Recent Trades (home) view. See the honesty
 *     note on the backend's /api/portfolio/performance route: this tracks
 *     ESTIMATED NET INVESTED CAPITAL (disclosed purchases minus disclosed
 *     sales), not a verified real-time market value -- disclosures never
 *     include share counts or live prices, so true portfolio value can't
 *     be computed from public, key-less sources. Clearly labeled as such.
 * ------------------------------------------------------------------- */

const PORTFOLIO_RANGES = [
  { key: '1m', label: '1M' },
  { key: '3m', label: '3M' },
  { key: '6m', label: '6M' },
  { key: 'ytd', label: 'YTD' },
  { key: '1y', label: '1Y' },
  { key: '5y', label: '5Y' },
];

const PORTFOLIO_COMPARISON_COLORS = ['#2563eb', '#d97706', '#0891b2', '#be185d', '#65a30d', '#7c3aed'];
const MAX_COMPARISON_POLITICIANS = 6;

function portfolioChartPanelHTML() {
  return `
    <div class="chart-panel portfolio-chart-panel">
      <div class="chart-panel__header">
        <h3 class="section-heading" style="margin:0;">Combined Politician Portfolio Performance</h3>
        <div class="range-buttons">
          ${PORTFOLIO_RANGES.map(
            (r) => `<button class="range-btn${r.key === '1y' ? ' active' : ''}" data-range="${r.key}">${r.label}</button>`
          ).join('')}
        </div>
      </div>
      <div class="portfolio-chart-controls">
        <div class="portfolio-mode-toggle">
          <button type="button" class="btn btn-secondary btn-small portfolio-mode-btn active" data-mode="value">$ Value</button>
          <button type="button" class="btn btn-secondary btn-small portfolio-mode-btn" data-mode="pct">% Change</button>
        </div>
        <label class="portfolio-checkbox"><input type="checkbox" class="portfolio-benchmark-toggle" data-benchmark="sp500"/> S&amp;P 500</label>
        <label class="portfolio-checkbox"><input type="checkbox" class="portfolio-benchmark-toggle" data-benchmark="nasdaq"/> NASDAQ</label>
        <div class="portfolio-add-politician">
          <input type="search" class="portfolio-politician-search" placeholder="Add a politician to compare…" disabled/>
          <button type="button" class="btn btn-secondary btn-small portfolio-add-btn" disabled>Add</button>
        </div>
      </div>
      <p class="portfolio-mode-hint">Switch to “% Change” to add individual politician comparison lines — S&amp;P 500/NASDAQ work in either $ or % mode (shown on their own % axis in $ mode).</p>
      <div class="portfolio-chips"></div>
      <div class="chart-container portfolio-chart-container"><p class="loading-text">Loading chart…</p></div>
      <p class="disclaimer-note portfolio-disclaimer"></p>
    </div>`;
}

function updatePortfolioControlsAvailability(container, state) {
  // S&P 500/NASDAQ toggles work in both $ and % mode (see initPortfolioChart --
  // in $ mode they're drawn against a secondary % axis), so only the
  // individual-politician comparison controls are gated to % mode (a
  // politician's net invested capital in $ isn't comparable to an index's
  // point value on the same axis the way a % change is).
  const disabled = state.mode !== 'pct';
  container.querySelectorAll('.portfolio-politician-search, .portfolio-add-btn').forEach((el) => {
    el.disabled = disabled;
  });
  const hint = container.querySelector('.portfolio-mode-hint');
  if (hint) hint.classList.toggle('hidden', !disabled);
}

function renderPortfolioChips(container, state) {
  const chipsEl = container.querySelector('.portfolio-chips');
  if (!chipsEl) return;
  if (!state.comparedPoliticians.length) {
    chipsEl.innerHTML = '';
    return;
  }
  chipsEl.innerHTML = state.comparedPoliticians
    .map(
      (p) => `
      <span class="portfolio-chip" style="border-color:${p.color}">
        <span class="portfolio-chip-dot" style="background:${p.color}"></span>
        ${escapeHtml(p.name)}
        <button type="button" class="portfolio-chip-remove" data-bioguide="${escapeHtml(
          p.bioguideId
        )}" aria-label="Remove ${escapeHtml(p.name)}">×</button>
      </span>`
    )
    .join('');
  chipsEl.querySelectorAll('.portfolio-chip-remove').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.comparedPoliticians = state.comparedPoliticians.filter((p) => p.bioguideId !== btn.dataset.bioguide);
      renderPortfolioChips(container, state);
      loadAndRenderPortfolioChart(container, state);
    });
  });
}

/** Mounts the Combined Politician Portfolio Performance chart into `container`. */
function mountPortfolioChart(container) {
  const state = {
    range: '1y',
    mode: 'value',
    showSP500: false,
    showNasdaq: false,
    comparedPoliticians: [],
  };

  container.innerHTML = portfolioChartPanelHTML();

  container.querySelectorAll('.range-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      container.querySelectorAll('.range-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.range = btn.dataset.range;
      loadAndRenderPortfolioChart(container, state);
    });
  });

  container.querySelectorAll('.portfolio-mode-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      container.querySelectorAll('.portfolio-mode-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.mode = btn.dataset.mode;
      updatePortfolioControlsAvailability(container, state);
      loadAndRenderPortfolioChart(container, state);
    });
  });

  container.querySelectorAll('.portfolio-benchmark-toggle').forEach((cb) => {
    cb.addEventListener('change', () => {
      if (cb.dataset.benchmark === 'sp500') state.showSP500 = cb.checked;
      if (cb.dataset.benchmark === 'nasdaq') state.showNasdaq = cb.checked;
      loadAndRenderPortfolioChart(container, state);
    });
  });

  const searchInput = container.querySelector('.portfolio-politician-search');
  const addBtn = container.querySelector('.portfolio-add-btn');

  async function handleAdd() {
    const term = searchInput.value.trim();
    if (!term) return;
    if (state.comparedPoliticians.length >= MAX_COMPARISON_POLITICIANS) {
      showToast(`You can compare up to ${MAX_COMPARISON_POLITICIANS} politicians at a time.`);
      return;
    }
    try {
      const results = await fetchJSON(`/api/politicians?search=${encodeURIComponent(term)}`);
      if (!results.length) {
        showToast(`No politician found matching "${term}".`);
        return;
      }
      const match = results[0];
      if (state.comparedPoliticians.some((p) => p.bioguideId === match.bioguide_id)) {
        showToast(`${match.full_name} is already being compared.`);
        return;
      }
      const usedColors = new Set(state.comparedPoliticians.map((p) => p.color));
      const color =
        PORTFOLIO_COMPARISON_COLORS.find((c) => !usedColors.has(c)) || PORTFOLIO_COMPARISON_COLORS[0];
      state.comparedPoliticians.push({ bioguideId: match.bioguide_id, name: match.full_name, color });
      searchInput.value = '';
      renderPortfolioChips(container, state);
      loadAndRenderPortfolioChart(container, state);
    } catch (err) {
      showToast('Could not search for that politician.');
    }
  }

  addBtn.addEventListener('click', handleAdd);
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleAdd();
    }
  });

  updatePortfolioControlsAvailability(container, state);
  loadAndRenderPortfolioChart(container, state);
}

/** Fetches portfolio performance data for the current state and (re)draws the chart. */
async function loadAndRenderPortfolioChart(container, state) {
  const chartContainer = container.querySelector('.portfolio-chart-container');
  chartContainer.innerHTML = '<p class="loading-text">Loading chart…</p>';

  try {
    const params = new URLSearchParams();
    params.set('range', state.range);
    state.comparedPoliticians.forEach((p) => params.append('bioguide_id', p.bioguideId));
    const data = await fetchJSON(`/api/portfolio/performance?${params.toString()}`);

    const disclaimerEl = container.querySelector('.portfolio-disclaimer');
    if (disclaimerEl) disclaimerEl.textContent = data.disclaimer || '';

    if (typeof window.Chart === 'undefined') {
      chartContainer.innerHTML = `<div class="chart-fallback-msg">
        Chart.js was not found (missing <code>frontend/vendor/chart.min.js</code>), so this chart
        can't be displayed. See the comment at the top of that file for instructions.
      </div>`;
      return;
    }

    if (!data.labels || !data.labels.length) {
      chartContainer.innerHTML = '<div class="empty-state">No trade data available yet for this time range.</div>';
      return;
    }

    chartContainer.innerHTML = '<canvas></canvas>';
    initPortfolioChart(chartContainer.querySelector('canvas'), data, state);
  } catch (err) {
    chartContainer.innerHTML = `<div class="empty-state">Failed to load chart data: ${escapeHtml(err.message)}</div>`;
  }
}

/** Draws the combined portfolio line (colored green/red by overall trend),
 * plus any enabled S&P 500 / NASDAQ dashed comparison lines and individual
 * politician comparison lines. S&P 500/NASDAQ work in both $ and % mode --
 * in $ mode they're plotted against a secondary right-hand % axis, since an
 * index's point value isn't on the same scale as a dollar figure. Individual
 * politician comparison lines still require % mode (see
 * updatePortfolioControlsAvailability), since a politician's net invested
 * capital in $ isn't comparable to an index at all. */
function initPortfolioChart(canvasEl, data, state) {
  if (typeof window.Chart === 'undefined') return null;

  if (portfolioChartInstance) {
    portfolioChartInstance.destroy();
    portfolioChartInstance = null;
  }

  const theme = chartThemeColors();
  const isPct = state.mode === 'pct';
  const combinedValues = isPct ? data.combined.pct_change : data.combined.value;
  const trend = data.combined.trend;
  const trendColor = trend === 'down' ? '#b91c1c' : trend === 'up' ? '#15803d' : '#475569';
  const trendBg = trend === 'down' ? 'rgba(185, 28, 28, 0.12)' : trend === 'up' ? 'rgba(21, 128, 61, 0.12)' : 'rgba(71, 85, 105, 0.10)';

  const datasets = [
    {
      label: 'Combined (all tracked politicians)',
      data: combinedValues,
      borderColor: trendColor,
      backgroundColor: trendBg,
      fill: true,
      tension: 0.25,
      pointRadius: 0,
      borderWidth: 2,
      yAxisID: 'y',
      isPercent: isPct,
    },
  ];

  // S&P 500 / NASDAQ: always available, regardless of $ vs % mode.
  if (state.showSP500 && data.benchmarks.sp500) {
    datasets.push({
      label: 'S&P 500',
      data: data.benchmarks.sp500.pct_change,
      borderColor: '#0891b2',
      borderDash: [6, 4],
      fill: false,
      tension: 0.25,
      pointRadius: 0,
      borderWidth: 1.5,
      yAxisID: isPct ? 'y' : 'y1',
      isPercent: true,
    });
  }
  if (state.showNasdaq && data.benchmarks.nasdaq) {
    datasets.push({
      label: 'NASDAQ',
      data: data.benchmarks.nasdaq.pct_change,
      borderColor: '#7c3aed',
      borderDash: [6, 4],
      fill: false,
      tension: 0.25,
      pointRadius: 0,
      borderWidth: 1.5,
      yAxisID: isPct ? 'y' : 'y1',
      isPercent: true,
    });
  }

  // Individual politician comparison lines: % mode only (see doc comment above).
  if (isPct) {
    state.comparedPoliticians.forEach((p) => {
      const series = data.politicians[p.bioguideId];
      if (!series) return;
      datasets.push({
        label: p.name,
        data: series.pct_change,
        borderColor: p.color,
        fill: false,
        tension: 0.25,
        pointRadius: 0,
        borderWidth: 1.5,
        yAxisID: 'y',
        isPercent: true,
      });
    });
  }

  const scales = {
    x: {
      grid: { display: false },
      ticks: { font: { size: 11 }, maxTicksLimit: 10, color: theme.tick },
    },
    y: {
      ticks: {
        font: { size: 11 },
        callback: (v) => (isPct ? `${v}%` : formatMoney(v)),
        color: theme.tick,
      },
      grid: { color: theme.grid },
    },
  };

  // Secondary right-hand axis for S&P 500/NASDAQ when the primary axis is
  // in $ terms (only added when actually needed, so we don't show an empty
  // extra axis when no benchmark is checked).
  if (!isPct && (state.showSP500 || state.showNasdaq)) {
    scales.y1 = {
      position: 'right',
      ticks: { font: { size: 11 }, callback: (v) => `${v}%`, color: theme.tick },
      grid: { drawOnChartArea: false },
      title: { display: true, text: '% change (S&P 500 / NASDAQ)', font: { size: 10 } },
    };
  }

  portfolioChartInstance = new Chart(canvasEl.getContext('2d'), {
    type: 'line',
    data: {
      labels: data.labels,
      datasets,
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 11 }, color: theme.tick } },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const v = ctx.parsed.y;
              const asPercent = ctx.dataset.isPercent;
              return `${ctx.dataset.label}: ${asPercent ? `${v > 0 ? '+' : ''}${v.toFixed(2)}%` : formatMoneyFull(v)}`;
            },
          },
        },
      },
      scales,
    },
  });

  return portfolioChartInstance;
}

/* ---------------------------------------------------------------------
 * 5. Politicians list view
 * ------------------------------------------------------------------- */

function politicianCardHTML(p) {
  const committeeNames = [...new Set((p.committees || []).map((c) => simplifyCommitteeName(c.name)))];
  const visibleCommittees = committeeNames
    .slice(0, 3)
    .map((name) => `<span class="chip">${escapeHtml(name)}</span>`)
    .join('');
  const moreChip = committeeNames.length > 3
    ? `<span class="chip">+${committeeNames.length - 3} more</span>`
    : '';

  return `
    <div class="politician-card" data-bioguide="${escapeHtml(p.bioguide_id)}" role="button" tabindex="0">
      ${renderAvatar(p.photo_url, p.first_name, p.last_name)}
      <div class="politician-card__body">
        <p class="politician-card__name">${escapeHtml(p.full_name)}</p>
        <div class="politician-card__meta">
          ${partyBadge(p.party)}
          <span>${escapeHtml(p.state || '—')}</span>
          <span>${chamberLabel(p.chamber)}</span>
        </div>
        <div class="politician-card__stats">
          <strong>${p.trade_count ?? 0}</strong> trades &middot; <strong>${formatMoney(p.total_volume)}</strong> volume
        </div>
        <div class="chips">${visibleCommittees}${moreChip}</div>
      </div>
    </div>`;
}

async function renderPoliticiansView() {
  const container = document.getElementById('politicians-content');
  container.innerHTML = '<p class="loading-text">Loading…</p>';

  try {
    const params = new URLSearchParams();
    if (filterState.party) params.set('party', filterState.party);
    if (filterState.state) params.set('state', filterState.state);
    if (filterState.chamber) params.set('chamber', filterState.chamber);
    if (filterState.search) params.set('search', filterState.search);

    const list = await fetchJSON(`/api/politicians?${params.toString()}`);

    if (!list.length) {
      const filtersActive = filterState.party || filterState.state || filterState.chamber || filterState.search;
      container.innerHTML = filtersActive
        ? `<div class="empty-state"><strong>No politicians match your filters.</strong>Try clearing a filter or search term.</div>`
        : `<div class="empty-state"><strong>No data yet</strong>Click "Refresh Data" above to load politicians and trades.</div>`;
      return;
    }

    const sorted = sortPoliticiansList(list);
    container.innerHTML = `<div class="politician-grid">${sorted.map(politicianCardHTML).join('')}</div>`;

    container.querySelectorAll('.politician-card').forEach((card) => {
      const go = () => navigateTo(`#/politicians/${encodeURIComponent(card.dataset.bioguide)}`);
      card.addEventListener('click', go);
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          go();
        }
      });
    });
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Failed to load politicians: ${escapeHtml(err.message)}</div>`;
  }
}

/* ---------------------------------------------------------------------
 * 6. Politician detail view
 * ------------------------------------------------------------------- */

async function renderPoliticianDetail(bioguideId, highlightTradeId) {
  const container = document.getElementById('politician-detail-content');
  container.innerHTML = '<p class="loading-text">Loading…</p>';

  try {
    const p = await fetchJSON(`/api/politicians/${encodeURIComponent(bioguideId)}`);

    const committeeNames = [...new Set((p.committees || []).map((c) => simplifyCommitteeName(c.name)))];
    const committeesHTML = committeeNames.length
      ? `<ul class="committee-list">${committeeNames
          .map((name) => `<li>${escapeHtml(name)}</li>`)
          .join('')}</ul>`
      : '<p class="loading-text" style="padding:6px 0;">No committee assignments on file.</p>';

    container.innerHTML = `
      <div class="detail-header">
        ${renderAvatar(p.photo_url, p.first_name, p.last_name)}
        <div class="detail-header__info">
          <h2 class="detail-header__name">${escapeHtml(p.full_name)}</h2>
          <div class="detail-header__meta">
            ${partyBadge(p.party)}
            <span>${escapeHtml(p.state || '—')}</span>
            <span>${chamberLabel(p.chamber)}${p.district ? ` &middot; District ${escapeHtml(String(p.district))}` : ''}</span>
          </div>
          <div class="detail-header__stats">
            <div class="stat-block"><span class="stat-value">${p.trade_count ?? 0}</span><span class="stat-label">Trades</span></div>
            <div class="stat-block"><span class="stat-value">${p.purchase_count ?? 0}</span><span class="stat-label">Purchases</span></div>
            <div class="stat-block"><span class="stat-value">${p.sale_count ?? 0}</span><span class="stat-label">Sales</span></div>
            <div class="stat-block"><span class="stat-value">${formatMoney(p.total_volume)}</span><span class="stat-label">Total Volume</span></div>
          </div>
          <div class="detail-header__committees">
            <h3>Committees</h3>
            ${committeesHTML}
          </div>
        </div>
      </div>

      ${chartPanelHTML()}

      <h3 class="section-heading">Individual Trades</h3>
      <div id="politician-trades-table"></div>
    `;

    const chartPanel = container.querySelector('.chart-panel');
    wireRangeButtons(chartPanel, 'politicians', bioguideId);
    loadAndRenderVolumeChart(chartPanel, 'politicians', bioguideId, '6m');

    mountTradesTable(container.querySelector('#politician-trades-table'), p.trades || [], {
      showPoliticianColumn: false,
      searchable: true,
      emptyMessage: 'No trades found for this politician yet.',
      tableId: 'politician-detail-trades',
    });

    if (highlightTradeId) highlightTradeRow(container, highlightTradeId);
  } catch (err) {
    container.innerHTML = `<div class="empty-state"><strong>Failed to load politician.</strong>${escapeHtml(err.message)}</div>`;
  }
}

/* ---------------------------------------------------------------------
 * 7. Stocks list view
 * ------------------------------------------------------------------- */

// Column definitions for the Stocks list view's sortable headers. `type`
// drives the default sort direction when a column is first clicked ('desc'
// for numeric columns, so "most politicians"/"highest value" shows first;
// 'asc' for text, so it reads alphabetically) -- see the th.sortable click
// handler wired up in renderStocksView() below.
const STOCKS_VIEW_COLUMNS = [
  { key: 'ticker', label: 'Ticker', type: 'text' },
  { key: 'company_name', label: 'Company', type: 'text' },
  { key: 'politician_count', label: '# of politicians<br>holding this stock', type: 'number', thClass: 'th-center col-politician-count' },
  { key: 'trade_count', label: '# Trades', type: 'number', thClass: 'th-center' },
  { key: 'total_value', label: 'Total Value', type: 'number', thClass: 'th-center' },
];

const stocksViewState = { sortKey: 'politician_count', sortDir: 'desc' };

function stocksViewRowHTML(s) {
  return `
    <tr class="clickable" data-ticker="${escapeHtml(s.ticker)}">
      <td><strong>${escapeHtml(s.ticker)}</strong></td>
      <td class="wrap-cell">${escapeHtml(s.company_name || '—')}</td>
      <td class="td-center">${s.politician_count}</td>
      <td class="td-center">${s.trade_count}</td>
      <td class="td-center">${formatMoney(s.total_value)}</td>
    </tr>`;
}

function sortStocksViewRows(stocks) {
  const { sortKey, sortDir } = stocksViewState;
  const col = STOCKS_VIEW_COLUMNS.find((c) => c.key === sortKey);
  const rows = stocks.slice();
  rows.sort((a, b) => {
    let av = a[sortKey];
    let bv = b[sortKey];
    if (col && col.type === 'number') {
      av = av ?? 0;
      bv = bv ?? 0;
    } else {
      av = (av || '').toString().toLowerCase();
      bv = (bv || '').toString().toLowerCase();
    }
    if (av < bv) return sortDir === 'asc' ? -1 : 1;
    if (av > bv) return sortDir === 'asc' ? 1 : -1;
    return 0;
  });
  return rows;
}

function renderStocksViewTable(container, stocks) {
  const tbody = container.querySelector('tbody');
  if (!tbody) return;
  const rows = sortStocksViewRows(stocks);
  tbody.innerHTML = rows.map(stocksViewRowHTML).join('');
  tbody.querySelectorAll('tr.clickable').forEach((row) => {
    row.addEventListener('click', () => navigateTo(`#/stocks/${encodeURIComponent(row.dataset.ticker)}`));
  });
}

function updateStocksViewSortArrows(container) {
  container.querySelectorAll('th.sortable').forEach((th) => {
    const arrow = th.querySelector('.sort-arrow');
    arrow.textContent =
      th.dataset.key === stocksViewState.sortKey ? (stocksViewState.sortDir === 'asc' ? ' ▲' : ' ▼') : '';
  });
}

async function renderStocksView() {
  const container = document.getElementById('stocks-content');
  container.innerHTML = '<p class="loading-text">Loading…</p>';

  try {
    // Backed by sort_stocks_by_politician_count() on the server -- already
    // sorted most-widely-held first, so no client-side re-sort is needed.
    // include_politicians=false here since this list view links each row to
    // the stock detail page (which already lists every politician); we
    // don't need the full per-ticker politician list duplicated here too.
    const stocks = await fetchJSON('/api/stocks/sorted?min_politicians=2&include_politicians=false');

    if (!stocks.length) {
      container.innerHTML = `<div class="empty-state"><strong>No data yet</strong>Click "Refresh Data" above to load trade data.</div>`;
      return;
    }

    container.innerHTML = `
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              ${STOCKS_VIEW_COLUMNS.map(
                (c) =>
                  `<th data-key="${c.key}" class="sortable${c.thClass ? ` ${c.thClass}` : ''}">${c.label}<span class="sort-arrow"></span></th>`
              ).join('')}
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>`;

    renderStocksViewTable(container, stocks);
    updateStocksViewSortArrows(container);

    container.querySelectorAll('th.sortable').forEach((th) => {
      th.addEventListener('click', () => {
        const key = th.dataset.key;
        const col = STOCKS_VIEW_COLUMNS.find((c) => c.key === key);
        if (stocksViewState.sortKey === key) {
          stocksViewState.sortDir = stocksViewState.sortDir === 'asc' ? 'desc' : 'asc';
        } else {
          stocksViewState.sortKey = key;
          stocksViewState.sortDir = col && col.type === 'number' ? 'desc' : 'asc';
        }
        updateStocksViewSortArrows(container);
        renderStocksViewTable(container, stocks);
      });
    });
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Failed to load stocks: ${escapeHtml(err.message)}</div>`;
  }
}

/* ---------------------------------------------------------------------
 * 8. Stock detail view
 * ------------------------------------------------------------------- */

async function renderStockDetail(ticker) {
  const container = document.getElementById('stock-detail-content');
  container.innerHTML = '<p class="loading-text">Loading…</p>';

  try {
    const data = await fetchJSON(`/api/stocks/${encodeURIComponent(ticker)}`);

    const politiciansHTML = (data.politicians || [])
      .map((entry) => {
        const p = entry.politician || {};
        const hasId = !!p.bioguide_id;
        const [firstGuess, ...restGuess] = (p.full_name || '').split(' ');
        return `
          <div class="stock-politician-card">
            ${renderAvatar(p.photo_url, firstGuess, restGuess.join(' '))}
            <div class="stock-politician-card__body">
              <button class="stock-politician-card__name" ${hasId ? `data-bioguide="${escapeHtml(p.bioguide_id)}"` : 'disabled'}>
                ${escapeHtml(p.full_name || 'Unknown politician')}
              </button>
              <div class="politician-card__meta">
                ${p.party ? partyBadge(p.party) : ''}
                ${p.state ? `<span>${escapeHtml(p.state)}</span>` : ''}
              </div>
              <div class="stock-politician-card__stats">
                ${entry.buy_count || 0} buys &middot; ${entry.sell_count || 0} sells &middot; ${formatMoney(entry.total_volume)}
              </div>
            </div>
          </div>`;
      })
      .join('');

    container.innerHTML = `
      <h2 class="detail-header__name">${escapeHtml(data.ticker)}${
        data.company_name ? ` <span class="detail-header__subname">${escapeHtml(data.company_name)}</span>` : ''
      }</h2>
      <p class="disclaimer-note">${escapeHtml(data.disclaimer || '')}</p>

      <h3 class="section-heading">Politicians Who Traded ${escapeHtml(data.ticker)}</h3>
      <div class="stock-politician-list">
        ${politiciansHTML || '<p class="loading-text">No politician data available.</p>'}
      </div>

      ${chartPanelHTML()}

      <h3 class="section-heading">Trade History</h3>
      <div id="stock-trades-table"></div>
    `;

    container.querySelectorAll('.stock-politician-card__name[data-bioguide]').forEach((btn) => {
      btn.addEventListener('click', () => navigateTo(`#/politicians/${encodeURIComponent(btn.dataset.bioguide)}`));
    });

    const chartPanel = container.querySelector('.chart-panel');
    wireRangeButtons(chartPanel, 'stocks', ticker);
    loadAndRenderVolumeChart(chartPanel, 'stocks', ticker, '6m');

    mountTradesTable(container.querySelector('#stock-trades-table'), data.trades || [], {
      showPoliticianColumn: true,
      searchable: true,
      emptyMessage: 'No trade history found for this ticker.',
      tableId: 'stock-detail-trades',
    });
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Failed to load stock detail: ${escapeHtml(err.message)}</div>`;
  }
}

/* ---------------------------------------------------------------------
 * 9. Shared "trades table" component
 *    Used by the Recent Trades, politician detail, and stock detail views.
 *    Supports client-side search + column sorting, drag-to-reorder columns
 *    (persisted per table via localStorage when a tableId is given),
 *    per-row Notify and News buttons, and gold highlighting for trades
 *    that fall into a sector overseen by a committee the trading
 *    politician sits on.
 * ------------------------------------------------------------------- */

/** Reads a saved column order for `tableId` from localStorage, falling back
 * to (and folding in any unrecognized/new keys onto the end of) `defaultKeys`
 * if nothing is saved yet or the saved data is stale/invalid. */
function loadColumnOrder(tableId, defaultKeys) {
  if (!tableId) return defaultKeys.slice();
  try {
    const raw = localStorage.getItem(`columnOrder:${tableId}`);
    if (!raw) return defaultKeys.slice();
    const saved = JSON.parse(raw);
    if (!Array.isArray(saved)) return defaultKeys.slice();
    const savedValid = saved.filter((k) => defaultKeys.includes(k));
    const missing = defaultKeys.filter((k) => !savedValid.includes(k));
    return [...savedValid, ...missing];
  } catch (_) {
    return defaultKeys.slice();
  }
}

function saveColumnOrder(tableId, keys) {
  if (!tableId) return;
  try {
    localStorage.setItem(`columnOrder:${tableId}`, JSON.stringify(keys));
  } catch (_) {
    /* localStorage unavailable (e.g. private browsing) -- reordering still
       works for the current session, it just won't be remembered. */
  }
}

/** Renders a small colored single-letter party indicator (R/D/I), or
 * nothing if the trade never resolved to a current officeholder with a
 * known party -- we don't guess. */
function partyLetterBadge(party) {
  const p = (party || '').trim().toLowerCase();
  if (p.startsWith('d')) return `<span class="party-letter party-letter-dem" title="Democrat">D</span>`;
  if (p.startsWith('r')) return `<span class="party-letter party-letter-rep" title="Republican">R</span>`;
  if (p) return `<span class="party-letter party-letter-ind" title="${escapeHtml(party)}">I</span>`;
  return '';
}

// Pure-button columns (no data to read, just an action) -- rendered with
// tighter cell padding (see .col-btn in style.css) than data columns, and
// used to decide which header cells get that same tighter padding, so
// adding more of these doesn't keep pushing the table wider than it needs
// to be.
const ACTION_COLUMN_KEYS = new Set(['_notify', '_news', '_records']);

/** Renders a single <td> for one column of one trade row. */
function renderTradeCell(key, trade) {
  switch (key) {
    case 'ticker':
      return `<td><strong>${escapeHtml(trade.ticker || '—')}</strong></td>`;
    case 'asset_description':
      return `<td class="wrap-cell">${escapeHtml(trade.asset_description || '—')}</td>`;
    case 'party':
      return `<td>${partyLetterBadge(trade.party)}</td>`;
    case 'politician_name':
      return `<td><button class="politician-link ticker-link" data-bioguide="${escapeHtml(
        trade.bioguide_id || ''
      )}">${escapeHtml(trade.politician_name || '—')}</button></td>`;
    case 'transaction_type':
      return `<td>${transactionTypeBadge(trade.transaction_type)}</td>`;
    case 'transaction_date':
      return `<td>${formatDate(trade.transaction_date)}</td>`;
    case 'disclosure_date':
      return `<td>${formatDate(trade.disclosure_date)}</td>`;
    case 'amount_min':
      return `<td>${escapeHtml(trade.amount_range || '—')}</td>`;
    case '_profit_loss':
      return `<td>${profitLossBadge(trade)}</td>`;
    case '_notify':
      return `
        <td class="col-btn">
          <button class="btn btn-secondary btn-small notify-btn"
            data-ticker="${escapeHtml(trade.ticker || '')}"
            data-bioguide="${escapeHtml(trade.bioguide_id || '')}"
            data-politician-name="${escapeHtml(trade.politician_name || '')}">
            Notify
          </button>
        </td>`;
    case '_news':
      return `
        <td class="col-btn">
          <button class="btn btn-secondary btn-small news-btn"
            data-ticker="${escapeHtml(trade.ticker || '')}">
            News
          </button>
        </td>`;
    case '_records':
      return `
        <td class="col-btn">
          <button class="btn btn-secondary btn-small records-btn"
            data-source-url="${escapeHtml(trade.source_url || '')}">
            Records
          </button>
        </td>`;
    default:
      return '<td>—</td>';
  }
}

/** Renders one full <tr>, in whatever column order is currently active,
 * highlighted gold if the trade's ticker falls into a sector overseen by a
 * committee the trading politician sits on (see backend
 * `_annotate_committee_conflicts`). */
function tradeRowHTML(trade, columns) {
  const isConflict = !!trade.conflict_flag;
  const sectors = (trade.conflict_sectors || []).join(', ');
  const rowAttrs = isConflict
    ? ` class="trade-row-conflict" title="Potential committee overlap: ${escapeHtml(
        sectors
      )} is overseen by a committee this politician sits on"`
    : '';
  const idAttr = trade.id != null ? ` data-trade-id="${escapeHtml(String(trade.id))}"` : '';
  return `<tr${idAttr}${rowAttrs}>${columns.map((c) => renderTradeCell(c.key, trade)).join('')}</tr>`;
}

/**
 * Scrolls to and briefly highlights the trade row with the given trade id
 * within `container` (used when arriving from a notification link so the
 * user can immediately see which trade triggered it). No-ops if the row
 * isn't found (e.g. it was filtered/paginated out).
 */
function highlightTradeRow(container, tradeId) {
  requestAnimationFrame(() => {
    const row = container.querySelector(`tr[data-trade-id="${CSS.escape(String(tradeId))}"]`);
    if (!row) return;
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    row.classList.add('trade-row-highlighted');
    setTimeout(() => row.classList.remove('trade-row-highlighted'), 3000);
  });
}

/**
 * Renders a searchable, sortable, column-reorderable trades table into
 * `container`. `trades` is the full dataset; filtering/sorting happens
 * client-side. Pass a unique `tableId` in `opts` to remember that table's
 * column order across restarts (via localStorage); omit it for a
 * session-only (still fully working) reorder.
 */
function mountTradesTable(container, trades, opts = {}) {
  const {
    showPoliticianColumn = false,
    showPartyColumn = false,
    searchable = true,
    emptyMessage = 'No trades found.',
    defaultSortKey = 'transaction_date',
    tableId = null,
    paginate = false,
    pageSize = 50,
    // When set, `trades` holds only the current page's rows (fetched from
    // the server) rather than the full dataset -- pagination controls call
    // onPageChange(newPage) to fetch a different page instead of slicing
    // locally. { page, totalPages, totalRows, onPageChange }
    serverPagination = null,
  } = opts;

  const usePagination = paginate || !!serverPagination;
  const state = { sortKey: defaultSortKey, sortDir: 'desc', search: '', page: serverPagination ? serverPagination.page : 1 };

  const baseColumns = [
    { key: 'ticker', label: 'Ticker' },
    { key: 'asset_description', label: 'Asset' },
    ...(showPartyColumn ? [{ key: 'party', label: 'Party' }] : []),
    ...(showPoliticianColumn ? [{ key: 'politician_name', label: 'Politician' }] : []),
    { key: 'transaction_type', label: 'Type' },
    { key: 'transaction_date', label: 'Trade Date' },
    { key: 'disclosure_date', label: 'Disclosed' },
    { key: 'amount_min', label: 'Amount' },
    { key: '_profit_loss', label: 'Profit/Loss', sortable: false },
    { key: '_notify', label: '', sortable: false },
    { key: '_news', label: '', sortable: false },
    { key: '_records', label: '', sortable: false },
  ];
  const columnByKey = new Map(baseColumns.map((c) => [c.key, c]));
  let columns = loadColumnOrder(tableId, baseColumns.map((c) => c.key))
    .map((k) => columnByKey.get(k))
    .filter(Boolean);

  const hasConflicts = trades.some((t) => t.conflict_flag);

  function wireColumnDragAndDrop() {
    const ths = Array.from(container.querySelectorAll('th.draggable-col'));
    let dragKey = null;

    ths.forEach((th) => {
      th.addEventListener('dragstart', (e) => {
        dragKey = th.dataset.key;
        e.dataTransfer.effectAllowed = 'move';
        try {
          e.dataTransfer.setData('text/plain', dragKey);
        } catch (_) {
          /* some hosts restrict setData; the drag still works via the dragKey closure */
        }
        th.classList.add('dragging');
      });
      th.addEventListener('dragend', () => {
        th.classList.remove('dragging');
        ths.forEach((t) => t.classList.remove('drag-over'));
        dragKey = null;
      });
      th.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        if (th.dataset.key !== dragKey) th.classList.add('drag-over');
      });
      th.addEventListener('dragleave', () => th.classList.remove('drag-over'));
      th.addEventListener('drop', (e) => {
        e.preventDefault();
        th.classList.remove('drag-over');
        const targetKey = th.dataset.key;
        if (!dragKey || dragKey === targetKey) return;
        const fromIdx = columns.findIndex((c) => c.key === dragKey);
        const toIdx = columns.findIndex((c) => c.key === targetKey);
        if (fromIdx === -1 || toIdx === -1) return;
        const [moved] = columns.splice(fromIdx, 1);
        columns.splice(toIdx, 0, moved);
        saveColumnOrder(tableId, columns.map((c) => c.key));
        renderShell();
        applyAndRender();
      });
    });
  }

  function wireSortClicks() {
    container.querySelectorAll('th.sortable').forEach((th) => {
      th.addEventListener('click', () => {
        const key = th.dataset.key;
        if (state.sortKey === key) {
          state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
        } else {
          state.sortKey = key;
          state.sortDir = ['transaction_date', 'disclosure_date', 'amount_min'].includes(key) ? 'desc' : 'asc';
        }
        // Server-paginated tables only hold the current page's rows -- resetting
        // to page 1 here without actually fetching it would show a mismatched
        // "Page 1 of N" label, so leave the page alone and just re-sort in place.
        if (!serverPagination) state.page = 1;
        updateSortArrows();
        applyAndRender();
      });
    });
  }

  function updateSortArrows() {
    container.querySelectorAll('th.sortable').forEach((th) => {
      const arrow = th.querySelector('.sort-arrow');
      arrow.textContent = th.dataset.key === state.sortKey ? (state.sortDir === 'asc' ? ' \u25B2' : ' \u25BC') : '';
    });
  }

  function resetColumnOrder() {
    columns = baseColumns.slice();
    if (tableId) {
      try {
        localStorage.removeItem(`columnOrder:${tableId}`);
      } catch (_) {
        /* localStorage unavailable -- nothing to clear, in-session order still resets below */
      }
    }
    renderShell();
    applyAndRender();
    showToast('Column order reset to default.');
  }

  function renderShell() {
    container.innerHTML = `
      <div class="view-toolbar">
        ${
          searchable
            ? `<input type="search" class="trades-search" placeholder="Search ticker or description…" style="max-width:260px;padding:7px 10px;border-radius:6px;border:1px solid var(--color-border);font-size:13px;"/>`
            : '<span></span>'
        }
        <button type="button" class="btn btn-secondary btn-small reset-columns-btn" title="Restore the default column order">Reset Columns</button>
      </div>
      ${
        hasConflicts
          ? `<div class="table-legend"><span class="legend-swatch legend-swatch-gold"></span> Highlighted rows: this politician sits on a committee that oversees this trade's sector</div>`
          : ''
      }
      ${usePagination ? '<div class="pagination-slot pagination-slot-top"></div>' : ''}
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              ${columns
                .map(
                  (c) =>
                    `<th draggable="true" data-key="${c.key}" class="draggable-col ${
                      c.sortable === false ? '' : 'sortable'
                    } ${ACTION_COLUMN_KEYS.has(c.key) ? 'col-btn' : ''}" title="Drag to reorder columns">${escapeHtml(
                      c.label
                    )}<span class="sort-arrow"></span></th>`
                )
                .join('')}
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
      ${usePagination ? '<div class="pagination-slot pagination-slot-bottom"></div>' : ''}`;

    wireColumnDragAndDrop();
    wireSortClicks();
    updateSortArrows();

    const searchInput = container.querySelector('.trades-search');
    if (searchInput) {
      searchInput.value = state.search;
      searchInput.addEventListener(
        'input',
        debounce(() => {
          state.search = searchInput.value;
          if (!serverPagination) state.page = 1;
          applyAndRender();
        }, 250)
      );
    }

    const resetBtn = container.querySelector('.reset-columns-btn');
    if (resetBtn) resetBtn.addEventListener('click', resetColumnOrder);
  }

  function paginationBarHTML(totalRows, totalPages) {
    const startIdx = totalRows === 0 ? 0 : (state.page - 1) * pageSize + 1;
    const endIdx = Math.min(state.page * pageSize, totalRows);
    return `
      <div class="pagination-bar">
        <div class="pagination-row">
          <span class="pagination-info">Showing ${startIdx}–${endIdx} of ${totalRows.toLocaleString()}</span>
          <div class="pagination-controls">
            <button type="button" class="btn btn-secondary btn-small pagination-prev" ${
              state.page <= 1 ? 'disabled' : ''
            }>← Previous</button>
            <span class="pagination-page-jump">
              Page <input type="number" class="pagination-page-input" min="1" max="${totalPages}" value="${state.page}" aria-label="Page number" /> of ${totalPages}
            </span>
            <button type="button" class="btn btn-secondary btn-small pagination-next" ${
              state.page >= totalPages ? 'disabled' : ''
            }>Next →</button>
          </div>
        </div>
        <div class="pagination-total-count">${totalRows.toLocaleString()} record${totalRows === 1 ? '' : 's'} total</div>
      </div>`;
  }

  function renderPaginationBars(totalRows, totalPages) {
    const html = paginationBarHTML(totalRows, totalPages);
    container.querySelectorAll('.pagination-slot').forEach((slot) => {
      slot.innerHTML = html;
    });

    function goToPage(newPage) {
      newPage = Math.max(1, Math.min(totalPages, newPage));
      if (newPage === state.page) return;
      state.page = newPage;
      if (serverPagination) {
        serverPagination.onPageChange(newPage);
      } else {
        applyAndRender();
        container.querySelector('.table-wrap').scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }

    container.querySelectorAll('.pagination-prev').forEach((btn) => {
      btn.addEventListener('click', () => goToPage(state.page - 1));
    });
    container.querySelectorAll('.pagination-next').forEach((btn) => {
      btn.addEventListener('click', () => goToPage(state.page + 1));
    });
    container.querySelectorAll('.pagination-page-input').forEach((input) => {
      const jumpToInputValue = () => {
        const parsed = parseInt(input.value, 10);
        if (!parsed || parsed === state.page) {
          input.value = state.page; // reset to current page if invalid/unchanged
          return;
        }
        goToPage(parsed);
      };
      input.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        jumpToInputValue();
      });
      input.addEventListener('blur', jumpToInputValue);
    });
  }

  function applyAndRender() {
    const tbody = container.querySelector('tbody');
    if (!tbody) return;

    let rows = trades.slice();

    if (state.search) {
      const term = state.search.toLowerCase();
      rows = rows.filter(
        (t) =>
          (t.ticker || '').toLowerCase().includes(term) ||
          (t.asset_description || '').toLowerCase().includes(term) ||
          (t.politician_name || '').toLowerCase().includes(term)
      );
    }

    rows.sort((a, b) => {
      let av = a[state.sortKey];
      let bv = b[state.sortKey];
      if (state.sortKey === 'amount_min') {
        av = av ?? 0;
        bv = bv ?? 0;
      } else {
        av = (av || '').toString().toLowerCase();
        bv = (bv || '').toString().toLowerCase();
      }
      if (av < bv) return state.sortDir === 'asc' ? -1 : 1;
      if (av > bv) return state.sortDir === 'asc' ? 1 : -1;
      return 0;
    });

    const totalRows = serverPagination ? serverPagination.totalRows : rows.length;

    if (usePagination) {
      const totalPages = serverPagination
        ? serverPagination.totalPages
        : Math.max(1, Math.ceil(totalRows / pageSize));
      if (!serverPagination) {
        if (state.page > totalPages) state.page = totalPages;
        if (state.page < 1) state.page = 1;
        const startIdx = (state.page - 1) * pageSize;
        rows = rows.slice(startIdx, startIdx + pageSize);
      }
      // serverPagination's `trades` is already just the current page's rows.
      renderPaginationBars(totalRows, totalPages);
    }

    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="${columns.length}"><div class="empty-state">${escapeHtml(
        emptyMessage
      )}</div></td></tr>`;
      return;
    }

    tbody.innerHTML = rows.map((t) => tradeRowHTML(t, columns)).join('');

    tbody.querySelectorAll('.notify-btn').forEach((btn) => {
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            openNotifyModal({
              ticker: btn.dataset.ticker,
              bioguideId: btn.dataset.bioguide,
              politicianName: btn.dataset.politicianName,
            });
          });
        });

        tbody.querySelectorAll('.news-btn').forEach((btn) => {
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            callNews(btn.dataset.ticker);
          });
        });

        tbody.querySelectorAll('.records-btn').forEach((btn) => {
          btn.addEventListener('click', (e) => {
            e.stopPropagation();
            callRecords(btn.dataset.sourceUrl);
          });
        });

    tbody.querySelectorAll('.politician-link').forEach((link) => {
      link.addEventListener('click', (e) => {
        e.stopPropagation();
        const id = link.dataset.bioguide;
        if (id) navigateTo(`#/politicians/${encodeURIComponent(id)}`);
      });
    });
  }

  renderShell();
  applyAndRender();
}

/* ---------------------------------------------------------------------
 * 10. Chart.js helpers
 * ------------------------------------------------------------------- */

const VOLUME_RANGES = [
  { key: '1m', label: '1M' },
  { key: '3m', label: '3M' },
  { key: '6m', label: '6M' },
  { key: 'ytd', label: 'YTD' },
  { key: '1y', label: '1Y' },
  { key: '5y', label: '5Y' },
];

/** Returns the HTML for a chart panel: heading, range buttons, and an empty chart container. */
function chartPanelHTML() {
  return `
    <div class="chart-panel">
      <div class="chart-panel__header">
        <h3 class="section-heading" style="margin:0;">Trading Volume (Buy vs Sell)</h3>
        <div class="range-buttons">
          ${VOLUME_RANGES.map(
            (r) => `<button class="range-btn${r.key === '6m' ? ' active' : ''}" data-range="${r.key}">${r.label}</button>`
          ).join('')}
        </div>
      </div>
      <div class="chart-container"><p class="loading-text">Loading chart…</p></div>
    </div>`;
}

/** Wires up the 1M/3M/6M/YTD/1Y/5Y buttons inside a chart panel. */
function wireRangeButtons(panelEl, kind, id) {
  panelEl.querySelectorAll('.range-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      panelEl.querySelectorAll('.range-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      loadAndRenderVolumeChart(panelEl, kind, id, btn.dataset.range);
    });
  });
}

/** Fetches volume data for a politician or stock and (re)draws the chart. */
async function loadAndRenderVolumeChart(panelEl, kind, id, range) {
  const container = panelEl.querySelector('.chart-container');
  container.innerHTML = '<p class="loading-text">Loading chart…</p>';

  try {
    const url =
      kind === 'politicians'
        ? `/api/politicians/${encodeURIComponent(id)}/volume?range=${encodeURIComponent(range)}`
        : `/api/stocks/${encodeURIComponent(id)}/volume?range=${encodeURIComponent(range)}`;
    const data = await fetchJSON(url);

    if (typeof window.Chart === 'undefined') {
      container.innerHTML = `<div class="chart-fallback-msg">
        Chart.js was not found (missing <code>frontend/vendor/chart.min.js</code>), so the volume
        chart can't be displayed. See the comment at the top of that file for instructions.
      </div>`;
      return;
    }

    if (!data.labels || !data.labels.length) {
      container.innerHTML = '<div class="empty-state">No volume data for this time range.</div>';
      return;
    }

    container.innerHTML = '<canvas></canvas>';
    initVolumeChart(container.querySelector('canvas'), data);
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Failed to load chart data: ${escapeHtml(err.message)}</div>`;
  }
}

/** Draws a grouped bar chart of monthly buy vs sell volume onto `canvasEl`. */
function initVolumeChart(canvasEl, data) {
  if (typeof window.Chart === 'undefined') return null;

  if (currentChartInstance) {
    currentChartInstance.destroy();
    currentChartInstance = null;
  }

  const theme = chartThemeColors();

  currentChartInstance = new Chart(canvasEl.getContext('2d'), {
    type: 'bar',
    data: {
      labels: data.labels,
      datasets: [
        {
          label: 'Buys',
          data: data.buy,
          backgroundColor: 'rgba(21, 128, 61, 0.75)',
          borderRadius: 3,
          maxBarThickness: 28,
        },
        {
          label: 'Sells',
          data: data.sell,
          backgroundColor: 'rgba(185, 28, 28, 0.75)',
          borderRadius: 3,
          maxBarThickness: 28,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 11 }, color: theme.tick } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${formatMoneyFull(ctx.parsed.y)}`,
          },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { font: { size: 11 }, color: theme.tick } },
        y: {
          beginAtZero: true,
          ticks: { font: { size: 11 }, callback: (v) => formatMoney(v), color: theme.tick },
          grid: { color: theme.grid },
        },
      },
    },
  });

  return currentChartInstance;
}

/* ---------------------------------------------------------------------
 * 11. Notify modal (per politician + stock alert preference)
 *     Saves a preference only -- no alert-sending/trigger logic yet.
 * ------------------------------------------------------------------- */

let notifyModalContext = null;
let notifyModalExistingPref = null;

/** Opens the Notify modal for one politician + stock, prefilling it with
 * any already-saved preference for that pair. */
async function openNotifyModal({ ticker, bioguideId, politicianName }) {
  if (!ticker) {
    showToast('No ticker available for this trade.');
    return;
  }
  notifyModalContext = { ticker, bioguideId: bioguideId || '', politicianName: politicianName || '' };
  notifyModalExistingPref = null;

  const modal = document.getElementById('notify-modal');
  const subtitle = document.getElementById('notify-modal-subtitle');
  const status = document.getElementById('notify-status');
  const deleteBtn = document.getElementById('notify-delete-btn');
  const thresholdInput = document.getElementById('notify-amount-threshold');
  const comparisonSelect = document.getElementById('notify-amount-comparison');

  subtitle.textContent = politicianName
    ? `${politicianName} \u2014 ${ticker.toUpperCase()}`
    : ticker.toUpperCase();
  status.textContent = '';
  deleteBtn.classList.add('hidden');
  thresholdInput.value = '';
  comparisonSelect.value = 'above';
  document.querySelector('input[name="notify-direction"][value="either"]').checked = true;

  modal.classList.remove('hidden');

  try {
    const params = new URLSearchParams({ ticker });
    if (bioguideId) params.set('bioguide_id', bioguideId);
    if (politicianName) params.set('politician_name', politicianName);
    const matches = await fetchJSON(`/api/notification-preferences?${params.toString()}`);
    const existing = matches[0];
    if (existing) {
      notifyModalExistingPref = existing;
      document.querySelector(
        `input[name="notify-direction"][value="${existing.direction}"]`
      ).checked = true;
      comparisonSelect.value = existing.amount_comparison;
      thresholdInput.value = existing.amount_threshold;
      deleteBtn.classList.remove('hidden');
      status.textContent = 'An alert is already saved for this politician + stock.';
    }
  } catch (err) {
    status.textContent = `Could not check for an existing alert: ${err.message}`;
  }
}

function closeNotifyModal() {
  document.getElementById('notify-modal').classList.add('hidden');
  notifyModalContext = null;
  notifyModalExistingPref = null;
}

async function saveNotifyPreference() {
  const status = document.getElementById('notify-status');
  if (!notifyModalContext) return;

  const direction = document.querySelector('input[name="notify-direction"]:checked').value;
  const amountComparison = document.getElementById('notify-amount-comparison').value;
  const amountThreshold = parseFloat(document.getElementById('notify-amount-threshold').value);

  if (Number.isNaN(amountThreshold) || amountThreshold < 0) {
    status.textContent = 'Enter a valid, non-negative amount.';
    return;
  }

  try {
    await fetchJSON('/api/notification-preferences', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bioguide_id: notifyModalContext.bioguideId,
        politician_name: notifyModalContext.politicianName,
        ticker: notifyModalContext.ticker,
        direction,
        amount_comparison: amountComparison,
        amount_threshold: amountThreshold,
      }),
    });
    showToast('Alert saved.');
    closeNotifyModal();
    refreshNotificationsBadge();
  } catch (err) {
    status.textContent = `Failed to save alert: ${err.message}`;
  }
}

async function deleteNotifyPreference() {
  const status = document.getElementById('notify-status');
  if (!notifyModalExistingPref) return;
  try {
    await fetchJSON(`/api/notification-preferences/${notifyModalExistingPref.id}`, { method: 'DELETE' });
    showToast('Alert removed.');
    closeNotifyModal();
  } catch (err) {
    status.textContent = `Failed to remove alert: ${err.message}`;
  }
}

/* ---------------------------------------------------------------------
 * 12. News button handler / toast
 * ------------------------------------------------------------------- */

function showToast(message, duration = 2500) {
  const toastEl = document.getElementById('toast');
  toastEl.textContent = message;
  toastEl.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.add('hidden'), duration);
}

/**
 * Builds a Yahoo Finance "News" tab URL for `ticker`, e.g.
 * https://finance.yahoo.com/quote/LHX/news/ -- the ticker's own page,
 * scoped straight to its latest news.
 */
function yahooFinanceNewsUrl(ticker) {
  const symbol = encodeURIComponent((ticker || '').trim().toUpperCase());
  return `https://finance.yahoo.com/quote/${symbol}/news/`;
}

/**
 * Opens the ticker's Yahoo Finance News page (see yahooFinanceNewsUrl) in a
 * new browser tab. Purely client-side now that the app always runs in a
 * real browser tab -- no backend round-trip needed.
 */
function callNews(ticker) {
  if (!ticker) {
    showToast('No ticker available for this trade.');
    return;
  }
  const url = yahooFinanceNewsUrl(ticker);
  window.open(url, '_blank', 'noopener,noreferrer');
}

/**
 * Opens the official filing this trade came from (the House Clerk/Senate
 * PTR PDF itself, see backend's source_url) in a new browser tab -- the
 * primary-source, human-readable record a trade row was extracted from,
 * not a re-formatted summary. Left disclosed-but-unlinked rather than
 * guessed at if a trade has no source_url on file (e.g. an older
 * community/fallback source that didn't provide one).
 */
function callRecords(sourceUrl) {
  if (!sourceUrl) {
    showToast('No official record link is on file for this trade.');
    return;
  }
  window.open(sourceUrl, '_blank', 'noopener,noreferrer');
}

/* ---------------------------------------------------------------------
 * 13. App bootstrap
 * ------------------------------------------------------------------- */

function init() {
  initTheme();

  if (typeof window.Chart === 'undefined') {
    console.warn(
      'Chart.js was not found on window. Volume charts will show a fallback message. ' +
        'See the comment at the top of frontend/vendor/chart.min.js for how to add it.'
    );
  }

  wireHeaderEvents();
  populateFilterOptions();
  updateMeta();
  startBackgroundMetaWatcher();
  startNotificationsPolling();
  checkForAppUpdate();
  setInterval(checkForAppUpdate, 6 * 60 * 60 * 1000); // re-check every 6 hours

  window.addEventListener('hashchange', router);
  router();
}

document.addEventListener('DOMContentLoaded', init);
