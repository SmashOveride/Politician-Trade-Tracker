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

block_cipher = None
# SPECPATH is injected by PyInstaller and points at the directory containing
# this .spec file (packaging/), regardless of the cwd the command was run
# from -- so resolve the project root from there rather than ".".
project_root = os.path.abspath(os.path.join(SPECPATH, os.pardir))

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
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PoliticianTradesTracker",
)

app = BUNDLE(
    coll,
    name="PoliticianTradesTracker.app",
    icon=None,
    bundle_identifier="com.politiciantradestracker.app",
    info_plist={
        # No native window is ever shown (the app opens a browser tab
        # instead), so keep it out of the Dock/Cmd+Tab -- same intent as
        # console=False + pythonw on Windows and a windowless build on Linux.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "CFBundleShortVersionString": "1.0.0",
    },
)
