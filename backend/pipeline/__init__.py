"""
Congressional trading data collection pipeline.

This package implements the resilient, multi-source ingestion architecture
described (as future work) in backend/settings.py:

- backend/pipeline/http_client.py -- shared HTTP layer with retries
  (exponential backoff on transient network/5xx errors) and on-disk caching
  (conditional GET via ETag/Last-Modified, content-hash fallback), reusing
  the same data/cache/ directory and source_cache table as data_fetch.py.
- backend/pipeline/house_clerk.py -- primary source: House Clerk bulk ZIP
  downloads of Periodic Transaction Reports (index + per-filer PDFs).
- backend/pipeline/senate_efd.py -- primary source: Senate eFD search
  (session handshake + DataTables search API + per-filing HTML reports).
- backend/pipeline/secondary_sources.py -- secondary/API sources (House/
  Senate Stock Watcher JSON dumps) used as a fallback whenever a primary
  source's bulk download is unreachable or fails to parse.
- backend/pipeline/ocr.py -- optional OCR fallback (via the pytesseract
  binding to the Tesseract OCR engine) for House PTR PDFs filed as scanned
  images with no text layer at all, which house_clerk.py's normal
  text-based table parser can't read. Entirely optional and degrades
  gracefully: if Tesseract isn't installed, those (rare) filings are
  reported as an unrecognized format exactly as before this existed.
- backend/pipeline/dedup.py -- content hashing / checksum bookkeeping so a
  filing that has already been successfully parsed is never re-fetched or
  re-parsed unless its content has actually changed.
- backend/pipeline/monitoring.py -- structured logging of parse failures,
  format-version-mismatch alerts, and stale-data alerts, backed by both a
  rotating log file (data/logs/pipeline.log) and the pipeline_events /
  pipeline_source_status tables (surfaced via /api/pipeline/status).
- backend/pipeline/orchestrator.py -- top-level entry point (run_pipeline())
  that ties the above together: try each primary source's API/bulk path,
  fall back to secondary sources on failure, normalize everything into the
  trades table schema, and record run-level status/alerts.
"""
