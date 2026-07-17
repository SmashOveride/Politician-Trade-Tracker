@echo off
REM Double-click this file to start -- or reopen -- the Politician Trades
REM Tracker.
REM
REM Safe to run any time:
REM   - If the app is already running in the background, this just opens
REM     your browser to it (no second copy is ever started).
REM   - Otherwise it starts the app in the background and opens your
REM     browser to it. The app keeps running in the background --
REM     automatically checking for new disclosures once an hour -- even
REM     after you close the browser tab. Double-click this file again
REM     later, or just reopen the bookmark it gives you, for quick access
REM     any time.
REM
REM Prefers the prebuilt standalone .exe (dist\PoliticianTradesTracker\) if
REM present; otherwise falls back to running from source with Python
REM (requires "pip install -r requirements.txt" to have been run first, see
REM README.md).

cd /d "%~dp0"

if exist "dist\PoliticianTradesTracker\PoliticianTradesTracker.exe" (
    start "" "dist\PoliticianTradesTracker\PoliticianTradesTracker.exe"
    goto :done
)

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" run.py
    goto :done
)

where pythonw >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    start "" pythonw run.py
    goto :done
)

echo Could not find the app executable or a Python install.
echo See README.md for setup instructions.
pause
exit /b 1

:done
echo Starting Politician Trades Tracker... your browser will open shortly.
timeout /t 2 >nul
