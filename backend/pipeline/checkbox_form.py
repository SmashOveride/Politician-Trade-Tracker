"""
Parser for the House's *paper* PTR form -- the checkbox-grid variant that
paper filers' offices fill out and scan (confirmed real-world example: every
one of Rep. Ro Khanna's PTRs, filed monthly for years, all in this format).

Unlike the e-filed PTR PDFs (a normal text table that house_clerk.py's
header-calibrated parser reads), the paper form is a printed grid where:

- Transaction type is NOT text: it's an "x" mark in one of five checkbox
  columns (Purchase / Sale / Exchange / Capital Gain >$200 / Partial Sale).
- Amount is NOT text either: an "x" mark in one of eleven lettered columns
  (A..J = the standard disclosure dollar buckets, K = "transaction in a
  spouse or dependent" -- not an amount, ignored here).
- The scans are noisy enough that the header row OCRs to garbage, and the
  checkbox "x" marks themselves come back as unusable noise characters
  ('|', 'ta', 'fy', ...), so no amount of text-side parsing can recover
  type/amount -- confirmed empirically before this module existed.

So this parser deliberately does NOT read the header or the marks as text.
Instead it works from what IS reliable on these scans:

1. The transaction/notification DATE text in each row OCRs well -- dates are
   found by regex and their x-positions cluster into the two date columns,
   anchoring both the rows (one per transaction-date token) and the grid.
2. The form's printed vertical grid lines are strong pixel features --
   detected directly from a binarized page image (column ink-density
   profile), giving every column's true pixel boundaries per page, robust
   to per-scan margins/scale.
3. The two detected date columns are matched to their grid cells, which
   pins down the whole template: the five type-checkbox cells sit
   immediately left of the transaction-date cell, the amount cells A..K
   immediately right of the notification-date cell, and the asset name is
   the text left of the checkbox region.
4. Checkbox marks are then read as *pixels*: the ink fraction inside each
   (shrunken, to exclude the grid lines) cell interior of the row's band.
   An "x" is dramatically more ink than an empty box.

Everything degrades honestly: if the grid can't be confidently mapped on a
page, that page's rows are still emitted with the asset/date text but with
type 'unknown' and no amount, rather than guessed. The paper form also has
no ticker column at all ("provide full name, not ticker symbol"), so trades
from it will genuinely have ticker=None -- the app already treats that as
"no ticker disclosed" rather than an error.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageStat

# MM/DD/YY or MM/DD/YYYY anywhere inside a token (OCR routinely glues
# brackets/pipes onto the ends: '01/12/23]', '04/06/23}').
_DATE_TOKEN_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")

# The paper form's amount buckets, in printed left-to-right column order
# (columns A..J). Strings match the app's existing amount_range format so
# normalize.parse_amount_range() reads them identically to e-filed trades.
AMOUNT_BUCKETS = [
    "$1,001 - $15,000",
    "$15,001 - $50,000",
    "$50,001 - $100,000",
    "$100,001 - $250,000",
    "$250,001 - $500,000",
    "$500,001 - $1,000,000",
    "$1,000,001 - $5,000,000",
    "$5,000,001 - $25,000,000",
    "$25,000,001 - $50,000,000",
    "Over $50,000,000",
]

# Candidate ink-fraction thresholds for vertical grid-line detection, tried
# in order until one produces a mapping that passes the template validation
# below. Too high and a faint printed line drops out (shifting every cell
# index -- confirmed failure mode on a real scan at 0.55, where losing one
# line silently mis-assigned the asset/checkbox cells); too low and the
# vertical strokes of the date digits themselves start reading as "lines".
# The validation makes a wrong mapping fail loudly instead of parsing
# garbage, so trying several thresholds is safe.
_GRID_LINE_THRESHOLDS = (0.45, 0.40, 0.50, 0.35)

# Plausible detected-line-count window for this 20-column form (owner,
# asset, 5 type checkboxes, 2 dates, 11 amounts => 21 boundary lines);
# outside it, the "grid" is probably not this form.
_GRID_LINE_COUNT_RANGE = (12, 40)

# Template cell-width sanity ranges (PDF points), used to validate a
# candidate grid mapping. On the printed form the five type-checkbox cells
# and eleven amount cells are narrow (~19-29pt), the date cells ~28-37pt,
# and the asset cell is wide (~185pt) -- a mapping that violates these has
# mis-detected/miscounted lines and must be rejected, never trusted.
_CHECKBOX_CELL_WIDTH = (13, 48)
_DATE_CELL_WIDTH = (18, 55)
_ASSET_CELL_MIN_WIDTH = 80
_MIN_AMOUNT_CELLS = 8

# How far above the row's own baseline ink level a cell must be to count
# as marked. Empty cells on this form are NOT clean -- the printed row
# guides give every cell a baseline ink fraction of ~0.07-0.10 that varies
# by row/scan -- so marks are judged relative to the median ink fraction
# across the row's 15 checkbox+amount cells rather than any absolute
# threshold. Calibrated on a visually-verified real page: unmarked cells
# sat within ~0.02 of their row's baseline, real 'x' marks 0.04-0.11 above
# it.
_MARK_MARGIN = 0.03

# A row whose cells are nearly all "marked" isn't a transaction -- it's the
# printed column-header band or a solid section-separator bar (both ink
# every cell). Guarded two ways: too many type cells above baseline, or a
# baseline itself so high the band must be a solid bar.
_MAX_MARKED_TYPE_CELLS = 3
_MAX_ROW_BASELINE = 0.35

# How much of each cell edge to shave off before measuring ink, so the
# printed grid lines themselves (and slight scan skew) don't count as marks.
_CELL_SHRINK = 0.25

# Vertical extent of one row's checkbox band, in PDF points, relative to
# the row's transaction-date text top. Rows on this form are ~20.6pt apart,
# so this window covers one row without touching its neighbors.
_ROW_BAND_ABOVE = 6
_ROW_BAND_BELOW = 14

# Two date tokens whose x-centers differ by less than this (points) belong
# to the same date column; the printed date columns are ~38pt apart.
_DATE_CLUSTER_GAP = 12

# (?![A-Za-z]) rather than \b: OCR glues separators straight onto the code
# ('DC_|DANAHER', 'OC__|MARSH'), and '_' is a word character, so \b would
# fail to match exactly the cases this exists for. 'OC'/'BC'/'PC' are the
# common OCR misreads of 'DC'; 'SE' of 'SP'.
_OWNER_PREFIX_RE = re.compile(
    r"^[\s_\|\[\]\(\)\{\}:.\-]*(sp|dc|jt|oc|bc|pc|se)(?![A-Za-z])[\s_\|\[\]\(\)\{\}:.\-]*",
    re.IGNORECASE,
)
_LEADING_NOISE_RE = re.compile(r"^[\s_\|\[\]\(\)\{\}:.\-]+")


def _plausible_date(mm, dd, yy) -> Optional[str]:
    """Returns 'MM/DD/YYYY' if the captured groups look like a real modern
    filing date, else None. 2-digit years are expanded as 20xx (this form
    only exists in the electronic archive from the 2000s on). The upper
    bound is next year, not some far-future cap: transaction dates are
    always in the past, and OCR junk from ratio-looking header text ('3/3]
    38/8') otherwise fabricates plausible-shaped dates years in the future
    (observed on a real scan as '02/02/2038')."""
    from datetime import date

    mm, dd, yy = int(mm), int(dd), int(yy)
    if yy < 100:
        yy += 2000
    if not (1 <= mm <= 12 and 1 <= dd <= 31 and 2000 <= yy <= date.today().year + 1):
        return None
    return f"{mm:02d}/{dd:02d}/{yy:04d}"


def _find_date_words(words) -> List[Dict[str, Any]]:
    """Returns [{'word', 'date' (normalized or None), 'xc' (x-center)}] for
    every token containing a date-shaped fragment with plausible month/day.
    Implausible values are dropped entirely so stray OCR junk can't anchor
    a column."""
    out = []
    for w in words:
        m = _DATE_TOKEN_RE.search(w["text"])
        if not m:
            continue
        date = _plausible_date(*m.groups())
        if date is None:
            continue
        out.append({"word": w, "date": date, "xc": (w["x0"] + w["x1"]) / 2.0})
    return out


def _cluster_by_x(date_words) -> List[List[Dict[str, Any]]]:
    """Groups date tokens into x-position clusters (sorted left to right),
    splitting wherever the gap between neighbors exceeds _DATE_CLUSTER_GAP."""
    ordered = sorted(date_words, key=lambda d: d["xc"])
    clusters: List[List[Dict[str, Any]]] = []
    for d in ordered:
        if clusters and d["xc"] - clusters[-1][-1]["xc"] <= _DATE_CLUSTER_GAP:
            clusters[-1].append(d)
        else:
            clusters.append([d])
    return clusters


def _column_ink_profile(mark_image, y0: int, y1: int) -> List[float]:
    """Per-x-column ink fraction over the [y0, y1) band of the binarized
    page (computed in one C-speed resize rather than per-pixel Python)."""
    w, h = mark_image.size
    y0 = max(0, min(int(y0), h - 1))
    y1 = max(y0 + 1, min(int(y1), h))
    band = mark_image.crop((0, y0, w, y1)).convert("L")
    resample = getattr(getattr(Image, "Resampling", Image), "BOX")
    return [v / 255.0 for v in band.resize((w, 1), resample).getdata()]


def _grid_lines_at(profile: List[float], threshold: float) -> List[float]:
    """The x pixel positions of vertical grid lines: runs of columns whose
    ink fraction meets `threshold`, merged to each run's center (a thick or
    slightly skewed line spans a few pixel columns)."""
    lines: List[float] = []
    run_start = None
    for x in range(len(profile) + 1):
        hit = x < len(profile) and profile[x] >= threshold
        if hit and run_start is None:
            run_start = x
        elif not hit and run_start is not None:
            lines.append((run_start + x - 1) / 2.0)
            run_start = None
    return lines


def _validate_mapping(lines: List[float], tx_xc_px: float, notif_xc_px: Optional[float], scale: float):
    """Tries to anchor the template on `lines`: locates the cell holding the
    transaction-date column and checks the whole neighborhood against the
    printed form's known cell-width proportions (see the _*_WIDTH
    constants). Returns the tx cell index if everything checks out, else
    None -- a mapping that fails any check is worthless (one missed or
    spurious line shifts every cell assignment), so nothing is salvaged."""
    if not (_GRID_LINE_COUNT_RANGE[0] <= len(lines) <= _GRID_LINE_COUNT_RANGE[1]):
        return None
    tx_cell = _cell_index_for(tx_xc_px, lines)
    if tx_cell is None or tx_cell < 6:
        return None
    if notif_xc_px is not None and _cell_index_for(notif_xc_px, lines) != tx_cell + 1:
        return None
    if tx_cell + 1 >= len(lines) - 1:
        return None

    def width_pt(i):
        return (lines[i + 1] - lines[i]) / scale

    lo, hi = _DATE_CELL_WIDTH
    if not (lo <= width_pt(tx_cell) <= hi and lo <= width_pt(tx_cell + 1) <= hi):
        return None
    lo, hi = _CHECKBOX_CELL_WIDTH
    for c in range(tx_cell - 5, tx_cell):
        if not (lo <= width_pt(c) <= hi):
            return None
    if width_pt(tx_cell - 6) < _ASSET_CELL_MIN_WIDTH:
        return None
    amount_cells = len(lines) - 1 - (tx_cell + 2)
    if amount_cells < _MIN_AMOUNT_CELLS:
        return None
    return tx_cell


def _cell_ink_fraction(mark_image, x0: float, x1: float, y0: float, y1: float) -> float:
    """Ink fraction inside the cell interior, with _CELL_SHRINK margins
    shaved off every edge so the grid lines themselves don't count."""
    dx = (x1 - x0) * _CELL_SHRINK
    dy = (y1 - y0) * _CELL_SHRINK
    box = (int(x0 + dx), int(y0 + dy), int(x1 - dx), int(y1 - dy))
    if box[2] <= box[0] or box[3] <= box[1]:
        return 0.0
    w, h = mark_image.size
    box = (max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3]))
    if box[2] <= box[0] or box[3] <= box[1]:
        return 0.0
    return ImageStat.Stat(mark_image.crop(box).convert("L")).mean[0] / 255.0


