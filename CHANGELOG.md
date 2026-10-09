# Changelog

Version numbers follow `__version__` in `src/monash_study_kit/__init__.py`: patch for bug fixes, minor for new features.

## 1.0.3

Fixes for things that went wrong halfway through a sync.

- Ed Lessons: if one lesson's details fail to load (a 500 or a timeout), its saved slides and quiz questions are kept instead of being wiped until the next sync.
- Ed Lessons: lessons that were deleted or hidden on Ed are removed from the local database and from `ed-files`, so search stops returning them.
- Downloads cut off halfway: Ed lesson PDFs are now written to a `.part` file first, so a half file is never treated as finished, and Moodle downloads clean up their `.part` file instead of leaving it in the course folder.
- Ed posts: replies deleted on Ed are removed from the local copy the next time that post is fetched.
- `monash todo` / `get_study_todo`: when Moodle can't be reached, the Ed half (lessons, announcements, unread replies) is still returned, with a note about what is missing.
- Full-text index: one unreadable file (encrypted, truncated, deleted mid-sync) no longer stops the whole index run.
- Word / PowerPoint text: `&amp;` and `&lt;` are decoded, and words split across formatting runs are joined, so searching "Monads" finds a slide written as "Mon" + "ads".
- Course-notes websites: `robots.txt` is now respected (checked once a day per site).
- MCP server: a malformed message gets a JSON-RPC error instead of crashing the server, and an unexpected error in the background loop is logged instead of silently stopping hourly sync and session refresh.
- `monash config`: number settings accept decimals (`tz_offset 9.5` for Adelaide, `auto_sync_hours 0.5`), and a typo in a true/false setting is rejected instead of being read as false.
- Login window: cookies are only taken from Moodle's own domain or its parent domains, matched on a dot boundary.
- CI runs `ruff` for syntax errors, undefined names and unused imports.

## 1.0.2

- MCP tools renamed to a consistent `get_` / `list_` / `search_` / `read_` / `start_` / `sync_` scheme. The old names still work when called.

## 1.0.1

- Every MCP tool parameter has a short description, and tool descriptions say which related tool to use instead.

## 1.0.0

First stable release. See the [release notes](https://github.com/Waldo0926/monash-study-kit/releases/tag/v1.0.0).
