"""
Re-OCR Ro Khanna scanned PTR pages (best-of-4 rotations) and fill garbled
asset names in khanna_garbled_review_with_pages_NEW.csv.

Strategy:
  1. For each PDF page referenced by the review CSV, render at 300 DPI and
     try rotations 0/90/180/270; keep the rotation with the most financial
     markers (CMN/INC/CORP/...).
  2. Cluster OCR words into visual lines and pull asset-name candidates.
  3. Match each review row to a candidate by:
       a) row_number_on_page (when counts align),
       b) fuzzy similarity to ocr_asset_name_as_stored,
       c) sequential alignment of garbled rows on that page.
  4. Preserve any human-filled YOUR_correct_company_name_or_ticker values.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.pipeline import ocr as ocr_mod  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402

PDF_DIR = ROOT / "data" / "khanna_pdfs"
CACHE_DIR = ROOT / "data" / "khanna_pages" / "reocr_cache"
IN_CSV = ROOT / "khanna_garbled_review_with_pages_NEW.csv"
OUT_CSV = ROOT / "khanna_garbled_review_with_pages_NEW_filled.csv"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MARKER_RE = re.compile(
    r"(CMN|CORP|INC\.?|PLC|LTD|CLASS|SP_|DC_|PUT|ADR|STOCK|BOND|TRUST|COMMON)",
    re.I,
)

# Patterns that look like real asset lines
ASSET_HINT_RE = re.compile(
    r"(CMN|INC\.?|CORP|PLC|LTD|ADR|STOCK|TRUST|CLASS\s*[A-Z0-9]|BOND|"
    r"HOLDINGS|TECHNOLOG|PHARMA|FINANCIAL|PROPERTIES|BANC|BANK|"
    r"REIT|FUNDING|SERIES|SPONSORED)",
    re.I,
)


def _score_rotation(image, rot, pt):
    im = image.rotate(rot, expand=True) if rot else image
    data = pt.image_to_data(
        im, config=ocr_mod.TESSERACT_CONFIG, output_type=pt.Output.DICT
    )
    conf = ocr_mod._mean_word_confidence(data)
    words_txt = [
        data["text"][i]
        for i in range(len(data["text"]))
        if (data["text"][i] or "").strip() and int(data["conf"][i]) >= 0
    ]
    markers = len(MARKER_RE.findall(" ".join(words_txt)))
    score = markers * 10 + conf + len(words_txt) * 0.05
    scale = ocr_mod.OCR_RESOLUTION / 72.0
    words = ocr_mod._words_from_ocr_data(data, scale)
    return score, rot, words, markers, conf, len(words_txt), im


def best_orientation_words(image):
    """Return (words, rot, markers, conf, image) for best of 4 rotations.

    Khanna PTR scans are almost always landscape (rotate 90). Try 90 first
    and skip the other three when markers already look strong -- ~4x faster
    on the common case, full search only when 90 is weak.
    """
    pt = ocr_mod._pytesseract
    # Prefer 90 (observed winner on virtually every Khanna page), then 270,
    # then upright/flip as fallbacks.
    order = (90, 270, 0, 180)
    best = _score_rotation(image, order[0], pt)
    # Strong financial-table signal: no need to burn 3 more full OCR passes.
    if best[3] >= 12 and best[4] >= 28:
        return best[2], best[1], best[3], best[4], best[6]
    for rot in order[1:]:
        cand = _score_rotation(image, rot, pt)
        if cand[0] > best[0]:
            best = cand
    return best[2], best[1], best[3], best[4], best[6]


def clean_asset(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = re.sub(
        r"^(SP_|DC_|OC_|_SP_|_DC_|\$P_|3P_|5P_|PL_|P_\||SP\s*\||DC\s*\||"
        r"OC\s*\||_?SP\s*\|?|_?DC\s*\|?)\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"^[|:_.\-\s\$]+", "", text)
    text = re.sub(r"[|]+", " ", text)
    # drop trailing checkbox/date debris
    text = re.sub(
        r"\s+\d{1,2}/\d{1,2}/\d{2,4}.*$",
        "",
        text,
    )
    text = re.sub(r"\s+x\s*$", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -_|.,;")
    return text


def looks_usable(name: str) -> bool:
    if not name or len(name) < 4:
        return False
    if re.fullmatch(r"[\d/$.,\-\s]+", name):
        return False
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}\.?", name):
        return False
    letters = re.sub(r"[^A-Za-z]", "", name)
    if len(letters) < 3:
        return False
    # reject pure grid noise
    if re.fullmatch(r"[Il1\|_\-\s\.]+", name):
        return False
    vowels = len(re.findall(r"[AEIOUaeiou]", letters))
    if len(letters) >= 10 and vowels / len(letters) < 0.12:
        return False
    if re.search(r"[bcdfghjklmnpqrstvwxyz]{10,}", name, re.I):
        return False
    return True


def extract_asset_lines(words) -> list[str]:
    """Cluster words into visual lines; keep those that look like assets."""
    if not words:
        return []
    # cluster by top
    buckets: dict[int, list] = defaultdict(list)
    for w in words:
        key = int(round(float(w["top"]) / 4.0) * 4)
        buckets[key].append(w)

    # merge nearby buckets
    keys = sorted(buckets.keys())
    merged: list[list] = []
    cur_key = None
    cur_words: list = []
    for k in keys:
        if cur_key is None or k - cur_key <= 6:
            cur_words.extend(buckets[k])
            cur_key = k if cur_key is None else cur_key
        else:
            merged.append(cur_words)
            cur_words = list(buckets[k])
            cur_key = k
    if cur_words:
        merged.append(cur_words)

    assets = []
    for group in merged:
        group = sorted(group, key=lambda w: w["x0"])
        # Prefer left/mid portion (asset column); drop far-right amount col words
        max_x = max(w["x1"] for w in group) if group else 0
        # keep words in left 65% of line extent, but if line is short keep all
        xs = [w["x0"] for w in group]
        if xs:
            xmin, xmax = min(xs), max(w["x1"] for w in group)
            cutoff = xmin + (xmax - xmin) * 0.72
            leftish = [w for w in group if w["x0"] <= cutoff]
            if len(leftish) >= 2:
                group = leftish
        text = clean_asset(" ".join(w["text"] for w in group))
        if not text:
            continue
        if not ASSET_HINT_RE.search(text) and not looks_usable(text):
            continue
        # skip header junk
        if re.search(
            r"(Provide full|Asset Name|Transaction|Amount|\$1,001|\$15,000|"
            r"Page \d|HOUSE OF REPRESENTATIVES|Periodic Transaction)",
            text,
            re.I,
        ):
            continue
        if looks_usable(text) or ASSET_HINT_RE.search(text):
            # further clean glued tokens a bit
            text = re.sub(r"(?<=[A-Za-z])(?=CMN\b)", " ", text)
            text = re.sub(r"(?<=[A-Za-z])(?=INC\b)", " ", text)
            text = re.sub(r"(?<=[A-Za-z])(?=CORP\b)", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in assets:
                assets.append(text)
    return assets


def similarity(a: str, b: str) -> float:
    a = re.sub(r"[^A-Za-z0-9]", "", (a or "")).lower()
    b = re.sub(r"[^A-Za-z0-9]", "", (b or "")).lower()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def normalize_spaces_caps(name: str) -> str:
    """Light cleanup for presentation."""
    name = re.sub(r"\s+", " ", name).strip()
    # expand common glued forms if already spaced poorly
    name = re.sub(r"\bCMN\b", "CMN", name)
    return name


PROGRESS_PATH = CACHE_DIR / "progress.jsonl"


def _heartbeat(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(PROGRESS_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def ocr_page(pdf_path: Path, page_no: int) -> dict:
    """1-indexed page_no. Always safe to retry; writes cache atomically."""
    cache = CACHE_DIR / f"{pdf_path.name}_p{page_no}.json"
    if cache.exists() and cache.stat().st_size > 10:
        with open(cache, encoding="utf-8") as f:
            return json.load(f)

    t0 = time.time()
    pdf = pdfium.PdfDocument(str(pdf_path))
    if page_no < 1 or page_no > len(pdf):
        result = {
            "page": page_no,
            "rot": 0,
            "markers": 0,
            "conf": 0.0,
            "nwords": 0,
            "assets": [],
            "error": f"page {page_no} out of range ({len(pdf)} pages)",
            "seconds": 0,
        }
    else:
        page = pdf[page_no - 1]
        bitmap = page.render(scale=ocr_mod.OCR_RESOLUTION / 72)
        image = bitmap.to_pil()
        words, rot, markers, conf, _im = best_orientation_words(image)
        assets = extract_asset_lines(words)
        dt = time.time() - t0
        result = {
            "page": page_no,
            "rot": rot,
            "markers": markers,
            "conf": conf,
            "nwords": len(words),
            "assets": assets,
            "seconds": round(dt, 1),
        }
    # atomic write so a kill mid-write doesn't poison cache
    tmp = cache.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    tmp.replace(cache)
    dt = time.time() - t0
    _heartbeat(
        f"{pdf_path.name} p{page_no:02d} rot={result.get('rot', 0):3d} "
        f"mk={result.get('markers', 0):3d} conf={result.get('conf', 0):5.1f} "
        f"w={result.get('nwords', 0):3d} assets={len(result.get('assets') or []):2d} "
        f"({dt:.1f}s)"
    )
    return result


def pdf_key_from_link(link: str) -> str:
    link = link.replace("pdfMcCormi", "pdf")
    fname = link.rstrip("/").split("/")[-1]
    year = link.split("/")[-2]
    return f"{year}_{fname}"


def match_row(ocr_name: str, rownum: int, page_assets: list[str], used: set[int]) -> tuple[str | None, str]:
    """Return (matched_name, method)."""
    if not page_assets:
        return None, "none"

    # 1) row number if in range and unused
    if 1 <= rownum <= len(page_assets) and (rownum - 1) not in used:
        cand = page_assets[rownum - 1]
        if looks_usable(cand):
            # only trust if not wildly different when ocr_name has readable parts
            readable = re.findall(r"[A-Za-z]{4,}", ocr_name or "")
            if readable:
                if any(similarity(tok, cand) >= 0.45 or tok.lower() in cand.lower() for tok in readable):
                    return cand, "row+token"
            else:
                return cand, "row"

    # 2) best fuzzy against full ocr_name
    best_i, best_s = -1, 0.0
    for i, a in enumerate(page_assets):
        if i in used:
            continue
        s = similarity(ocr_name, a)
        # also token containment bonus
        for tok in re.findall(r"[A-Za-z]{5,}", ocr_name or ""):
            if tok.lower() in a.lower():
                s = max(s, 0.55)
        if s > best_s:
            best_s, best_i = s, i
    if best_i >= 0 and best_s >= 0.35 and looks_usable(page_assets[best_i]):
        return page_assets[best_i], f"fuzzy:{best_s:.2f}"

    # 3) readable token search (e.g. PERKINELMER inside garbage)
    tokens = re.findall(r"[A-Za-z]{5,}", ocr_name or "")
    for tok in sorted(tokens, key=len, reverse=True):
        for i, a in enumerate(page_assets):
            if i in used:
                continue
            if tok.lower() in a.lower() or similarity(tok, a) >= 0.6:
                if looks_usable(a):
                    return a, f"token:{tok}"

    return None, "none"


def main():
    if not ocr_mod.is_available():
        print("OCR not available")
        sys.exit(1)

    with open(IN_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = [c for c in (reader.fieldnames or []) if c]
        rows = list(reader)

    # group review rows by pdf+page
    by_pdf_page: dict[tuple[str, int], list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        key = pdf_key_from_link(r["filing_pdf_link"])
        page = int(r["pdf_page_number"] or 0)
        by_pdf_page[(key, page)].append(i)

    # OCR all needed pages
    page_data: dict[tuple[str, int], dict] = {}
    pdfs = sorted({k for k, _ in by_pdf_page.keys()})
    for pdf_key in pdfs:
        path = PDF_DIR / pdf_key
        if not path.exists():
            print(f"MISSING {path}")
            continue
        pages_needed = sorted({p for (k, p) in by_pdf_page if k == pdf_key})
        _heartbeat(f"=== {pdf_key} pages {pages_needed} ===")
        for p in pages_needed:
            try:
                page_data[(pdf_key, p)] = ocr_page(path, p)
            except Exception as e:
                _heartbeat(f"  p{p} ERROR: {e!r}")
                page_data[(pdf_key, p)] = {"assets": [], "error": str(e)}
                # still write a cache stub so we don't infinite-retry a hard crash
                stub = CACHE_DIR / f"{path.name}_p{p}.json"
                if not stub.exists():
                    with open(stub, "w", encoding="utf-8") as f:
                        json.dump(
                            {
                                "page": p,
                                "assets": [],
                                "error": str(e),
                                "rot": 0,
                                "markers": 0,
                                "conf": 0,
                                "nwords": 0,
                            },
                            f,
                        )

    # Match and fill
    stats = defaultdict(int)
    for (pdf_key, page), idxs in sorted(by_pdf_page.items()):
        assets = page_data.get((pdf_key, page), {}).get("assets") or []
        used: set[int] = set()
        # process in row_number order for sequential assignment
        ordered = sorted(
            idxs,
            key=lambda i: int(rows[i].get("row_number_on_page") or 0),
        )
        # sequential pointer for leftover matching
        seq_i = 0
        for i in ordered:
            r = rows[i]
            existing = (r.get("YOUR_correct_company_name_or_ticker") or "").strip()
            if existing and not existing.startswith("[WEAK]"):
                stats["kept_human"] += 1
                continue

            ocr_name = r.get("ocr_asset_name_as_stored") or ""
            rownum = int(r.get("row_number_on_page") or 0)
            name, method = match_row(ocr_name, rownum, assets, used)

            if name is None and assets:
                # sequential fill among unused usable assets
                while seq_i < len(assets):
                    if seq_i not in used and looks_usable(assets[seq_i]):
                        name = assets[seq_i]
                        method = "sequential"
                        break
                    seq_i += 1
                seq_i += 1

            if name:
                # mark used
                try:
                    used.add(assets.index(name))
                except ValueError:
                    pass
                if looks_usable(name):
                    rows[i]["YOUR_correct_company_name_or_ticker"] = normalize_spaces_caps(
                        name
                    )
                    stats["filled"] += 1
                    stats[f"method_{method.split(':')[0]}"] += 1
                else:
                    rows[i]["YOUR_correct_company_name_or_ticker"] = f"[WEAK] {name}"
                    stats["weak"] += 1
            else:
                stats["unmatched"] += 1

    out_fields = [
        "trade_id",
        "filing_pdf_link",
        "pdf_page_number",
        "row_number_on_page",
        "transaction_date",
        "transaction_type",
        "amount_range",
        "ocr_asset_name_as_stored",
        "YOUR_correct_company_name_or_ticker",
    ]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    good = sum(
        1
        for r in rows
        if (r.get("YOUR_correct_company_name_or_ticker") or "").strip()
        and not (r.get("YOUR_correct_company_name_or_ticker") or "").startswith("[WEAK]")
    )
    weak = sum(
        1
        for r in rows
        if (r.get("YOUR_correct_company_name_or_ticker") or "").startswith("[WEAK]")
    )
    empty = sum(
        1
        for r in rows
        if not (r.get("YOUR_correct_company_name_or_ticker") or "").strip()
    )
    print("\n==== SUMMARY ====")
    print(dict(stats))
    print(f"Wrote {OUT_CSV}")
    print(f"good={good} weak={weak} empty={empty} total={len(rows)}")


if __name__ == "__main__":
    main()
