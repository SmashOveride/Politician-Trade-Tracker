"""
One-time build step (run by whoever builds the Windows package, not by end
users) that fetches Tesseract OCR and stages the minimal set of files needed
to run it into packaging/vendor/tesseract-windows/. windows.spec bundles
that folder into the packaged app so Windows users get OCR support (see
backend/pipeline/ocr.py) out of the box, with nothing to install themselves.

Usage (from the project root):
    python packaging/fetch_tesseract_windows.py

Requires 7-Zip (https://www.7-zip.org/) to be installed -- the official
Tesseract Windows build is an NSIS installer, and 7-Zip can unpack an NSIS
installer's payload directly without ever running/elevating it (NSIS
installers built this way often demand admin elevation to run at all, even
when installing to a non-Program-Files folder, so actually running the
installer isn't an option here).

Only tesseract.exe, its actual runtime DLL dependencies (verified via the PE
import table, not guessed), and the English + orientation-detection trained
data are staged -- not the training/debugging tools (lstmtraining.exe,
text2image.exe, etc.) or other-language data the full installer offers,
which aren't needed to run OCR and would roughly double the size for
nothing this app uses.
"""

import os
import shutil
import subprocess
import sys
import urllib.request

# Pinned to a specific, verified-working release rather than "latest" so a
# rebuild months from now can't silently pick up a build this script's file
# list has never been checked against.
TESSERACT_VERSION = "5.3.0.20221214"
INSTALLER_URL = (
    f"https://digi.bib.uni-mannheim.de/tesseract/"
    f"tesseract-ocr-w64-setup-v{TESSERACT_VERSION}.exe"
)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
DOWNLOAD_DIR = os.path.join(PROJECT_ROOT, "packaging", "_tesseract_build_tmp")
INSTALLER_PATH = os.path.join(DOWNLOAD_DIR, "tesseract-setup.exe")
EXTRACT_DIR = os.path.join(DOWNLOAD_DIR, "extracted")
VENDOR_DIR = os.path.join(PROJECT_ROOT, "packaging", "vendor", "tesseract-windows")

# tesseract.exe's own runtime DLL dependencies, verified with pefile against
# the pinned TESSERACT_VERSION above (direct imports plus their own
# transitive imports) -- not the training/dev tool executables, which pull
# in a much larger, unrelated set (libcairo, libpango, ICU, etc.) that
# OCR-ing a PDF page never touches.
NEEDED_FILES = [
    "tesseract.exe",
    "libtesseract-5.dll",
    "libgcc_s_seh-1.dll",
    "libstdc++-6.dll",
    "liblept-5.dll",
    "libarchive-13.dll",
    "libcurl-4.dll",
    "iconv.dll",
    "libbz2-1.dll",
    "libgif-7.dll",
    "libidn2-0.dll",
    "libintl-8.dll",
    "libjbig-2.dll",
    "libjpeg-8.dll",
    "liblz4-1.dll",
    "liblzma-5.dll",
    "liblzo2-2.dll",
    "libnghttp2-14.dll",
    "libopenjp2.dll",
    "libpng16-16.dll",
    "libtiff-5.dll",
    "libunistring-2.dll",
    "libwebp-7.dll",
    "libwinpthread-1.dll",
    "libxml2-2.dll",
    "libzstd-1.dll",
    "zlib1.dll",
]

# Just English + the orientation/script-detection model PTR filings need --
# not the dozens of other languages the full installer offers.
NEEDED_TESSDATA = ["eng.traineddata", "osd.traineddata"]


def _find_7z():
    candidates = [
        shutil.which("7z"),
        shutil.which("7z.exe"),
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def main():
    if sys.platform != "win32":
        print("This script prepares a Windows-only vendor bundle; run it on Windows.")
        sys.exit(1)

    seven_zip = _find_7z()
    if not seven_zip:
        print(
            "7-Zip is required to unpack the Tesseract installer without running/"
            "elevating it. Install it from https://www.7-zip.org/ and re-run this script."
        )
        sys.exit(1)

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if not os.path.exists(INSTALLER_PATH):
        print(f"Downloading Tesseract {TESSERACT_VERSION} installer...")
        urllib.request.urlretrieve(INSTALLER_URL, INSTALLER_PATH)
    else:
        print("Using already-downloaded installer.")

    if os.path.exists(EXTRACT_DIR):
        shutil.rmtree(EXTRACT_DIR)
    os.makedirs(EXTRACT_DIR)

    print("Extracting installer payload with 7-Zip (not running the installer itself)...")
    result = subprocess.run(
        [seven_zip, "x", f"-o{EXTRACT_DIR}", INSTALLER_PATH, "-y"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("7-Zip extraction failed:")
        print(result.stdout)
        print(result.stderr)
        sys.exit(1)

    if os.path.exists(VENDOR_DIR):
        shutil.rmtree(VENDOR_DIR)
    os.makedirs(os.path.join(VENDOR_DIR, "tessdata"))

    missing = []
    for name in NEEDED_FILES:
        src = os.path.join(EXTRACT_DIR, name)
        if not os.path.exists(src):
            missing.append(name)
            continue
        shutil.copy2(src, os.path.join(VENDOR_DIR, name))

    for name in NEEDED_TESSDATA:
        src = os.path.join(EXTRACT_DIR, "tessdata", name)
        if not os.path.exists(src):
            missing.append(f"tessdata/{name}")
            continue
        shutil.copy2(src, os.path.join(VENDOR_DIR, "tessdata", name))

    if missing:
        print(
            "One or more expected files weren't found in the extracted installer "
            f"(the installer layout may have changed upstream): {missing}"
        )
        sys.exit(1)

    shutil.rmtree(DOWNLOAD_DIR)

    total_size = sum(
        os.path.getsize(os.path.join(root, f))
        for root, _dirs, files in os.walk(VENDOR_DIR)
        for f in files
    )
    print(f"Done. Staged Tesseract at {VENDOR_DIR} ({total_size / 1024 / 1024:.0f} MB).")
    print("windows.spec will bundle this the next time you run: pyinstaller packaging/windows.spec")


if __name__ == "__main__":
    main()
