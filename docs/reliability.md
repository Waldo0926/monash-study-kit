# Sync reliability and course changes (1.12.0)

`monash status` and `get_monash_status` distinguish a complete sync from a
partial one. `last_sync` remains the latest attempt for compatibility;
`last_complete_sync` records when all reported failures were last resolved.
Individual service results include failure counts and retry identifiers.

```bash
monash sync --retry-failed
monash changes
monash changes --days 7 --json
monash changes --since '2026-11-01T00:00:00+08:00'
```

Retry mode re-reads failed Moodle activities, Ed threads/Lessons and index
entries. If only a service-level error was available, it retries that service.
It does not re-fetch successful services, or mark unrelated files removed.
It cannot be combined with course selection, dry-run or Google refresh.
A normal sync still discovers new material and retries missing material.

Changes are local observations, not a complete upstream audit trail. The first
observation establishes a baseline. Later observations detect new or changed
Moodle/Ed files, assignment instructions, deadline times and teacher replies.
They include observation times and source links, with old/new times for deadline
changes. Deadline history updates when an existing due/todo query reads the
calendar; change tracking itself makes no additional school requests. Material
changes are observed during sync. `monash changes` defaults to the latest sync
window; choose `--days` or `--since` for older observations. The study todo MCP
response also includes a bounded change list. Nothing is implicitly marked read.
History stays in `data/changes.db`, with a 90-day / 5,000-event limit. Snapshot
fingerprints and source metadata remain local; course bodies are not duplicated.
Materials-only profiles omit personal deadlines and forum information.

Search results from a failed refresh of a previously indexed document carry
`stale`, `indexed_at`, `index_error` and a warning. Verify the source before using
these results as current assignment requirements. A successful reindex clears
the warning; new unreadable documents appear in index failure statistics.

The existing `read_file` MCP tool accepts `page_start` and `page_end` for PDFs.
Pages start at 1, are inclusive, and a call may select up to 100 pages. With only
`page_start`, it reads that page. Without a range, the original whole-document
behavior remains. Character pagination applies within the selected pages.
The extraction cache is process-local, bounded, and invalidated by file version
or extraction-backend changes.

Moodle outages and rate limits are reported separately from authentication
failures. GETs and explicitly allowed read/session AJAX calls retry at most twice;
form submissions are never automatically replayed. A failed keepalive caused by
a temporary connection error retries after one minute. After a real session
expiry, background recovery tries the remembered dedicated browser profile
without opening a window. Attempts have a shared 15-minute cooldown; if MFA is
needed, status shows `needs_login`. Run `monash login`, or explicitly request the
MCP login tool. Disable automatic recovery with:

```bash
monash config auto_relogin false
```

Keepalive and recovery only run while an MCP server is running and the computer
is awake. They cannot override the school's login/session policies. Materials-only
mode skips both.

Directory exports now keep `.monash-export-manifest.json` and an advisory lock
inside the course directory. On re-export, obsolete generated files are removed
only when unchanged since the previous export. Edited obsolete files and user
notes are preserved; preserved edited paths are returned in export statistics.
Pre-1.12 exports have no manifest, so their old files are retained on the first
new export. ZIP output keeps its existing snapshot behavior.

For Australian campuses, set an IANA timezone matching the Moodle account:

```bash
monash config timezone Australia/Melbourne
# Or Australia/Adelaide for its half-hour offset and daylight saving.
```

This controls page-time parsing and CLI date display. Leave `timezone` empty to
retain the existing numeric `tz_offset` parsing and the computer's display zone.
Restart the MCP client after changing settings. Windows installs include tzdata;
minimal Linux images may need their system timezone database or `pip install tzdata`.
