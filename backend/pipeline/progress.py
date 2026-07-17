"""
Lightweight, best-effort progress tracking for a single refresh run.

The pipeline doesn't know the total amount of work up front (the House
Clerk's yearly indexes and the Senate eFD search results are only known once
fetched), so ProgressTracker simply accumulates "total units of work
discovered so far" and "units completed so far" as the pipeline goes,
exactly like a download manager whose total grows as more of a directory
listing is read. The resulting percentage can occasionally tick backwards
slightly when a new batch of filings is discovered -- that's expected and
preferable to a fake, precomputed total.

A single ProgressTracker instance is created per refresh job (see
app.py's _start_refresh_job) and threaded through data_fetch.refresh_data ->
pipeline.orchestrator.run_pipeline -> the individual source collectors
(house_clerk.collect_trades / senate_efd.collect_trades), which call
add_total()/add_completed() as they discover and process filings.
"""

import threading
import time


class ProgressTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.started_at = time.monotonic()
        self.total_units = 0
        self.completed_units = 0

    def add_total(self, n):
        if n <= 0:
            return
        with self._lock:
            self.total_units += n

    def add_completed(self, n=1):
        if n <= 0:
            return
        with self._lock:
            self.completed_units += n

    def snapshot(self):
        """Returns a plain dict safe to serialize over the API:
          - completed / total: raw unit counts
          - percent: 0-100 (None if total isn't known yet)
          - eta_seconds: estimated seconds remaining, based on the average
            rate so far (None until there's enough data to estimate)
        """
        with self._lock:
            completed = self.completed_units
            total = self.total_units
            elapsed = time.monotonic() - self.started_at

        percent = None
        eta_seconds = None
        if total > 0:
            percent = max(0.0, min(100.0, completed / total * 100))
            if completed > 0 and completed < total and elapsed > 1:
                rate = completed / elapsed
                if rate > 0:
                    eta_seconds = max(0, (total - completed) / rate)
            elif completed >= total:
                eta_seconds = 0

        return {
            "completed": completed,
            "total": total,
            "percent": percent,
            "eta_seconds": eta_seconds,
        }
