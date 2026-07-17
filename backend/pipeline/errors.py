"""
Shared exception for cooperative refresh cancellation.

A single, lightweight exception type used across the pipeline and
data_fetch.py so that a user-requested "Stop Refresh" (see app.py's
/api/refresh/stop) can unwind cleanly through whichever stage of a refresh
happens to be running -- legislator directory sync, House Clerk PDF
parsing, Senate eFD scraping, etc -- without leaving partial/inconsistent
writes for the step that was interrupted. Each step's database writes only
happen after that step's fetch+parse work completes, so raising this
between steps (or between individual filings, within a step) never
corrupts already-committed data -- it just stops before doing more work.
"""


class RefreshCancelled(Exception):
    """Raised to unwind out of an in-progress refresh once the user has
    clicked "Stop Refresh". Caught at the top level in app.py's
    _start_refresh_job, which records a clean "stopped by user" status
    instead of treating this as an error."""
    pass


def check_cancelled(cancel_check):
    """Raises RefreshCancelled if `cancel_check` (a zero-arg callable
    returning bool, typically threading.Event().is_set) is given and
    currently true. No-ops if cancel_check is None -- callers that aren't
    running as part of a cancellable refresh (e.g. a script or test) can
    simply omit it."""
    if cancel_check and cancel_check():
        raise RefreshCancelled("Refresh stopped by user")
