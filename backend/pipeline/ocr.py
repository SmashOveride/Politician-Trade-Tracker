"""
Optional OCR fallback for scanned/image-only PDF disclosures.

Some House Clerk PTR PDFs are filed as flat scanned images with no embedded
text layer at all (rather than the usual machine-generated text). For
those, pdfplumber's extract_words() returns nothing on every page, so
house_clerk.py's column-position-based table parser can't find a
recognizable transaction table header, and the filing would otherwise be
reported as an unrecognized format and skipped entirely.

This module renders each page of such a PDF to an image and runs Tesseract
OCR (via the pytesseract binding) over it, returning word-level results in
the same shape pdfplumber's Page.extract_words() produces -- a list of
dicts with 'text', 'x0', 'x1', and 'top' in PDF point units -- so callers
can reuse their existing position-based table-reconstruction logic
unchanged, just fed OCR'd words instead of pdfplumber's native ones.

OCR support is entirely optional and degrades gracefully:
- The packaged Windows build bundles Tesseract itself (see
  packaging/fetch_tesseract_windows.py and windows.spec) -- is_available()
  detects and uses that bundled copy automatically, nothing to install.
- Running from source, or on macOS/Linux, Tesseract is a system-level
  binary this app doesn't bundle. If it (or the pytesseract package) isn't
  installed, is_available() returns False and callers fall back to their
  pre-OCR behavior (reporting the filing as an unrecognized format) --
  exactly as before OCR support existed. This is a bonus capability, never
  a requirement.
- See README.md's "Optional OCR support" section for manual install
  instructions on platforms without a bundled copy.
"""

import io
import logging
import os
import sys

import pdfplumber

logger = logging.getLogger("politician_trades.pipeline")

# 300 DPI is a common sweet spot for OCR accuracy on scanned government
# filings without making rendering/processing each page too slow or
# memory-hungry. OCR is only ever attempted on the (rare) filings that
# pdfplumber's normal text extraction couldn't read at all, not on every
# filing, so this cost is only paid when it's actually needed.
OCR_RESOLUTION = 300

# Tesseract's image_to_data() reports -1 confidence for boxes it considers
# non-text (e.g. stray marks/lines) -- filter those out, but otherwise keep
# everything (even low-confidence words), since the downstream table
# parsers already validate shapes (dates, "$" amounts, P/S/E codes, etc.)
# before trusting a row as a real transaction, so a few noisy OCR words
# that don't fit those shapes are simply ignored rather than mis-parsed.
MIN_CONFIDENCE = 0

# Tesseract's default page segmentation mode (PSM 3, fully automatic layout
# analysis) tries to classify regions of the page as text/table/image
# before OCR-ing them -- on a dense, grid-lined PTR table it can badly
# under-segment, treating most of the page as non-text and OCR-ing only a
# small fraction of it (confirmed: 34 words recovered from a page that
# visually has a full ~19-row table, vs 260 with the mode below, on the
# same image). PSM 6 ("assume a single uniform block of text") skips that
# region classification and just reads everything, which -- combined with
# the strict shape validation already done downstream (dates, "$" amounts,
# P/S/E codes) to decide what's a real transaction row -- consistently
# recovers far more real content on these forms without regressing
# already-working filings (also verified).
TESSERACT_CONFIG = "--psm 6"

_checked = False
_available = False
_pytesseract = None


