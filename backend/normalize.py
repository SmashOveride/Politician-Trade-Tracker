"""
Shared normalization helpers for turning free-text disclosure data (dollar
amount ranges, transaction type strings, dates, politician names) into the
common shapes used across every data source -- the legacy JSON-dump sources
in data_fetch.py, and the new House Clerk / Senate eFD pipeline in
backend/pipeline/. Kept dependency-free (no db/network access) so it's safe
to import from anywhere without risk of circular imports.
"""

import re
from datetime import datetime

_NAME_STOPWORDS = {
    "hon", "sen", "rep", "dr", "mr", "mrs", "ms", "jr", "sr", "ii", "iii", "iv",
    "md", "phd", "esq", "facs", "dds",
}


def parse_amount_range(amount_str):
    """Parse strings like '$1,001 - $15,000' or '$1,000,001 - $5,000,000' into
    (min, max) floats. Returns (None, None) if unparseable."""
    if not amount_str:
        return None, None
    nums = re.findall(r"[\d,]+(?:\.\d+)?", amount_str)
    nums = [float(n.replace(",", "")) for n in nums if n.replace(",", "").strip()]
    if len(nums) >= 2:
        return nums[0], nums[1]
    if len(nums) == 1:
        return nums[0], nums[0]
    return None, None


def normalize_transaction_type(raw_type):
    """Normalizes a variety of source-specific transaction type strings
    ('Purchase', 'Sale (Partial)', 'S', 'P', 'buy', etc.) into one of
    'purchase' | 'sale' | 'sale_partial' | 'exchange' | 'unknown'."""
    if not raw_type:
        return "unknown"
    t = raw_type.strip().lower()
    if t in ("p", "buy") or "purchase" in t:
        return "purchase"
    if "sale (partial)" in t or "sale_partial" in t or t == "s (partial)":
        return "sale_partial"
    if "sale (full)" in t or t in ("s", "sale"):
        return "sale"
    if t == "e" or "exchange" in t:
        return "exchange"
    return t


def clean_name_tokens(name):
    """Lowercase, strip punctuation, and drop titles/suffixes from a free-text
    disclosure name, returning the remaining ordered tokens (first ... last)."""
    if not name:
        return []
    name = name.lower()
    name = re.sub(r"[^a-z\s]", " ", name)
    tokens = [t for t in name.split() if t and t not in _NAME_STOPWORDS]
    return tokens


def norm_date(date_str):
    """Normalizes a variety of date string formats (most commonly MM/DD/YYYY)
    into ISO YYYY-MM-DD. Returns '' if the string can't be parsed."""
    if not date_str:
        return ""
    date_str = date_str.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def within_trade_history_window(tx_date, cutoff):
    """Returns True if `tx_date` (an ISO date string, possibly empty/
    unparsed) should be kept given a retention/refresh `cutoff` (ISO date
    string). Only ever excludes a row when we can positively confirm it's
    older than the cutoff -- rows with a missing/unparsable transaction date
    are kept rather than guessed away."""
    if not cutoff or not tx_date:
        return True
    return tx_date >= cutoff


def resolve_bioguide(name, bioguide_lookup_by_name):
    """Matches a free-text disclosure name (which often includes middle
    initials, suffixes like Jr./MD, or slightly different formatting than the
    official record) against a last-name-indexed lookup of
    {last_name_token: [(first_name_tokens_set, bioguide_id), ...]}.
    Disambiguates same-surname collisions using first-name token overlap."""
    tokens = clean_name_tokens(name)
    if not tokens:
        return None
    last = tokens[-1]
    first_tokens = tokens[:-1]
    candidates = bioguide_lookup_by_name.get(last)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][1]
    for cand_first_tokens, bio in candidates:
        for tok in first_tokens:
            if tok in cand_first_tokens or any(
                t.startswith(tok) or tok.startswith(t) for t in cand_first_tokens if t and tok
            ):
                return bio
    return None
