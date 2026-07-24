"""
Best-effort ticker resolution for trades that disclosed an asset NAME but no
ticker symbol.

The House's *paper* checkbox PTR form (see pipeline/checkbox_form.py)
explicitly says "provide full name, not ticker symbol" -- so every trade
recovered from those scanned filings (e.g. Rep. Khanna's thousands of rows)
arrives with an asset description like "PEPSICO, INC. CMN" and no ticker,
which locks it out of every ticker-keyed feature (price history, profit/
loss estimates, the News button, per-stock pages).

Rather than calling some external symbol-search API, this resolves names
against the app's OWN data: the e-filed trades already in the database
carry both a ticker and a disclosure-style company name for ~1,700+
distinct securities -- the exact naming universe congressional filings use.
Resolution is tiered, strictest first, and every tier only accepts a
UNIQUE answer (one distinct ticker); anything ambiguous stays blank rather
than guessed:

  T1  exact normalized-token match       ("PEPSICO, INC. CMN" == "PepsiCo,
                                          Inc. - Common Stock" after both
                                          normalize to ('PEPSICO',))
  T2  known name's tokens are a subset   (OCR junk words around a real
      of the row's tokens                name: "feemumscwmean PEPSICO...")
  T3  known name is a substring of the   (OCR gluing words together:
      row's de-spaced text               "PFIZERINC." contains "PFIZER")
  T4  high-cutoff fuzzy match on the     (OCR-garbled letters:
      de-spaced text (difflib >= 0.88)   "MICROSGFTCORPORATION" ~
                                         "MICROSOFT...")

Resolved tickers are written back onto the trades rows (never overwriting a
disclosed ticker -- only filling blanks). Rows that stay blank are honest:
municipal bonds, private/hedge funds and the like genuinely have no ticker,
and unreadable OCR is left unresolved rather than guessed. Runs as part of
every refresh (see data_fetch.refresh_data) so newly loaded paper-form
trades get resolved, and the index improves as more e-filed data arrives.
"""

import difflib
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

# Corporate-suffix and security-type words that carry no identity: dropped
# before comparing names. Includes the common OCR misreads of "CMN" seen on
# real scanned filings (CRIN/CMIN).
_STOP_TOKENS = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD",
    "PLC", "LP", "LLC", "THE",
    "COMMON", "STOCK", "CMN", "CRIN", "CMIN", "SHARES", "SHS", "CL",
    "CLASS", "ORDINARY", "ADR", "ADS", "NEW",
}

# A name key must keep at least this much substance to participate in the
# looser tiers -- short keys ("KO") would substring-match everywhere.
_MIN_SUBSET_KEY_CHARS = 5
_MIN_SUBSTRING_KEY_CHARS = 6
_MIN_FUZZY_CHARS = 8
_FUZZY_CUTOFF = 0.88

# When the same normalized name maps to more than one ticker across the
# e-filed data (share classes, OCR'd tickers, plain data noise), the key is
# only trusted if one ticker clearly dominates.
_MAJORITY_SHARE = 0.9

# Defense in depth against ticker-extraction artifacts already present in
# the data (parenthesized name words like "(The)"/"(New)" misread as
# tickers -- see house_clerk._NOT_A_TICKER): never let them into the index,
# where one bad row would spread its "ticker" to every matching clean name.
_BLACKLISTED_TICKERS = {"THE", "NEW", "OLD", "DEL"}


def _tokens(name: str) -> Tuple[str, ...]:
    return tuple(
        t for t in re.split(r"[^A-Z0-9]+", (name or "").upper()) if t and t not in _STOP_TOKENS
    )


# Stop-words to peel off the END of a de-spaced blob when OCR glued them
# straight onto the company name ('MICROSGFTCORPORATIONCMN'). Only
# unambiguous multi-character suffixes -- notably NOT 'CO', which is a
# legitimate ending of real names ('COSTCO').
_BLOB_TRAILING_STOPS = sorted(
    ["CORPORATION", "INCORPORATED", "COMPANY", "HOLDINGS", "COMMON",
     "STOCK", "SHARES", "CLASS", "CMN", "CRIN", "CMIN", "CORP", "INC"],
    key=len, reverse=True,
)


def _strip_trailing_stops(blob: str) -> str:
    """Iteratively removes glued trailing stop-words from a de-spaced name
    blob, never shrinking it below 6 characters (so a strip can't eat into
    the company name itself)."""
    changed = True
    while changed:
        changed = False
        for suffix in _BLOB_TRAILING_STOPS:
            if blob.endswith(suffix) and len(blob) - len(suffix) >= 6:
                blob = blob[: -len(suffix)]
                changed = True
    return blob