def _bundled_tesseract_paths():
    """Returns (tesseract_exe_path, tessdata_dir_path) for a Tesseract
    build shipped with this app on Windows, or (None, None) when there
    isn't one -- e.g. macOS/Linux, which rely on a system-installed
    Tesseract instead (see is_available() below). Two locations:

    - Packaged app: the `tesseract/` folder PyInstaller bundled into the
      build (see packaging/fetch_tesseract_windows.py and windows.spec).
      sys._MEIPASS is where PyInstaller actually puts bundled `datas` --
      for this onedir build that's the `_internal` folder next to the exe,
      not the exe's own directory (which holds only the exe itself).
    - Running from source: the staged vendor copy at
      packaging/vendor/tesseract-windows/, when present. Without this, a
      source checkout on a machine with no system Tesseract silently ran
      with OCR unavailable even though a fully working bundled copy sat
      right there in the repo (confirmed: a whole refresh "re-parsed" the
      scanned filings in seconds by skipping OCR entirely).
    """
    if sys.platform != "win32":
        return None, None
    if getattr(sys, "frozen", False):
        base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        candidate = os.path.join(base_dir, "tesseract")
    else:
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        candidate = os.path.join(project_root, "packaging", "vendor", "tesseract-windows")
    exe_path = os.path.join(candidate, "tesseract.exe")
    tessdata_dir = os.path.join(candidate, "tessdata")
    if os.path.exists(exe_path):
        return exe_path, tessdata_dir
    return None, None


def is_available():
    """Returns True if OCR support (the pytesseract package and the
    Tesseract OCR engine binary it wraps) is installed and usable. The
    check is only performed once and cached; this never raises. Prefers the
    bundled Tesseract build in the packaged Windows app, if present, over
    checking for a system install."""
    global _checked, _available, _pytesseract
    if _checked:
        return _available
    _checked = True
    try:
        import pytesseract

        bundled_exe, bundled_tessdata = _bundled_tesseract_paths()
        if bundled_exe:
            pytesseract.pytesseract.tesseract_cmd = bundled_exe
            # Set directly rather than via a `--tessdata-dir "..."` config
            # string -- pytesseract invokes tesseract.exe without a shell,
            # so quotes meant to protect a path containing spaces are never
            # stripped and get passed through as literal characters instead,
            # corrupting the path (confirmed: this broke OCR entirely until
            # switched to the env var).
            os.environ["TESSDATA_PREFIX"] = bundled_tessdata

        pytesseract.get_tesseract_version()
        _pytesseract = pytesseract
        _available = True
    except Exception as e:
        logger.info("OCR fallback unavailable (%s) -- continuing without it", e)
        _available = False
    return _available


def _correct_page_orientation(image):
    """Detects and corrects a scanned page's rotation using Tesseract's
    orientation/script detection (OSD, via the bundled osd.traineddata)
    before the main word-level OCR pass. Some real House PTR filings are
    scanned with the whole page rotated 90 degrees (confirmed: e.g. Rep.
    Khanna's filings, apparently scanned in landscape) -- fed to OCR as-is,
    every word comes back in a reading order that doesn't correspond to the
    real rows/columns at all, so the table-reconstruction logic can't make
    sense of any of it even though the text itself OCRs fine once upright.

    Best-effort and per-page (different pages of the same scanned filing
    can end up rotated differently, e.g. if they were fed into a scanner
    inconsistently): any OSD failure (blank page, too little text to judge
    orientation, etc.) is treated as "no rotation needed" rather than an
    error. OSD's own confidence score is not used as a gate here -- checked
    empirically against both a correctly-oriented filing and a rotated one,
    confidence values were actually *lower* on the genuinely-rotated dense
    checkbox-table content than on already-upright pages, i.e. inverted
    from what a confidence gate would assume, so trusting the detected
    rotation directly outperforms filtering on it."""
    try:
        osd = _pytesseract.image_to_osd(image, output_type=_pytesseract.Output.DICT)
        rotate = osd.get("rotate", 0)
    except Exception:
        return image
    return image.rotate(rotate, expand=True) if rotate else image


# Grayscale threshold below which a pixel counts as "ink" when building the
# 1-bit mark image used by checkbox_form.py's grid/checkbox detection. These
# are high-contrast B/W government scans, so anything meaningfully darker
# than paper is ink; 150 keeps light scanner noise out without losing faint
# grid lines.
BINARIZE_THRESHOLD = 150