def _cell_index_for(x: float, lines: List[float]) -> Optional[int]:
    """Index of the cell (gap between consecutive grid lines) containing x,
    or None if x falls outside the grid."""
    for i in range(len(lines) - 1):
        if lines[i] <= x < lines[i + 1]:
            return i
    return None


def _clean_asset_text(tokens: List[str]) -> str:
    """Joins and cleans the asset-cell tokens: strips the owner-code prefix
    (SP/DC/JT, with OCR variants like 'OC' and glued separators such as
    'DC_|DANAHER'), leading grid-noise characters, and collapses spaces."""
    text = " ".join(tokens)
    text = _OWNER_PREFIX_RE.sub("", text)
    text = _LEADING_NOISE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" |_[](){}-")
    return text


def _parse_page(page) -> List[Dict[str, Any]]:
    """Parses one page dict (from ocr.ocr_pdf_pages) into raw transaction
    dicts shaped like house_clerk's pre-_finalize rows:
    {'owner', 'asset_lines', 'txtype', 'txdate', 'amount', 'in_metadata'}."""
    words = page["words"]
    mark_image = page["mark_image"]
    scale = page["scale"]

    date_words = _find_date_words(words)
    if not date_words:
        return []

    clusters = sorted(_cluster_by_x(date_words), key=len, reverse=True)[:2]
    clusters.sort(key=lambda c: c[0]["xc"])
    tx_cluster = clusters[0]
    notif_cluster = clusters[1] if len(clusters) > 1 else []
    # Guard against splitting one real column in two on a page with a
    # single stray date elsewhere: the tx column should hold most dates.
    if len(tx_cluster) < 2 and notif_cluster:
        tx_cluster, notif_cluster = notif_cluster, tx_cluster

    tx_xc = sorted(d["xc"] for d in tx_cluster)[len(tx_cluster) // 2]
    notif_xc = (
        sorted(d["xc"] for d in notif_cluster)[len(notif_cluster) // 2] if notif_cluster else None
    )

    tops = [d["word"]["top"] for d in tx_cluster]
    profile = _column_ink_profile(
        mark_image, (min(tops) - 12) * scale, (max(tops) + 18) * scale
    )

    # Map the grid onto the template, anchored on the date columns: try
    # each detection threshold until one yields a line set that passes the
    # full template validation. If none does, rows still get parsed from
    # text alone (type 'unknown', no amount) rather than mis-assigned.
    grid_lines: List[float] = []
    tx_cell = None
    for threshold in _GRID_LINE_THRESHOLDS:
        candidate = _grid_lines_at(profile, threshold)
        mapped = _validate_mapping(
            candidate, tx_xc * scale, notif_xc * scale if notif_xc is not None else None, scale
        )
        if mapped is not None:
            grid_lines, tx_cell = candidate, mapped
            break

    # Anchors, best-first:
    # 1. transaction-date tokens (row top + transaction date),
    # 2. notification-date tokens on rows with no usable transaction date
    #    (the tx date OCR'd to garbage, e.g. 'atfoa/z3|' on a real scan) --
    #    the row's asset/type/amount are still recoverable, only its
    #    transaction_date is honestly left blank rather than guessed,
    # 3. synthesized row slots (below): the form prints rows at a fixed
    #    pitch, so rows where BOTH dates OCR'd to garbage still sit at
    #    predictable tops between the anchored ones. Those only become rows
    #    if their band actually contains asset text plus checkbox marks.
    anchors: List[Tuple[float, str, bool]] = [
        (d["word"]["top"], d["date"], False)
        for d in sorted(tx_cluster, key=lambda d: d["word"]["top"])
    ]
    tx_tops = [t for t, _, _ in anchors]
    for d in sorted(notif_cluster, key=lambda d: d["word"]["top"]):
        if not any(abs(d["word"]["top"] - t) < 8 for t in tx_tops):
            anchors.append((d["word"]["top"], "", False))
    anchors.sort(key=lambda a: a[0])

    if tx_cell is not None and len(anchors) >= 3:
        known = [t for t, _, _ in anchors]
        diffs = [b - a for a, b in zip(known, known[1:])]
        small = [x for x in diffs if x <= 1.5 * min(diffs)]
        if len(small) >= 2 and min(small) > 12:
            pitch = sorted(small)[len(small) // 2]
            # Fill interior gaps between anchored rows at pitch spacing.
            slots: List[float] = []
            for a, b in zip(known, known[1:]):
                steps = round((b - a) / pitch)
                for k in range(1, steps):
                    slots.append(a + (b - a) * k / steps)
            # Extend a little beyond the first/last anchored row too.
            for k in range(1, 4):
                slots.append(known[0] - k * pitch)
                slots.append(known[-1] + k * pitch)
            for s in slots:
                if not any(abs(s - t) < pitch * 0.45 for t, _, _ in anchors):
                    anchors.append((s, "", True))
            anchors.sort(key=lambda a: a[0])

    rows: List[Dict[str, Any]] = []
    seen_tops: List[float] = []
    for top, txdate, synthetic in anchors:
        if any(abs(top - t) < 8 for t in seen_tops):
            continue  # duplicate OCR hit on the same physical row
        seen_tops.append(top)

        # Asset text: everything left of the checkbox region on this row's
        # text line. With a mapped grid that's the cells before the first
        # type checkbox; without one, fall back to "well left of the date
        # columns". The text band is tighter than the checkbox band below:
        # the asset name sits on the anchor line itself, and a looser band
        # was observed pulling in OCR smear from the neighboring row.
        if tx_cell is not None:
            asset_right = grid_lines[tx_cell - 5] / scale
        else:
            asset_right = tx_xc - 50
        def asset_in_band(above, below):
            band_words = [
                w
                for w in words
                if (top - above) <= w["top"] <= (top + below) and w["x1"] <= asset_right + 2
            ]
            return _clean_asset_text(
                [w["text"] for w in sorted(band_words, key=lambda w: w["x0"])]
            )

        # Tight band first; if it comes up empty, retry a wider one -- a
        # notification-date anchor can sit several points below the row's
        # actual text baseline on skewed scans (confirmed on a real row
        # that was otherwise lost).
        asset = asset_in_band(6, 8)
        if len(re.sub(r"[^A-Za-z0-9]", "", asset)) < 3:
            asset = asset_in_band(11, 12)
        if len(re.sub(r"[^A-Za-z0-9]", "", asset)) < 3:
            continue  # noise-only row (stray date match in a header/footer)

        txtype = ""
        amount = ""
        if tx_cell is not None:
            y0 = (top - _ROW_BAND_ABOVE) * scale
            y1 = (top + _ROW_BAND_BELOW) * scale

            def ink(cell_idx):
                return _cell_ink_fraction(
                    mark_image, grid_lines[cell_idx], grid_lines[cell_idx + 1], y0, y1
                )

            # Five checkbox cells immediately left of the tx-date cell, in
            # printed order: Purchase, Sale, Exchange, CapGain>$200, Partial
            # Sale -- then the amount cells A..J immediately right of the
            # notification-date cell (the 11th, K, is the spouse/dependent
            # flag, not an amount).
            type_fracs = [ink(tx_cell - 5 + k) for k in range(5)]
            amount_cells = list(
                range(tx_cell + 2, min(tx_cell + 2 + len(AMOUNT_BUCKETS), len(grid_lines) - 1))
            )
            amount_fracs = [ink(c) for c in amount_cells]

            # Marks are judged against this row's own baseline ink level
            # (the form's printed row guides ink every cell a little, and
            # how much varies by row/scan) -- see _MARK_MARGIN.
            all_fracs = sorted(type_fracs + amount_fracs)
            baseline = all_fracs[len(all_fracs) // 2]
            marked_bar = baseline + _MARK_MARGIN

            purchase_f, sale_f, exchange_f, _capgain_f, partial_f = type_fracs
            type_marked_count = sum(1 for f in type_fracs if f >= marked_bar)
            if baseline > _MAX_ROW_BASELINE or type_marked_count > _MAX_MARKED_TYPE_CELLS:
                continue  # solid section bar or the printed header band

            type_scores = [("P", purchase_f), ("S", sale_f), ("E", exchange_f)]
            best_type, best_f = max(type_scores, key=lambda t: t[1])
            if best_f >= marked_bar:
                txtype = best_type
                if txtype == "S" and partial_f >= marked_bar:
                    txtype = "S (partial)"

            if amount_fracs:
                best_i = max(range(len(amount_fracs)), key=lambda i: amount_fracs[i])
                if amount_fracs[best_i] >= marked_bar:
                    amount = AMOUNT_BUCKETS[best_i]

        # A synthesized slot (no date text at all) must prove itself with
        # both a type mark and an amount mark to count as a transaction --
        # asset-region text alone also appears on section-title rows.
        if synthetic and not (txtype and amount):
            continue

        # A row with NO checkbox marks at all (no mapped grid, or nothing
        # cleared the bar) is only worth keeping when its asset text looks
        # like real words -- date-shaped OCR junk from ratio-like header
        # text otherwise fabricates rows like '2) ale' (observed on a real
        # scan). Rows with actual marks already proved themselves.
        if not txtype and not amount and len(re.sub(r"[^A-Za-z]", "", asset)) < 6:
            continue

        rows.append(
            {
                "owner": "",
                "asset_lines": [asset],
                "txtype": txtype,
                "txdate": txdate,
                "amount": amount,
                "in_metadata": False,
                "_top": top,
            }
        )

    return rows


def parse_pages(pages) -> Tuple[List[Dict[str, Any]], int]:
    """Parses every page (from ocr.ocr_pdf_pages) as the paper checkbox PTR
    form. Returns (raw_transactions, pages_with_rows) where raw_transactions
    are dicts in house_clerk's pre-_finalize shape. An empty list simply
    means 'this doesn't look like the checkbox form' -- callers treat that
    as this fallback not applying, never as an error."""
    all_rows: List[Dict[str, Any]] = []
    pages_with_rows = 0
    for page in pages:
        try:
            rows = _parse_page(page)
        except Exception:
            rows = []  # a bad page never sinks the rest of the filing
        if rows:
            pages_with_rows += 1
            all_rows.extend(rows)
    for r in all_rows:
        r.pop("_top", None)
    return all_rows, pages_with_rows
