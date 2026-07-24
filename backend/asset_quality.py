"""
Shared logic for labeling trade rows whose asset name can't be trusted --
either OCR produced gibberish text, or a filing failed to parse at all and
no name was ever extracted from it.

Both cases show the same "Unreadable -- See Records" placeholder in the UI
(the Records button on that row still links to the original filing PDF),
so a user browsing the app sees an honest, uniform signal rather than
confusing garbled text -- while the underlying raw OCR text (if any) stays
untouched in the database for later manual review/matching (see
ticker_resolve.py and the khanna_garbled_review workflow).

is_garbled() is a heuristic, validated by hand against Rep. Khanna's
scanned paper-form disclosures (the app's primary source of OCR'd asset
names) -- it has known false positives on short, vowel-light but real
names (e.g. "WIX.COM LTD CMN"). That's why callers (see app.py's
_trade_row_to_dict) only ever apply it to rows already known to be
OCR-sourced (trades.ocr_sourced) -- natively parsed e-filed text is
essentially never gibberish, so this is never even checked there, and a
resolved ticker always takes precedence over the label regardless (a row
we've matched to a real ticker is meaningfully identified even if its raw
text looks rough).
"""

import re

UNREADABLE_ASSET_LABEL = "Unreadable — See Records"

# A long token with almost no vowels is very unlikely to be a real word.
_MIN_LETTERS_FOR_VOWEL_CHECK = 6
_MIN_VOWEL_RATIO = 0.18

# 4+ of the same letter in a row -- see is_garbled()'s use of this below.
_REPEATED_CHAR_RE = re.compile(r"([A-Za-z])\1{3,}")

# A short (1-3 char) run repeated 4+ times in a row, e.g. "oscscscscsc" --
# broader than _REPEATED_CHAR_RE, which only catches a single character
# repeating. Confirmed on real data (McCaul's worst scans): this pattern
# evades every other check (mixed case, scattered vowels, no single
# 4-repeated letter) while still being obvious textured-scan-artifact
# gibberish -- verified against every other already-confirmed-good name in
# the database: zero false positives.
_REPEATING_PATTERN_RE = re.compile(r"(.{1,3})\1{3,}")

# Owner-code prefix the paper checkbox form prints before each asset name
# (SP/DC/JT, with OCR's common misreads OC/BC/PC/SE for DC/SP) -- see
# checkbox_form.py's identical pattern, used there to strip this same
# prefix before matching. (?![A-Za-z]) rather than \b: OCR glues
# separators straight onto the code ('DC_|DANAHER'), and '_' is a word
# character, so \b fails to match exactly the cases this exists for.
_OWNER_PREFIX_RE = re.compile(
    r"^[\s_\|\[\]\(\)\{\}:.\-]*(sp|dc|jt|oc|bc|pc|se)(?![A-Za-z])[\s_\|\[\]\(\)\{\}:.\-]*",
    re.IGNORECASE,
)

# "Common Stock" -- spelled out (as clean e-filed disclosures usually
# write it, often with a leading dash: "- Common Stock") or abbreviated to
# 3 letters and mis-OCR'd on scanned forms (CMN/CRIN/CMIN) -- either way
# it's pure instrument-type suffix noise, never part of what identifies
# the company, so always dropped. Trailing qualifiers that DO carry
# identifying meaning (e.g. "Class A", "Class B") are deliberately left
# alone -- only "Common Stock" itself (spelled out or abbreviated) is
# suffix noise here.
_COMMON_STOCK_SUFFIX_RE = re.compile(
    r"(\s*-\s*)?\bCommon\s+Stock\b|\b(CMN|CRIN|CMIN)\b", re.IGNORECASE
)

_LEADING_TRAILING_JUNK_RE = re.compile(r"^[\s,.\-_|\[\](){}'\"`~;*#]+|[\s,.\-_|\[\](){}'\"`~;*#]+$")

# Structural noise characters that also turn up stuck in the MIDDLE of an
# otherwise-legible name, not just at the edges (e.g. "Fa ; Sp Icmn Class B",
# "Corporationcmn * A") -- _LEADING_TRAILING_JUNK_RE only ever strips from
# the ends, so these need their own pass. Never appear in a real disclosed
# asset name, so safe to blank out unconditionally.
_STRAY_SYMBOL_RE = re.compile(r"[;*#~]")


