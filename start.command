#!/bin/sh
# Double-click this file (or run "./start.command") to start -- or reopen --
# the Politician Trades Tracker on macOS.
#
# Safe to run any time:
#   - If the app is already running in the background, this just opens your
#     browser to it (no second copy is ever started).
#   - Otherwise it starts the app in the background and opens your browser
#     to it. The app keeps running in the background -- automatically
#     checking for new disclosures once an hour -- even after you close the
#     browser tab. Run this script again later, or just reopen the bookmark
#     it gives you, for quick access any time.
#
# Prefers the prebuilt standalone app bundle
# (dist/PoliticianTradesTracker.app/) if present; otherwise falls back to
# running from source with Python (requires `pip install -r
# requirements.txt` to have been run first, see README.md).
#
# Note: macOS Finder runs double-clicked .command files in a new Terminal
# window automatically -- no chmod/setup needed first (unlike start.sh on
# Linux).

cd "$(dirname "$0")" || exit 1

LOG="start.log"
EXE="dist/PoliticianTradesTracker.app/Contents/MacOS/PoliticianTradesTracker"

if [ -x "$EXE" ]; then
    nohup "$EXE" >"$LOG" 2>&1 &
    disown 2>/dev/null || true
elif [ -x ".venv/bin/python3" ]; then
    nohup ".venv/bin/python3" run.py >"$LOG" 2>&1 &
    disown 2>/dev/null || true
elif command -v python3 >/dev/null 2>&1; then
    nohup python3 run.py >"$LOG" 2>&1 &
    disown 2>/dev/null || true
else
    echo "Could not find the app executable or a Python 3 install."
    echo "See README.md for setup instructions."
    exit 1
fi

echo "Starting Politician Trades Tracker... your browser will open shortly."
echo "(If nothing happens, check $LOG in this folder for details.)"
