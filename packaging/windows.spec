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
