"""
Android-only bootstrap for the Lite app, invoked from Kotlin (via Chaquopy)
by PythonServerService, not from run.py/desktop.py.

Deliberately does not reuse launcher.launch() -- that function's
single-instance detection, webbrowser.open() call, and os._exit()-based
shutdown/restart all assume a desktop process model that doesn't apply on
Android (one Activity, one process, and killing the process would take the
whole app down with it, not just the Python server). This module only
borrows launcher._bind_port(), which is plain socket logic with no such
assumptions.
"""

import os
import threading

_started = False
_port = None
_lock = threading.Lock()


def start(files_dir):
    """Starts the Flask server (once) against Android's private storage
    directory, returning the port it's listening on. Safe to call more than
    once (e.g. if the foreground service is restarted by the OS) -- returns
    the already-bound port instead of starting a second server."""
    global _started, _port

    with _lock:
        if _started:
            return _port

        os.environ["POLITICIAN_TRADES_DATA_DIR"] = files_dir
        os.environ["POLITICIAN_TRADES_ANDROID"] = "1"

        # Imported here, not at module level: backend.db computes its
        # module-level DB_PATH constant from get_data_dir() the moment it's
        # first imported anywhere in the process, so the env vars above
        # must already be set before that happens. This module is always
        # the first thing Kotlin imports (see PythonServerService.kt), so
        # deferring these imports until after the env vars are set
        # guarantees the right data dir is picked up everywhere, including
        # by settings.py/snapshot_download.py/market_data.py, which all
        # call db.get_data_dir() themselves.
        from . import db
        from .app import create_app
        from .launcher import _bind_port

        db.init_db()
        app = create_app()
        _port = _bind_port()

        def run_server():
            app.run(host="127.0.0.1", port=_port, debug=False, use_reloader=False)

        threading.Thread(target=run_server, daemon=True).start()
        _started = True
        return _port