# Tesseract's OSD reliably finds a rotated page's *axis* but sometimes picks
# the wrong *direction* along it (confirmed on a real filing: every page
# corrected to exactly 180 degrees off, all its text OCR-ing as mirrored
# gibberish like 'A1LVYOdYOONI dNOYD' for 'GROUP INCORPORATED'). Upside-down
# text tanks Tesseract's own per-word confidence, so when a page's mean
# confidence comes back below this, the page is re-OCR'd rotated 180 and
# the higher-confidence result wins (measured on that real filing: 26.6
# mean conf / 131 words upside down vs 51.9 / 357 upright). Costs one extra
# OCR pass only on pages that already read as garbage.
_RETRY_UPSIDE_DOWN_BELOW_CONF = 45
_RETRY_KEEP_MARGIN = 5

# A page whose primary (PSM 6) pass finds unusually few words is likely
# mostly blank -- confirmed on a real filing page with only 4 filled rows
# atop a ~50-row printed grid: PSM 6's "one uniform block of text"
# assumption badly under-reads a mostly-empty page (mean confidence 21.0,
# vs 36.2 from PSM 3's region-based segmentation on the *same* image), the
# opposite of the tradeoff on a fully-packed page (PSM 6 gets 260 words at
# conf 46.3; PSM 3 gets only 34 -- fewer, more confident, but missing most
# of the grid, since it treats most of a busy page as non-text).
#
# So below this word count, a supplementary PSM 3 pass is tried and REPLACES
# PSM 6's result if its confidence is clearly better. This can't regress a
# dense page (that branch is never reached above _SPARSE_PAGE_WORD_COUNT,
# where PSM 6 stays authoritative) -- and simply keeping whichever
# non-overlapping words PSM 3 adds doesn't work here: PSM 6 still emits
# *some* (garbage) word at nearly every position it scans, so a real PSM 3
# word and PSM 6's garbage reading of the same pixels occupy the same
# bounding box and "overlap" -- confirmed: an add-only merge picked up only
# 2 of PSM 3's words on the real failing page. A full replace, gated on
# this branch only firing when PSM 6 already measured as inadequate, is
# safe and actually recovers the page's content.
_SPARSE_PAGE_WORD_COUNT = 100
_SPARSE_PAGE_CONFIG = "--psm 3"


