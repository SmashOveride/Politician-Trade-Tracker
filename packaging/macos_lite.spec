# PyInstaller spec for "Politician Trade Tracker Lite" on macOS -- same app,
# same desktop.py entry point, but never bundles the live parsing/OCR
# pipeline's dependencies (pdfplumber, lxml, pytesseract, PIL, pypdfium2).
# See packaging/windows_lite.spec for the full explanation of how/why this
# works (backend/data_fetch.py's pipeline_available(), backend/
# snapshot_download.py) -- this is the same idea, just for a macOS .app
# bundle instead of a Windows .exe.
#
# Build (run from the project root, on a Mac -- PyInstaller does not
# cross-compile; see .github/workflows/build-macos.yml for a way to get a
# real Mac build without owning a Mac at all):
#     pyinstaller packaging/macos_lite.spec
#
# Output: dist/PoliticianTradesTrackerLite.app

import os
import sys

block_cipher = None
project_root = os.path.abspath(os.path.join(SPECPATH, os.pardir))

sys.path.insert(0, project_root)
from backend.version import APP_VERSION  # noqa: E402

# See packaging/macos.spec's identical line -- built by
# packaging/icon/build_icons.py.
app_icon_path = os.path.join(project_root, "packaging", "icon", "final", "app_icon.icns")

a = Analysis(
    [os.path.join(project_root, "desktop.py")],
    pathex=[project_root],
    binaries=[],
    datas=[
        (os.path.join(project_root, "frontend"), "frontend"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # This is what actually makes it "Lite" -- see windows_lite.spec's
    # identical list for the full explanation of why both the third-party
    # packages and the specific pipeline submodules that import them need
    # to be listed (PyInstaller's static analysis would otherwise still
    # trace the lazy import inside data_fetch.refresh_data() down to these
    # packages and bundle them anyway, even though nothing calls that code
    # path at runtime in this build).
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
    upx=False,  # see packaging/macos.spec's upx=False comment
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=app_icon_path,
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

app = BUNDLE(
    coll,
    name="PoliticianTradesTrackerLite.app",
    icon=app_icon_path,
    bundle_identifier="com.politiciantradestrackerlite.app",
    info_plist={
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "CFBundleShortVersionString": APP_VERSION,
    },
)
