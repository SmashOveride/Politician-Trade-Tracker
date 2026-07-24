# PyInstaller spec for a portable, no-install Linux build.
#
# Build (run from the project root, i.e. the folder containing desktop.py):
#     pyinstaller packaging/linux.spec
#
# Output: dist/PoliticianTradesTracker/PoliticianTradesTracker
# This is a single-FOLDER build. Users extract the folder anywhere (their
# home directory, a USB stick, etc.) and double-click/run the binary --
# no package manager, no root/sudo, no system install required.
#
# The app opens itself in the user's default web browser (see desktop.py /
# backend/launcher.py) rather than a native window, on a fixed local port
# so the address can be bookmarked. See start.sh in the project root for a
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
    # UPX-packed binaries are a common antivirus/EDR false-positive
    # trigger on every platform (plenty of real malware uses UPX to evade
    # signature scanning), not worth the smaller binary size here. See
    # README.md's "Antivirus / SmartScreen false positives" section.
    upx=False,
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
    upx=False,  # see the EXE() upx=False comment above
    upx_exclude=[],
    name="PoliticianTradesTracker",
)