def ocr_pdf_pages(raw_bytes):
    """Renders every page of the PDF `raw_bytes` to an image, corrects its
    orientation, and OCRs it. Returns a list with one dict per page:

        {
            "words": [...],        # pdfplumber-shaped word dicts ('text',
                                   # 'x0', 'x1', 'top'), in PDF point units
            "mark_image": <PIL '1'-mode image>,  # binarized page (ink=white),
                                   # same orientation the words were read at
            "scale": float,        # pixels per PDF point for mark_image
        }

    The mark_image is kept 1-bit so holding every page of a long filing in
    memory stays cheap (~1MB/page at 300dpi); it exists for
    checkbox_form.py, which needs actual pixels (grid lines, checkbox
    marks) that OCR's text output can't represent. Returns [] if OCR isn't
    available or the PDF can't be rendered/processed at all -- callers
    should treat that the same as "OCR didn't help here" rather than a hard
    error, since this is always a best-effort fallback on top of the normal
    parse path."""
    if not is_available():
        return []

    scale = OCR_RESOLUTION / 72.0
    pages = []
    try:
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            for page in pdf.pages:
                image = page.to_image(resolution=OCR_RESOLUTION, antialias=True).original
                image = _correct_page_orientation(image)
                data = _pytesseract.image_to_data(image, config=TESSERACT_CONFIG, output_type=_pytesseract.Output.DICT)
                conf = _mean_word_confidence(data)

                if conf < _RETRY_UPSIDE_DOWN_BELOW_CONF:
                    # Both this page's actual orientation AND its best PSM
                    # are uncertain at this point, and they interact: trying
                    # them one stage at a time (flip, *then* maybe swap PSM)
                    # can lock in a wrong early choice and never revisit it.
                    # Confirmed on a real page: orientation-only retry chose
                    # "flipped" because 28.0 > 21.0+margin, even though the
                    # *original* orientation was actually correct and just
                    # needed PSM 3, not PSM 6, to read it (36.2) -- flipped
                    # was never anything but wrong-side-up garbage. So all
                    # three alternatives are tried and scored together
                    # against the same original-orientation/PSM-6 baseline,
                    # and whichever genuinely reads best wins.
                    candidates = [(image, data, conf)]
                    flipped = image.rotate(180)
                    flipped_data = _pytesseract.image_to_data(
                        flipped, config=TESSERACT_CONFIG, output_type=_pytesseract.Output.DICT
                    )
                    candidates.append((flipped, flipped_data, _mean_word_confidence(flipped_data)))
                    try:
                        sparse_data = _pytesseract.image_to_data(
                            image, config=_SPARSE_PAGE_CONFIG, output_type=_pytesseract.Output.DICT
                        )
                        candidates.append((image, sparse_data, _mean_word_confidence(sparse_data)))
                        flipped_sparse_data = _pytesseract.image_to_data(
                            flipped, config=_SPARSE_PAGE_CONFIG, output_type=_pytesseract.Output.DICT
                        )
                        candidates.append(
                            (flipped, flipped_sparse_data, _mean_word_confidence(flipped_sparse_data))
                        )
                    except Exception:
                        pass  # alternate-PSM candidates are a bonus, never required
                    best_image, best_data, best_conf = max(candidates, key=lambda c: c[2])
                    if best_conf > conf + _RETRY_KEEP_MARGIN:
                        image, data = best_image, best_data

                words = _words_from_ocr_data(data, scale)
                if len(words) < _SPARSE_PAGE_WORD_COUNT:
                    try:
                        sparse_data = _pytesseract.image_to_data(
                            image, config=_SPARSE_PAGE_CONFIG, output_type=_pytesseract.Output.DICT
                        )
                        if _mean_word_confidence(sparse_data) > _mean_word_confidence(data):
                            words = _words_from_ocr_data(sparse_data, scale)
                    except Exception:
                        pass  # supplementary pass is a bonus, never required

                mark_image = (
                    image.convert("L")
                    .point(lambda p: 255 if p < BINARIZE_THRESHOLD else 0)
                    .convert("1")
                )
                pages.append(
                    {
                        "words": words,
                        "mark_image": mark_image,
                        "scale": scale,
                    }
                )
    except Exception as e:
        logger.warning("OCR rendering/processing failed: %s", e)
        return []
    return pages


def _mean_word_confidence(data) -> float:
    """Mean Tesseract confidence over the non-empty words of one page's
    image_to_data() output (0 when the page produced no words at all)."""
    confs = []
    for conf, text in zip(data.get("conf", []), data.get("text", [])):
        if not (text or "").strip():
            continue
        try:
            c = float(conf)
        except (TypeError, ValueError):
            continue
        if c >= 0:
            confs.append(c)
    return sum(confs) / len(confs) if confs else 0.0


def ocr_pdf_pages_to_words(raw_bytes):
    """Words-only view of ocr_pdf_pages() (one word-dict list per page),
    for callers that don't need the page images."""
    return [p["words"] for p in ocr_pdf_pages(raw_bytes)]


def _words_from_ocr_data(data, scale):
    """Converts one page's pytesseract image_to_data() output (pixel-space
    bounding boxes, at OCR_RESOLUTION dpi) into pdfplumber-shaped word dicts
    in PDF point units (scale = dpi / 72)."""
    words = []
    for i, text in enumerate(data.get("text", [])):
        text = text.strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError, KeyError, IndexError):
            conf = -1
        if conf < MIN_CONFIDENCE:
            continue
        left = data["left"][i] / scale
        top = data["top"][i] / scale
        width = data["width"][i] / scale
        words.append({"text": text, "x0": left, "x1": left + width, "top": top})
    return words