def build_name_index(conn):
    """Builds the name->ticker lookup from every ticker-bearing trade
    already in the database. Returns (exact_index, concat_index) where
    exact_index maps normalized token tuples to a ticker and concat_index
    is a list of (despaced_name, ticker) for the substring/fuzzy tiers.
    Keys whose ticker isn't consistent enough (see _MAJORITY_SHARE) are
    dropped entirely -- an ambiguous key must never resolve anything."""
    votes: Dict[Tuple[str, ...], Counter] = defaultdict(Counter)
    for row in conn.execute(
        "SELECT ticker, asset_description FROM trades "
        "WHERE ticker IS NOT NULL AND ticker != '' AND asset_description != ''"
    ):
        if row["ticker"] in _BLACKLISTED_TICKERS:
            continue
        toks = _tokens(row["asset_description"])
        if toks:
            votes[toks][row["ticker"]] += 1

    exact_index: Dict[Tuple[str, ...], str] = {}
    for toks, counter in votes.items():
        ticker, count = counter.most_common(1)[0]
        if count / sum(counter.values()) >= _MAJORITY_SHARE:
            exact_index[toks] = ticker

    concat_index: List[Tuple[str, str]] = [
        ("".join(toks), ticker)
        for toks, ticker in exact_index.items()
        if len("".join(toks)) >= _MIN_SUBSTRING_KEY_CHARS
    ]
    return exact_index, concat_index


def _resolve_one(name: str, exact_index, concat_index) -> Optional[str]:
    """Resolves a single asset name to a ticker via the tiers described in
    the module docstring, or None. Every tier requires a unique answer."""
    toks = _tokens(name)
    if not toks:
        return None

    # T1: exact normalized match.
    hit = exact_index.get(toks)
    if hit:
        return hit

    # T2: a known name's tokens all appear among this row's tokens (extra
    # OCR junk words around a real name).
    tokset = set(toks)
    subset_hits = {
        ticker
        for key, ticker in exact_index.items()
        if len("".join(key)) >= _MIN_SUBSET_KEY_CHARS and set(key) <= tokset
    }
    if len(subset_hits) == 1:
        return subset_hits.pop()
    if len(subset_hits) > 1:
        return None  # genuinely ambiguous -- never guess between candidates

    # T3: known name as a substring of the row's de-spaced text (OCR glued
    # words together). Glued trailing stop-words are peeled off first so
    # the fuzzy tier below compares name against name, not name against
    # name-plus-'CORPORATIONCMN'.
    blob = _strip_trailing_stops("".join(toks))
    substring_hits = {ticker for key, ticker in concat_index if key in blob}
    if len(substring_hits) == 1:
        return substring_hits.pop()
    if len(substring_hits) > 1:
        return None

    # T4: high-cutoff fuzzy match for OCR-garbled letters. get_close_matches
    # sorts by similarity; only trust a clear, unique-enough winner.
    if len(blob) >= _MIN_FUZZY_CHARS:
        keys = [key for key, _ in concat_index]
        close = difflib.get_close_matches(blob, keys, n=2, cutoff=_FUZZY_CUTOFF)
        if close:
            winners = {ticker for key, ticker in concat_index if key in close}
            if len(winners) == 1:
                return winners.pop()
    return None


def resolve_missing_tickers(conn, progress_cb=None) -> Dict[str, int]:
    """Fills in tickers for trades that have an asset description but no
    ticker, resolving each DISTINCT name once (paper-form filers repeat the
    same holdings monthly, so distinct names are few even when rows are
    thousands). Never overwrites an existing ticker. Returns
    {"resolved_names": ..., "updated_rows": ...}."""
    exact_index, concat_index = build_name_index(conn)

    distinct = [
        row["asset_description"]
        for row in conn.execute(
            "SELECT DISTINCT asset_description FROM trades "
            "WHERE (ticker IS NULL OR ticker = '') AND asset_description != ''"
        )
    ]

    resolved_names = 0
    updated_rows = 0
    for i, name in enumerate(distinct):
        ticker = _resolve_one(name, exact_index, concat_index)
        if not ticker:
            continue
        cur = conn.execute(
            "UPDATE trades SET ticker = ? "
            "WHERE asset_description = ? AND (ticker IS NULL OR ticker = '')",
            (ticker, name),
        )
        resolved_names += 1
        updated_rows += cur.rowcount
        if progress_cb and i % 200 == 0:
            progress_cb(f"Resolving tickers from asset names… ({i}/{len(distinct)} names)")

    return {"resolved_names": resolved_names, "updated_rows": updated_rows}
