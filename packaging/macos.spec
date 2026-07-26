# PyInstaller spec for a portable, no-install macOS build.
#
# Build (run from the project root, i.e. the folder containing desktop.py,
# on a Mac -- PyInstaller does not cross-compile):
#     pyinstaller packaging/macos.spec
#
# Output: dist/PoliticianTradesTracker.app
# This produces a standard macOS .app bundle. Users can copy the whole
# project folder (or just PoliticianTradesTracker.app) anywhere and
# double-click to launch -- no installer, no admin rights required. Since
# the app isn't code-signed/notarized, the first launch will need a
# right-click > Open (or System Settings > Privacy & Security > Open Anyway)
# to get past Gatekeeper -- see README.md.
#
# The app opens itself in the user's default web browser (see desktop.py /
# backend/launcher.py) rather than a native window, on a fixed local port
# so the address can be bookmarked. See start.command in the project root
# for a friendlier double-click launcher that also works before this .app
# is built. LSUIElement is set below so the app runs quietly in the
# background (no Dock icon) since there is no native window to show.

import os
import sys

block_cipher = None
# SPECPATH is injected by PyInstaller and points at the directory containing
# this .spec file (packaging/), regardless of the cwd the command was run
# from -- so resolve the project root from there rather than ".".
project_root = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# Read the app's version from the single source of truth (backend/version.py)
# rather than duplicating it here, so CFBundleShortVersionString below can
# never drift out of sync with what the running app reports in its footer
# and update-check API.
sys.path.insert(0, project_root)
from backend.version import APP_VERSION  # noqa: E402

# Built by packaging/icon/build_icons.py from packaging/icon/final/app_icon_256.png
# (the "capitol + uptrend arrow" design) -- checked into the repo since
# rebuilding it needs a couple of extra pip packages nobody else building
# the app should need to install. This is what actually shows up as the
# .app bundle's Finder/Dock icon (set via BUNDLE()'s icon= below); EXE()'s
# icon= just applies it to the raw Mach-O binary inside as well.
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
    excludes=[],
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
    # UPX-packed binaries are a common antivirus/Gatekeeper-adjacent
    # scanner false-positive trigger (plenty of real malware uses UPX to
    # evade signature scanning), and macOS's own arm64 binaries in
    # particular are prone to UPX corrupting them. Not worth the smaller
    # binary size. See README.md's "Antivirus / SmartScreen false
    # positives" section.
    upx=False,
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
    upx=False,  # see the EXE() upx=False comment above
    upx_exclude=[],
    name="PoliticianTradesTracker",
)

app = BUNDLE(
    coll,
    name="PoliticianTradesTracker.app",
    icon=app_icon_path,
    bundle_identifier="com.politiciantradestracker.app",
    info_plist={
        # No native window is ever shown (the app opens a browser tab
        # instead), so keep it out of the Dock/Cmd+Tab -- same intent as
        # console=False + pythonw on Windows and a windowless build on Linux.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "CFBundleShortVersionString": APP_VERSION,
    },
)
