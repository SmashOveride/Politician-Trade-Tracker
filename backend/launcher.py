"""
Shared launch logic used by both run.py (development) and desktop.py (the
packaged executable). Implements:

- A fixed, predictable port (so the app's URL is stable across restarts and
  safe to bookmark in a browser), falling back to any free port only if the
  fixed one is genuinely unavailable (e.g. taken by an unrelated program).
- Single-instance detection: before starting anything, checks whether an
  instance of this app is already running (via a lightweight /api/ping
  signature check) and, if so, just opens the browser to it instead of
  starting a second server.
- Opens the app's home screen (Recent Trades) in the user's default web
  browser -- a real browser tab, not a native app window, so the address
  can be bookmarked like any other website.
- Keeps running in the background after the browser tab is closed (the
  background auto-refresh scheduler, see app.py, keeps checking for new
  disclosures on its own). Double-clicking the launcher again later just
  reopens the browser to the same running instance.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

import requests

from . import db
from .app import create_app

# Arbitrary, distinctive high port, chosen to avoid common collisions with
# other local dev tools (5000, 8080, 8888, 3000, etc). If this port happens
# to be taken by something else, we transparently fall back to any free
# port for that session -- but the address won't be stable across restarts
# in that unlikely case, so this should be rare in practice.
DEFAULT_PORT = 58732

PING_TIMEOUT_SECONDS = 1.5


def _server_info_path():
    return os.path.join(db.get_data_dir(), "server_info.json")


def _read_server_info():
    path = _server_info_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_server_info(port):
    try:
        with open(_server_info_path(), "w", encoding="utf-8") as f:
            json.dump({"port": port, "pid": os.getpid()}, f)
    except Exception:
        pass  # non-fatal -- worst case, the next launch can't find us via
        # the lock file and falls back to checking DEFAULT_PORT directly.


def _is_our_app(port):
    try:
        resp = requests.get(f"http://127.0.0.1:{port}/api/ping", timeout=PING_TIMEOUT_SECONDS)
        return resp.ok and resp.json().get("app") == "politician-trades-tracker"
    except Exception:
        return False


def _find_running_instance():
    """Returns the port of an already-running instance of this app, or None
    if it doesn't appear to be running anywhere."""
    candidates = []
    info = _read_server_info()
    if info and info.get("port"):
        candidates.append(info["port"])
    if DEFAULT_PORT not in candidates:
        candidates.append(DEFAULT_PORT)

    for port in candidates:
        if _is_our_app(port):
            return port
    return None


def _bind_port():
    """Tries the fixed DEFAULT_PORT first (for a stable, bookmarkable URL);
    falls back to any free port if that's unavailable for some other
    reason (e.g. taken by an unrelated program)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", DEFAULT_PORT))
        port = DEFAULT_PORT
    except OSError:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    s.close()
    return port


def _relaunch_command():
    """Returns the argv needed to start a fresh instance of this app the
    same way it was originally started -- either the packaged executable
    (PyInstaller sets sys.frozen and sys.executable *is* the exe, taking no
    extra args) or the development entry point (`python run.py`)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable] + list(sys.argv)


def request_shutdown(delay_seconds=0.4):
    """Stops the running server process. Runs on a short delay in a
    background thread so the HTTP response confirming the request can
    actually reach the browser first. There's only ever a single daemon
    thread running the Flask server (see launch() below), so a hard process
    exit is a safe, simple way to stop everything at once."""

    def _do_shutdown():
        time.sleep(delay_seconds)
        os._exit(0)

    threading.Thread(target=_do_shutdown, daemon=True).start()


def request_restart(delay_seconds=0.4):
    """Starts a fresh instance of the app (same executable/script that
    launched this one) and then stops the current process, on a short delay
    so the HTTP response can reach the browser first."""

    def _do_restart():
        time.sleep(delay_seconds)
        try:
            subprocess.Popen(_relaunch_command(), close_fds=True)
        except Exception:
            pass  # if relaunching fails, at least don't leave two instances running
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()


def launch():
    """Main entry point used by both run.py and desktop.py. Safe to call
    any number of times (e.g. the user double-clicking the launcher
    repeatedly) -- only ever one server ends up running."""
    db.init_db()

    running_port = _find_running_instance()
    if running_port:
        url = f"http://127.0.0.1:{running_port}/#/recent"
        print(f"Politician Trades Tracker is already running -- opening {url}")
        webbrowser.open(url)
        return

    app = create_app()
    port = _bind_port()
    url = f"http://127.0.0.1:{port}/#/recent"
    _write_server_info(port)

    def run_server():
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    time.sleep(0.75)  # give the server a moment to bind before loading it

    webbrowser.open(url)
    print(f"Politician Trades Tracker is running at {url}")
    print("Bookmark that address in your browser for quick access any time.")
    print(
        "It keeps running in the background (and checks for new disclosures "
        "automatically) even after you close this browser tab. Press Ctrl+C "
        "here to stop it."
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping.")
