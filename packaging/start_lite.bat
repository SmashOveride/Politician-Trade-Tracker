@echo off
REM Double-click this file to start -- or reopen -- Politician Trade
REM Tracker Lite.
REM
REM Safe to run any time:
REM   - If the app is already running in the background, this just opens
REM     your browser to it (no second copy is ever started).
REM   - Otherwise it starts the app in the background and opens your
REM     browser to it. The app keeps running in the background --
REM     automatically checking for updated data once an hour -- even
REM     after you close the browser tab. Double-click this file again
REM     later, or just reopen the bookmark it gives you, for quick access
REM     any time.
REM
REM This folder is a self-contained, prebuilt copy of the app -- no Python
REM install required. Everything it needs lives right here next to this
REM file (PoliticianTradesTrackerLite.exe and the _internal folder); don't
REM separate them.

cd /d "%~dp0"
start "" "PoliticianTradesTrackerLite.exe"
echo Starting Politician Trade Tracker Lite... your browser will open shortly.
timeout /t 2 >nul
