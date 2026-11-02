# Changelog

Version numbers follow `__version__` in `src/monash_study_kit/__init__.py`: patch for bug fixes, minor for new features.

## 1.11.1

- `monash login` no longer crashes with “Browser context management is not supported” when the login window is closed before signing in finishes (on macOS Chrome keeps running without a window); it says the window was closed and how to retry.
- PDF export on Windows no longer fails when Chrome's helper processes still hold the temporary profile for a moment after closing; cleanup retries and never fails the export.
- `monash export` accepts `--refresh-google` (implies `--sync`) to re-export the course's Google files before packaging.

## 1.11.0

- Course exports now turn Markdown body pages, link lists, quizzes and the index into PDFs by default, keeping original course files. `monash export --format pdf|both|md` overrides the `export_format` setting; synced Markdown and MCP indexing are unchanged.
- PDFs are printed offline with one isolated temporary Chrome / Edge / Brave process per export. Local exported images are embedded, raw HTML is escaped, and remote requests are blocked. Without a browser, exports fall back to Markdown with an explanation in the index and terminal.
- Cache PDFs by the complete printable HTML and embedded image contents under `data/pdf-cache/`, evicting the oldest entries above 500 MB. JSON exports report generated and cached PDF counts and the fallback reason.

## 1.10.1

- Ed images whose address has no file extension are saved as `.png`, `.jpg`, `.gif`, `.webp` or `.svg` according to their content, instead of `.img`, which viewers did not show. Images saved as `.img` by earlier versions are renamed on the next sync, and the references in the Markdown pages are updated.

## 1.10.0

- Add an early account-selection entry point: `--profile NAME`, `--profile=NAME`, or `MONASH_KIT_PROFILE` isolates credentials, databases, materials and browser sessions under the default data home's `profiles/NAME`. Names are validated, directories are private, per-directory overrides cannot escape named profiles, and the default account keeps its existing behavior.
- Add `profile list` and confirmed `profile remove NAME [--all] [--yes]`. Removal revokes the target's Drive and Gmail OAuth authorizations before deleting any local session files; any revocation failure preserves all local files. Materials remain unless `--all` is used. No Moodle logout is requested, and the completion message points to Ed's manual API-token deletion page.
- Named profiles stay local: refuse remote serving, AI configuration and independent moves; setup only logs in and selects courses. Add `materials_only`, false by default and forced true for named profiles, with centralized CLI and MCP allowlists.
- Materials mode syncs Ed course lists and Lessons without discussions, preserves Moodle and linked Google material exports, and skips Gmail sync, attendance reads/keepalive, personal-calendar setup and private diagnostic checks. Its MCP surface is limited to file listing/reading/search, Lessons/quizzes and local Ed export; forbidden tools and aliases fail before their handlers run.
- Document complete command/tool policies, account isolation and cleanup. Add synthetic and subprocess tests for import order, directory isolation, settings enforcement, local-only restrictions, materials sync, background work, diagnostics, safe removal and credential-free listings.

## 1.9.0

Course exports now contain what used to be left as links, with the reason for anything still missing.

- Moodle Page, Book and assignment text, and the rubric or marking guide students can see, are saved as Markdown. Images in them are downloaded to `assets/` (named by content hash, so duplicates are stored once). Links to this unit's pages, files, folders and books are followed instead of listed. Books are read through the print view, which Moodle does not count as a view.
- Folders are downloaded whole (`download_folder.php`, not counted as a view), keeping subfolders, case and original file names. They are refreshed at most weekly, and if the zip is unavailable the files are fetched one by one. Images that a notebook references but that are missing (or differ only in case) are reported.
- Images in Ed lesson pages are downloaded and de-duplicated, with links rewritten to relative paths. Failed downloads keep the remote link and are listed in the export index. Ed and whole-unit exports include `assets/`.
- Google Slides, Docs, Sheets and Drive files linked from a unit are exported read-only through the Drive API: Slides and Docs to PDF (Docs optionally DOCX with `monash config google_docs_format docx`), Sheets to XLSX, Drive files as they are. When a file is too large or an administrator blocks the API, the dedicated login browser's Google session is tried; its cookies stay in memory. Failures keep the link and record the reason. New `monash google login` / `status` / `logout` use a separate token with only `drive.readonly` (`logout` revokes it first); `monash sync --refresh-google` exports again.
- Ed units whose code matches a tracked Moodle unit are tracked automatically, including archived and combined units. This is why a past unit's Ed Lessons could be missing. A choice made in `monash courses` is kept. `monash export --sync` syncs the unit's Moodle and Ed content first.
- The export index lists everything not exported with its reason, and gains a statistics section (links files, remote images left, Google links exported or failed by reason), also in `--json`.

