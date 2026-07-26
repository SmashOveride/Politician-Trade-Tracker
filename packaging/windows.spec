# PyInstaller spec for a portable, no-install Windows build.
#
# Build (run from the project root, i.e. the folder containing desktop.py):
#     pyinstaller packaging/windows.spec
#
# Output: dist\PoliticianTradesTracker\PoliticianTradesTracker.exe
# This is a single-FOLDER build (not --onefile) so startup is fast and the
# bundled `data/` folder (created on first run) sits right next to the exe.
# Users just unzip and double-click the .exe -- no installer, no admin rights.
#
# The app opens itself in the user's default web browser (see desktop.py /
# backend/launcher.py) rather than a native window, on a fixed local port
# so the address can be bookmarked. See start.bat in the project root for a
# friendlier double-click launcher that also works before this exe is built.
#
# Bundles Tesseract OCR (see backend/pipeline/ocr.py) so Windows users get
# OCR support for scanned PTR filings with nothing to install themselves --
# run packaging/fetch_tesseract_windows.py once beforehand to stage it at
# packaging/vendor/tesseract-windows/ (gitignored; not committed). If that
# folder hasn't been staged yet, the build still succeeds, just without
# bundled OCR, same as running from source without Tesseract installed.

import os
import sys

block_cipher = None
# SPECPATH is injected by PyInstaller and points at the directory containing
# this .spec file (packaging/), regardless of the cwd the command was run
# from -- so resolve the project root from there rather than ".".
project_root = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# Read the app's version from the single source of truth (backend/version.py,
# also used by macos.spec and the in-app footer/update checker) so the
# Windows version-info resource generated below can never drift out of sync.
sys.path.insert(0, project_root)
from backend.version import APP_VERSION  # noqa: E402


def _filevers_tuple(version_str):
    parts = [int(p) for p in version_str.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


# A Windows version-info resource (publisher/product/file description,
# version numbers) embedded into the exe. This is generated fresh on every
# build rather than hand-maintained, so it can't drift from APP_VERSION.
# An unsigned .exe with none of this metadata reads as more anonymous to a
# reviewer (and, marginally, to some antivirus heuristics) than one that at
# least identifies its publisher, product name, and version the way
# virtually all legitimate Windows software does -- it's not a substitute
# for actually code-signing (see README.md's "Antivirus / SmartScreen false
# positives" section for that), just one more low-cost signal alongside it.
_version_info_path = os.path.join(project_root, "packaging", "_win_version_info.txt")
_filevers = _filevers_tuple(APP_VERSION)
with open(_version_info_path, "w", encoding="utf-8") as _f:
    _f.write(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_filevers!r},
    prodvers={_filevers!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'SmashOveride (open source)'),
        StringStruct(u'FileDescription', u'Politician Trades Tracker'),
        StringStruct(u'FileVersion', u'{APP_VERSION}'),
        StringStruct(u'InternalName', u'PoliticianTradesTracker'),
        StringStruct(u'LegalCopyright', u'Open source project'),
        StringStruct(u'OriginalFilename', u'PoliticianTradesTracker.exe'),
        StringStruct(u'ProductName', u'Politician Trades Tracker'),
        StringStruct(u'ProductVersion', u'{APP_VERSION}')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""
    )

datas = [
    (os.path.join(project_root, "frontend"), "frontend"),
]

# Built by packaging/icon/build_icons.py from packaging/icon/final/app_icon_256.png
# (the "capitol + uptrend arrow" design) -- checked into the repo since
# rebuilding it needs a couple of extra pip packages nobody else building
# the app should need to install.
app_icon_path = os.path.join(project_root, "packaging", "icon", "final", "app_icon.ico")

tesseract_vendor_dir = os.path.join(project_root, "packaging", "vendor", "tesseract-windows")
if os.path.isdir(tesseract_vendor_dir):
    datas.append((tesseract_vendor_dir, "tesseract"))
else:
    print(
        "NOTE: packaging/vendor/tesseract-windows not found -- building without "
        "bundled OCR support. Run packaging/fetch_tesseract_windows.py first to "
        "include it."
    )

a = Analysis(
    [os.path.join(project_root, "desktop.py")],
    pathex=[project_root],
    binaries=[],
    datas=datas,
    # pytesseract is only ever imported inside a function (see
    # backend/pipeline/ocr.py's is_available()), which PyInstaller's static
    # import scan doesn't reliably pick up -- without this, the OCR fallback
    # silently has no pytesseract to call even with tesseract.exe bundled.
    hiddenimports=["pytesseract"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PoliticianTradesTracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-packed executables are one of the most common antivirus false-
    # positive triggers -- plenty of real malware uses UPX to evade
    # signature scanning, so heuristic engines weigh "packed with UPX" as
    # suspicious regardless of what's actually inside. This app gains
    # nothing from the smaller binary size that's worth that risk. See
    # README.md's "Antivirus / SmartScreen false positives" section.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=app_icon_path,
    version=_version_info_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,  # see the EXE() upx=False comment above
    upx_exclude=[],
    name="PoliticianTradesTracker",
)

# PyInstaller's binary/data reclassification step duplicates every DLL
# inside the bundled tesseract-windows vendor folder (added above via
# `datas`) to BOTH its original _internal/tesseract/ location AND the top
# level of _internal -- ~150MB of pure duplication, confirmed on a real
# build (dropped it from 389MB to 238MB). tesseract.exe only ever needs its
# own dependencies to be in ITS OWN directory (Windows checks a launched
# exe's own folder first in the DLL search order, regardless of PATH), and
# nothing on the Python side ever loads these tesseract-specific libraries
# directly, so the top-level copies are dead weight. Confirmed safe to
# remove: the packaged app still runs correctly and Tesseract OCR still
# works end-to-end (both `tesseract --version` and a real
# pytesseract.image_to_string() call) with only the _internal/tesseract/
# copies present. Matches on filename + exact byte size (not just name) so
# this can never delete a same-named-but-different DLL some other
# dependency actually needs at the top level.
_dist_dir = os.path.join(project_root, "dist", "PoliticianTradesTracker")
_internal_dir = os.path.join(_dist_dir, "_internal")
_tesseract_dir = os.path.join(_internal_dir, "tesseract")
if os.path.isdir(_tesseract_dir):
    _removed_bytes = 0
    for _name in os.listdir(_tesseract_dir):
        if not _name.lower().endswith(".dll"):
            continue
        _top_level_path = os.path.join(_internal_dir, _name)
        _tesseract_path = os.path.join(_tesseract_dir, _name)
        if os.path.isfile(_top_level_path) and os.path.getsize(_top_level_path) == os.path.getsize(_tesseract_path):
            _removed_bytes += os.path.getsize(_top_level_path)
            os.remove(_top_level_path)
    if _removed_bytes:
        print(f"NOTE: removed {_removed_bytes / 1_000_000:.0f} MB of duplicate Tesseract DLLs "
              f"from the top level of _internal (kept in _internal/tesseract/)")
