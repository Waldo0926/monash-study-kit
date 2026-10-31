# Changelog

Version numbers follow `__version__` in `src/monash_study_kit/__init__.py`: patch for bug fixes, minor for new features.

## Unreleased

- Optional school Gmail, installed with `[gmail]`. `monash gmail login` uses your own Google Cloud Desktop OAuth client and asks for read-only access by default. `monash gmail sync` keeps a private local copy; each page is saved with its cursor, so a failed run keeps the pages it finished and the next run picks up from the failed page. `monash gmail todo` lists recent unread inbox mail without promotions and social updates. Search results now carry the date, unread, inbox, starred, important, category and attachment count.
- Optional attendance, installed with `[attendance]`: `monash attendance login`, `status`, `sync`, `overview` and `codes` read your classes and check-in status from the attendance portal and look up candidate codes in synced Moodle, Ed and school mail. Checked only against the Malaysia campus portal; `MONASH_ATTENDANCE_ORIGIN` and `MONASH_ATTENDANCE_TZ` point it elsewhere.
- Local MCP: logging in turns on the read-only tools (`gmail_enabled`, `attendance_enabled`). Mail tools add `get_gmail_inbox_todo` and `gmail_read_attachment`; attendance adds status, overview, sync, code lookup and export, the last three as background jobs read with `student_job_status`. Mail tools and the server instructions tell the AI app to use them for questions about "my email" instead of a general Gmail connector. `monash help` and the help prompt list mail and attendance only when they are on.
- Drafting, sending a draft, changing labels and submitting attendance are not registered in the local MCP unless you set `gmail_manage_enabled` or `attendance_submit_enabled`. Each one returns a full preview and a single-use ticket bound to that payload, and runs only with the user's explicit confirmation of that action. Nothing permanently deletes mail.
- While the MCP server runs, mail syncs in the background every 30 minutes (`gmail_sync_minutes`, 0 turns it off), keeps the last query, continues an unfinished first download about a minute later, and indexes a few attachments per round. An expired Gmail login is logged once.
- Remote MCP: each app picks on the pairing page which mail and attendance features it may use. Only features enabled on the server are offered; read-only ones start ticked, write ones unticked. The choice is stored with the tokens and kept on refresh, intersected with the server settings at every call, and shown by `monash remote clients`. Apps paired earlier keep Moodle and Ed only. Review tickets and jobs are per app, so one app cannot run another's ticket. Remote apps get the shared catalog (`gmail_search`, `gmail_thread`, `gmail_sync`, `attendance_overview`, `attendance_find_codes` and others) plus `gmail_inbox_todo` and `student_job_status`.
- Setup guide in [docs/google-and-attendance-setup.md](docs/google-and-attendance-setup.md) (Chinese), design notes in [docs/gmail-foundation.md](docs/gmail-foundation.md), and a privacy policy in [PRIVACY.md](PRIVACY.md). The core still depends only on pypdf.
- `gmail_prepare_action` refuses early with a clear message when the Google authorization is read-only, instead of failing at execute time and leaving the ticket as unknown.
- `monash update` keeps every optional extra that is installed (`gmail`, `attendance`, `media`), not only `media`, so updating does not remove Gmail or attendance support.
- `monash status` and `monash doctor` include school mail and attendance when they are on. Doctor output never shows the mail address. A missing `[gmail]` install now says what to install instead of looking like a network error.
- The agent skill lists the optional mail and attendance commands and the rule that `--confirm` is only added after the student confirms the preview.
- Token cost when switched on: about 730 more per conversation for Gmail, 590 for attendance, 1,900 for everything including the write switches. Nothing changes while they are off.
- Attendance reads follow the timetable instead of the clock while the MCP server runs: weekday mornings at 8 and 15 minutes after each class not yet checked in to, nothing at night, at weekends or for classes already done (`attendance_auto_sync`, on by default).
- A browser read opens the portal page once and switches days in its menu, instead of reloading the page for every day.
- Browser-free read (`attendance_fast_read`, on by default): one request with the saved login cookies and a Python port of the parser. On the Malaysia portal both reads matched on all 15 days it offers. It falls back to the browser whenever the page does not look as expected. `monash attendance probe` compares both reads on the real portal and prints only counts and whether they match. Tests compare the Python and JavaScript parsers on the same pages.
- The browser-free read verifies HTTPS with the operating system's certificate store (`truststore`, part of `[attendance]`), so a portal that does not send its intermediate certificate still verifies on macOS and Windows. If it cannot be verified the read falls back to the browser and `probe` says why; verification is never turned off.
- The attendance login lifetime is now measured: logins, successful reads and the first expiry are recorded, and `monash attendance status` shows how long the current login has lasted and when earlier ones expired. Optional lightweight keepalive (`attendance_keepalive_minutes`, off by default) renews the login with a plain request and saves any new cookies.
- Not verified yet: attendance submission, how long the attendance login lasts, whether the browser-free read and the keepalive work on the real portal, and portals of campuses other than Malaysia.

