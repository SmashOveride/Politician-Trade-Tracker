# PyInstaller spec for "Politician Trade Tracker Lite" -- same app, same
# desktop.py entry point, but never bundles the live parsing/OCR pipeline's
# dependencies (pdfplumber, lxml, pytesseract, PIL, pypdfium2, Tesseract
# itself). Instead of fetching and parsing raw House Clerk/Senate filings
# itself, Lite downloads a pre-built, pre-OCR'd database snapshot published
# by scripts/publish_snapshot.py (see .github/workflows/publish-data.yml
# and backend/snapshot_download.py). Roughly 25-35MB versus the full
# build's ~238MB, and near-instant refreshes instead of waiting on live OCR.
#
# Build (run from the project root, i.e. the folder containing desktop.py):
#     pyinstaller packaging/windows_lite.spec
#
# Output: dist\PoliticianTradesTrackerLite\PoliticianTradesTrackerLite.exe
# Single-folder build, same as packaging/windows.spec -- see that file for
# the general Windows packaging notes (UPX/AV, version-info resource, etc),
# all of which apply equally here.
#
# Which code path actually runs isn't decided by anything in this spec --
# it falls out naturally from backend/data_fetch.py's pipeline_available(),
# which just tries importing the pipeline and catches ImportError. This
# spec's job is only to make sure that import genuinely fails in this
# build, via `excludes` below, so the app takes the snapshot-download path
# (backend/snapshot_download.py) instead of the live pipeline.

import os
import sys

block_cipher = None
project_root = os.path.abspath(os.path.join(SPECPATH, os.pardir))

sys.path.insert(0, project_root)
from backend.version import APP_VERSION  # noqa: E402


def _filevers_tuple(version_str):
    parts = [int(p) for p in version_str.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


# See packaging/windows.spec's identical block for why this exists at all
# (a real version-info resource is one more low-cost anti-false-positive
# signal, alongside upx=False below).
_version_info_path = os.path.join(project_root, "packaging", "_win_version_info_lite.txt")
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
        StringStruct(u'FileDescription', u'Politician Trade Tracker Lite'),
        StringStruct(u'FileVersion', u'{APP_VERSION}'),
        StringStruct(u'InternalName', u'PoliticianTradesTrackerLite'),
        StringStruct(u'LegalCopyright', u'Open source project'),
        StringStruct(u'OriginalFilename', u'PoliticianTradesTrackerLite.exe'),
        StringStruct(u'ProductName', u'Politician Trade Tracker Lite'),
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

# See windows.spec's identical line -- built by packaging/icon/build_icons.py.
app_icon_path = os.path.join(project_root, "packaging", "icon", "final", "app_icon.ico")

a = Analysis(
    [os.path.join(project_root, "desktop.py")],
    pathex=[project_root],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # This is what actually makes it "Lite" -- without these, PyInstaller's
    # static analysis would still trace the *lazy* import inside
    # data_fetch.refresh_data() (see the NOTE above that import) all the
    # way down to pdfplumber/lxml/PIL/pypdfium2 and bundle them anyway, even
    # though nothing calls that code path at runtime in this build. Listing
    # both the third-party packages and the specific pipeline submodules
    # that import them, so the exclusion holds even if some other module
    # someday imports one of these packages a different way.
    excludes=[
        "pdfplumber", "pdfminer", "lxml", "bs4", "beautifulsoup4",
        "pytesseract", "PIL", "pypdfium2", "pypdfium2_raw",
        "backend.pipeline.orchestrator",
        "backend.pipeline.house_clerk",
        "backend.pipeline.senate_efd",
        "backend.pipeline.secondary_sources",
        "backend.pipeline.custom_api_source",
        "backend.pipeline.checkbox_form",
        "backend.pipeline.ocr",
        "backend.ticker_resolve",
    ],
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
    name="PoliticianTradesTrackerLite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # see packaging/windows.spec's upx=False comment
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
    upx=False,
    upx_exclude=[],
    name="PoliticianTradesTrackerLite",
)