def is_garbled(name):
    """True if `name` looks like OCR noise rather than a real disclosed
    asset name. Never raises; empty input is treated as not garbled (there
    are separate, more direct checks for "no name at all")."""
    if not name:
        return False
    if "�" in name:  # the OCR replacement character always means garbage
        return True
    # Strip pure suffix/prefix noise before judging substance -- a raw
    # value that's LITERALLY JUST that noise (e.g. a bare ", CMN") would
    # otherwise slip through as "not garbled": extracting letters from the
    # RAW text (pre-strip) picks up "CMN" itself as if it were 3 letters
    # of real content, which is both short enough to skip the vowel-ratio
    # check below and all-caps enough to dodge the lowercase check too.
    # Confirmed on real data: this let dozens of Khanna rows whose entire
    # content was noise get (wrongly) treated as a real, if terse, name.
    # Deliberately NOT a minimum-length threshold on its own -- legitimate
    # short content exists too (bond/note descriptions like "BP 3.8500%
    # 07/01/25 33" have only 2 real letters, "BP", once dates/percentages
    # are excluded), so the only safe universal signal is "nothing
    # survives noise-stripping at all", not "not much survives".
    substantive = _COMMON_STOCK_SUFFIX_RE.sub("", _OWNER_PREFIX_RE.sub("", name))
    letters = re.sub(r"[^A-Za-z]", "", substantive)
    if not letters:
        return True
    # Real disclosure forms print asset names in ALL CAPS, so text that's
    # majority lowercase is characteristic of misread OCR noise rather than
    # an actual scanned form.
    if sum(1 for ch in letters if ch.islower()) / len(letters) > 0.5:
        return True
    if len(letters) >= _MIN_LETTERS_FOR_VOWEL_CHECK:
        vowel_ratio = sum(1 for ch in letters.upper() if ch in "AEIOU") / len(letters)
        if vowel_ratio < _MIN_VOWEL_RATIO:
            return True
    # A run of 4+ identical letters in a row doesn't happen in real company
    # names/disclosure text -- it's characteristic of OCR misreading a
    # solid/textured scan artifact (e.g. a repeated-dash table border) as
    # letters. Confirmed on real data (McCaul's scanned filings): strings
    # like "Csnoooooooocnc" or "Ssoscsossssc" evaded every check above
    # (mixed case dodges the lowercase-ratio rule, and they're long enough
    # with enough scattered vowels to dodge the vowel-ratio rule too) despite
    # being obvious gibberish to a human reader. Verified against every
    # already-confirmed-good asset name in the database: zero false
    # positives, so this is safe to apply universally rather than needing
    # its own narrower gating.
    if _REPEATED_CHAR_RE.search(letters):
        return True
    if _REPEATING_PATTERN_RE.search(letters):
        return True
    return False


def clean_company_name_text(raw):
    """Best-effort cleanup of a garbled-but-legible OCR'd asset name into a
    readable, presentable company name -- strips the owner-code prefix and
    "Common Stock" suffix noise, then Title Cases the result (matching how
    a human would write "Walmart Inc" rather than the disclosure form's
    "WALMART INC").

    Deliberately does NOT try to strip leading noise *words* (only
    structural punctuation/owner-codes) -- an earlier version stripped any
    lowercase run before a capitalized word, on the theory that real
    disclosure text is printed ALL CAPS so lowercase text must be OCR
    noise. That's true for noise like "feemumscwmean PEPSICO..." but also
    destroyed real, legitimately-lowercase-styled company names like
    "aptiv" (-> incorrectly left just "Plc"), confirmed on real data. No
    regex can reliably tell "noise word" from "real lowercase brand name"
    without an actual name reference, so this only removes noise it can
    identify structurally, never by casing alone.

    This is NOT a substitute for ticker-based canonical-name lookup where
    a ticker is available (see the Khanna asset-column cleanup script) --
    OCR can substitute a wrong letter into an otherwise well-formed name
    (e.g. "SAXTER INTERNATIONAL INC" for "BAXTER..."), which this has no
    way to detect or correct; it only removes noise around a name it
    trusts is already substantively correct. Only meant for text that
    is_garbled() has already deemed NOT garbled -- this doesn't fix
    genuine gibberish, it just tidies up real names that came with noise
    stuck to them."""
    text = raw or ""
    text = _OWNER_PREFIX_RE.sub("", text)
    text = _COMMON_STOCK_SUFFIX_RE.sub("", text)
    text = _STRAY_SYMBOL_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    text = _LEADING_TRAILING_JUNK_RE.sub("", text)
    return text.strip().title()
