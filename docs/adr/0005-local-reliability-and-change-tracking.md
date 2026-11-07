# Local reliability and course change tracking

Status: accepted for 1.12.0

Moodle outages, partial syncs and failed text extraction must be distinguishable
from expired credentials and current data. Multiple local MCP processes also
share credentials and status, so atomic rename alone is insufficient for a
read/modify/write update.

Use unique, owner-only temporary files and operating-system advisory locks for
shared status and settings. Hold the lock over read, merge and replacement;
process exit releases the lock. Keep the status JSON format for compatibility.
The existing full-sync lock remains independent of these short state locks.

Only GET and an explicit allowlist of read/session AJAX methods may retry.
Transient failures use bounded backoff, while confirmed login failures retain
the existing login flow. Background login recovery uses the dedicated browser
profile, a cross-process lock and a persisted cooldown. It never opens a visible
window. Materials-only profiles do not perform keepalive or recovery.

Keep the last searchable extraction when a changed document cannot be read, but
mark its results stale and report the failed file. A later successful extraction
clears the stale marker. Optional PDF page ranges use the same reader and a
bounded in-memory cache keyed by file version, range and extraction backend.

Directory exports have a per-course manifest of generated files and their hashes.
Remove an obsolete generated file only if it still has the recorded hash. Keep
user edits, unrelated files, and older exports with no manifest. A failed write
does not replace the previous manifest.

Course change history is a separate, local SQLite database with additive schema.
Store hashes and source metadata rather than duplicate course bodies. Initial
observations establish a baseline; later observations record changes, never
infer upstream deletion from an incomplete listing. No extra network request is
made for history: deadlines are observed during existing deadline reads and
staff replies during forum sync. Materials mode stores and exposes only file
and assignment-material changes. Events expire after 90 days and are capped at
5,000. File fingerprints reuse unchanged size/mtime metadata.

Keep fixed numeric timezone offsets as the fallback. An optional IANA timezone
handles Australian daylight saving; Windows installations include tzdata.