## 1.8.0

One export for a whole unit, and this package is now the only copy of the Moodle, Ed, Google and attendance code (the author's private server deployment depends on it instead of carrying its own copies).

- `monash export FIT1043` (`--to`, `--dir`, `--open`, `--json`) writes a unit's course materials into one zip (or folder): `Moodle/` with the synced files and a `links.md` per folder, `Ed Lessons/` with files, `Links.md` and a `Quiz.md` (questions only) per lesson, and a `README.md` index. The index lists what was not exported and why: a file missing on disk, an Ed file that failed to download, or an Ed unit that was never synced because it is not tracked or not in your Ed course list. It reads only local data and leaves out Ed posts, submissions, grades, feedback, mail, messages and attendance.
- Meeting passwords (`pwd=`, `passcode=` and similar) are removed from links in the export.
- Choose where the data lives. The first `monash setup` asks, listing each drive with its free space (another drive is recommended when C: has under 5 GB free). `monash move D:\MonashStudyKit` moves course files, database, logins and settings in one go; the default folder then keeps only a `location.json` pointer. `MONASH_KIT_HOME` still overrides both.
- A test now fails if a server path (`/opt/`, `/root/`, `/srv/`) appears anywhere in the package or its docs.

## 1.7.0

This is also the first release on PyPI with the 1.6.0 changes below (the local semester week view and private calendar export); 1.6.0 was never published on its own.

Ed Lessons materials are organised like Ed and can be taken away as one zip, matching Ed Web's "download all".

- `monash ed export FIT2109` (and `--module`, `--to`, `--open`) packs the synced Ed Lessons of a unit into a zip in Downloads: one folder per module, files in Ed's order, a `Links.md` per lesson for reading pages and the Ed page, and a `README.md` with progress and any files that failed to download. It reads only local files.
- The same export is a button per unit on the `monash library` page and the `export_ed_lessons` tool for AI apps (not read-only, so remote mode does not expose it; it adds about 360 characters to the tool list and is hidden with `use_ed false`).
- Ed files are stored as `<unit code>/<NN module>/<NN lesson>/<NN title>`, instead of numeric course and lesson ids. The first sync moves files from the old layout instead of downloading them again. `monash open ed` opens the folder.
- Files that Ed hosts inside document and code pages (`<file>` on `edusercontent.com`) are downloaded and indexed, up to 200 MB each. Links to other sites are not followed.
- A PDF replaced on Ed (new file URL) is downloaded again; files of slides removed on Ed are deleted, and empty folders are cleaned up.
- `list_files` includes Ed Lessons files (with `ed:` paths for `read_file`), filtered by unit, week or name.
- Fix: the `monash library` page now lists Ed files synced by this package (it expected absolute paths and skipped the relative ones in `ed.db`).

## 1.6.0

- `monash semester --days 7` combines Moodle deadlines, local Ed lesson/reply/announcement summaries and the optional attendance cache into one local-first view. No account data is sent to Monash Hub.
- `monash semester --ics [PATH]` exports verified timestamped Moodle deadlines as a private `.ics` file; it is a snapshot, not a background subscription or an attendance record.
- This is a CLI view; it does not create an externally hosted dashboard. Missing Moodle/Ed data remains explicitly marked as missing.

## 1.5.0

Optional school mail and attendance. Both are off until you connect them, the core still depends only on pypdf, and Moodle and Ed stay read-only.

**Connecting**

- Ask your AI app "connect my school email" or "connect my attendance". The new `start_school_service_setup` tool installs what is missing into the running install, opens the sign-in window on your computer and turns everything on, then syncs and tells the AI app its tool list changed. Passwords and codes stay in your browser. The tool adds 145 tokens per conversation and is hidden once both are connected.
- In a terminal: `uv tool install --reinstall "monash-study-kit[gmail,attendance]"`, then `monash gmail login` and `monash attendance login`.
- `monash gmail login` uses a Google app that ships with the package, so no Google Cloud project is needed. Google shows "this app isn't verified" (Advanced, then continue). `--client` uses your own app instead. For people who only want to ask about their inbox, the guide suggests the AI apps' built-in Gmail connectors.

**School mail** (`[gmail]`)

- A private local copy: `monash gmail sync` saves each page with its cursor and prints progress, so an interrupted run keeps what it finished and the next run continues. While your AI app is open, mail syncs every 30 minutes (`gmail_sync_minutes`) and a few attachments per round are indexed.
- `monash gmail todo` and `get_gmail_inbox_todo` list recent unread inbox mail without promotions and social updates. Search results carry the date, unread, inbox, starred, important, category and attachment count. `gmail_read_attachment` reads PDF, Office and image attachments.
- Drafting, sending a draft and changing labels (archive, mark as read, star) are on after login. Every action returns a full preview and a single-use ticket bound to that payload, and runs only after you confirm that exact action. Nothing permanently deletes mail. `monash gmail login --read-only` asks Google for read access only. See [ADR 0004](docs/adr/0004-confirmed-mail-and-attendance-actions.md).
- The server instructions tell the AI app to use this for questions about "my email" instead of a general Gmail connector.

**Attendance** (`[attendance]`)

- `monash attendance login`, `status`, `sync`, `overview`, `codes` and `probe`, and matching tools in the AI app: classes and check-in status from the portal, and candidate codes found in synced Moodle, Ed and school mail, each with its source.
- A read is one plain request with the saved login (about 5 KB, a few seconds) and a Python port of the extension's parser. On the Malaysia portal it matched the browser read on all 15 days the portal offers. It falls back to a single headless browser page whenever the page looks different or the certificate cannot be verified, and HTTPS is checked with the system certificate store (`truststore`).
- Reads follow your timetable: weekday mornings at 8 and 15 minutes after each class you have not checked in to, nothing at night, at weekends or for finished classes (`attendance_auto_sync`).
- Check-in is on after login and is prepared only when you say you attended, with the same preview and confirmation as mail.
- `monash attendance status` records how long the login lasts. An optional lightweight keepalive (`attendance_keepalive_minutes`) is off until that is known.
- Checked only against the Malaysia campus portal; `MONASH_ATTENDANCE_ORIGIN` and `MONASH_ATTENDANCE_TZ` point it elsewhere.

**Remote MCP**

- Each app chooses on the pairing page which mail and attendance features it may use. Only features turned on on the server are offered; read-only ones start ticked, actions unticked. The choice is kept on refresh, checked against the server settings at every call and shown by `monash remote clients`. Apps paired before 1.5.0 keep Moodle and Ed only. Tickets and background jobs belong to one app.

**Other changes**

- Units without Ed: `monash config use_ed false`, or answering yes in `monash setup` after skipping the Ed token, stops the reminders about a missing token and hides the Ed tools and help.
- `monash update` keeps every installed extra (`gmail`, `attendance`, `media`).
- `monash status` and `monash doctor` cover mail and attendance once they are on; doctor output never shows the mail address.
- `monash help` and the help prompt list mail and attendance once connected, and suggest connecting them until then.
- The agent skill lists the mail and attendance commands and the rule that `--confirm` comes only after the student confirms the preview.
- Token use: 3,377 per conversation by default (3,232 in 1.4.1), 5,132 with mail and attendance connected.
- Docs: setup guide in [docs/google-and-attendance-setup.md](docs/google-and-attendance-setup.md) (Chinese), design notes in [docs/gmail-foundation.md](docs/gmail-foundation.md), privacy policy in [PRIVACY.md](PRIVACY.md).

Not verified yet: attendance check-in on a real account, how long the attendance login lasts, the keepalive, and portals of campuses other than Malaysia.

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