## 1.4.1

- `monash --help` now lists `monash library` and `monash remote` in its summary at the top. They were already in the subcommand list below.
- The real Chromium pipe test waits longer for google-chrome to start on CI.

## 1.4.0

- Study todo, Ed Lesson progress and quiz practice now include optional MCP Apps panels. Existing tools keep their arguments and text results; clients without panel support still receive text. Quiz selections stay in the panel and are sent to the AI for explanation only, never submitted to Ed.
- Add `search_moodle_activities` to locate Moodle sections and activities by name, reusing the existing CLI finder. The local catalog has 18 tools; the original 17 names and schemas are unchanged.
- Add `monash library [--open]`: a loopback-only, read-only browser for registered Moodle and Ed Lesson files. Filter by course, week or filename and download files without an AI client.
- Add optional single-user `monash remote serve`, `pair`, `clients` and `revoke`. The remote service binds on loopback behind the owner's HTTPS proxy, uses one-time pairing, PKCE and rotating OAuth tokens, and exposes only the 16 query tools. Local login and sync commands cannot be invoked remotely. The server is never started by setup or the default stdio flow.
- Package the panel HTML in the wheel and test it from a clean installation. No assignment submission, quiz submission, posting, private server settings or hosted service is added.

## 1.3.0

- Questions about an assignment now follow a fixed order of sources. The Moodle brief and marking rubric come first. On Ed, only replies from teaching staff and answers they endorsed count; other students' replies are leads, not rules. A later staff correction wins, but when the brief and Ed disagree Claude lists both with their dates and suggests asking staff instead of picking one. Every rule it states names where it came from. This is in the server instructions, so it applies without doing anything.
- New preset prompt "作业要求" (assignment requirements) in the "+" menu: give it an assignment name and it reads the brief, the rubric and staff answers on Ed, and lists what to hand in, how it is marked, deadlines and format, and Ed clarifications, each with its source.
- Agent skill at `skills/monash-study-kit/SKILL.md`, installable with `npx skills add Waldo0926/monash-study-kit`. It maps questions to `monash` commands, carries the same rule for assignment questions, and lists what the agent must leave to the student (logins, tokens, submitting). A test checks every command in it against the CLI and the exit codes against `EXIT_CODES`.
- The package author is listed as Waldo.

## 1.2.0

- `monash connect <client>` connects Study Kit to Codex, Cursor, VS Code, Windsurf or Gemini CLI in one command. It adds only the `monash` entry with the full path to the program, keeps the rest of the config file and saves a backup first. Codex uses `codex mcp add` when the `codex` command is installed. `monash connect` alone lists the clients and whether each is connected, `--print` shows the entry without writing it, and `--remove` takes it out. A config file that isn't plain JSON (for example with comments) is left alone and the entry is printed to paste by hand.
- `monash uninstall` also removes the entry from those clients.

## 1.1.1

`monash doctor --deep` on my server reported four problems that weren't real. Fixed in the checks, no change to syncing:

- "Never synced" on a deployment that syncs Moodle with its own script: the last sync time now falls back to the one stored in the Moodle database when `status.json` has none (`doctor`, `monash status`).
- Files the teacher made available but hidden from the course page (Moodle "stealth" activities) were counted as missing. Sync follows the course page, so these are skipped.
- Videos served from CloudFront were counted as missing: the CloudFront address has no file extension, so the check now also looks at the Moodle file name, as sync does.
- The search probe sometimes picked a word inside a link (for example a Google Docs ID in `links.md`), which the index stores as a different word. It now picks whole words outside links.
- Ed posts changed after the last sync started are no longer reported as stale; the next sync picks them up.

