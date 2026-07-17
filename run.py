"""
Development entry point. Run with:

    python run.py

Starts the app (unless it's already running in the background, in which
case this just opens a new browser tab to it) and opens the Recent Trades
home screen in your default web browser. Safe to run repeatedly -- e.g. via
the double-click launcher (start.sh / start.bat) -- without ever starting a
second instance. See backend/launcher.py for the full behavior.
"""

from backend.launcher import launch

if __name__ == "__main__":
    launch()