## 1.1.0

The code that runs my own server deployment (moodle-sync on a VPS, and ed-digest) used to be a copy of this package and had drifted. Its fixes and features are now here, so both deployments install this package instead of carrying their own copy.

- Full-text search also covers text files inside `.zip` archives (starter code, READMEs). A hit carries `inner`, which `read_file` takes directly.
- `list_files` takes `since_days` to answer "what's new this week", and `monash todo` / `get_study_todo` list course files synced in the last N days.
- Ed reading pages saved by ed-digest (`lesson_texts`) are indexed and searchable, and Ed hits carry the original link.
- Calendar items from the subscription link get a clean activity name: Monash's "- Allocate+ ..." and "- CALLISTA: ..." suffixes are stripped.
- Forum lists: a row without a discussion id (a pinned-section divider) is skipped instead of breaking the whole list.
- A lecture transcript is hidden together with its video when the video is removed from Moodle.
- Interrupted downloads: leftover `.dl-*` files are cleaned up at the start of each unit's sync, temporary names no longer end in `.part.part`, and a cut-off Zoom download is deleted.
- `monash media` caches each video's fingerprint, so it no longer re-reads every transcribed video on each run.
- Ed: replies under a deleted reply are kept, as Ed still shows them.
- Saving the Moodle cookie or Ed token uses a per-process temporary file, so two MCP servers (Claude Desktop and Claude Code) refreshing at once can't clash.
- Security fixes from a review of the whole package (`docs/security.md`):
  - The Moodle cookie is only sent over the same scheme as `MOODLE_BASE_URL` (HTTPS), never to an `http://` link.
  - The Ed token is no longer carried along if Ed ever redirects a request to another site.
  - The login window talks to the browser over a pipe on macOS and Linux, so no DevTools port is open while you log in.
  - `read_file` refuses text files over 30 MB and zip entries that unpack to more than that, and docx/pptx text is read with a size cap.
  - `monash doctor` also hides `sesskey` values and scrubs error messages from checks that crash.
  - Symlinks in the course-file folders that point outside them are no longer indexed.
  - `read_file` refuses a file that was taken down on Moodle, even when asked for it by path.
- `monash doctor --deep` cross-checks the tools against a second source: upcoming due dates against the iCal feed, the assignment overview page against the course structure, synced files against the course structure (lecture videos that are deliberately not downloaded are told apart), the search index against the files on disk plus a few real searches that must find their file again, and the latest Ed posts against the local copy. It only reads, and prints counts, unit codes, dates and activity ids, never titles, file names or grades.
- Published on PyPI: install with `uv tool install monash-study-kit`. `monash update` installs from PyPI (falling back to GitHub), and the update notice checks PyPI first.
- Releases are made by pushing a tag: CI checks the tag against `__version__` and this changelog, runs the tests, uploads with Trusted Publishing, installs the result back from PyPI on three OSes and creates the GitHub release. Every PR also installs the built wheel into a clean venv and runs `monash --version`, `monash help` and an MCP handshake. See `docs/releasing.md`.
- New settings `name_limit` and `pdf_extractor` (`pypdf` or `pdftotext`). Every setting can also be overridden with `MONASH_KIT_<NAME>`, and `MONASH_KIT_DATA`, `MONASH_KIT_SECRETS`, `MONASH_KIT_ED_DATA` and `MONASH_KIT_ED_TOKEN` point the package at an existing server layout.

## 1.0.3

Fixes for things that went wrong halfway through a sync.

- Ed Lessons: if one lesson's details fail to load (a 500 or a timeout), its saved slides and quiz questions are kept instead of being wiped until the next sync.
- Ed Lessons: lessons that were deleted or hidden on Ed are removed from the local database and from `ed-files`, so search stops returning them.
- Downloads cut off halfway: Ed lesson PDFs are now written to a `.part` file first, so a half file is never treated as finished, and Moodle downloads clean up their `.part` file instead of leaving it in the course folder.
- Moodle files: files a teacher removed or re-uploaded under a new name are marked as removed after a clean sync, and drop out of `list_files`, `monash moodle ls`, search and `monash media`. The files stay on disk, and a file that comes back on Moodle is restored. Nothing is marked when any activity in the unit failed to load that run.
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
