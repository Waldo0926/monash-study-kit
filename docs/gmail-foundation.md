# Gmail mirror and mailbox actions: design notes

How the optional Gmail part works and why it is built this way. Setup steps (Google
Cloud project, OAuth client, signing in with the school account, Attendance) are in
[google-and-attendance-setup.md](google-and-attendance-setup.md) and are not repeated
here. What data is accessed and stored is in [PRIVACY.md](../PRIVACY.md).

Status: the shared modules (`gmail.py`, `service_actions.py`, `service_jobs.py`,
`student_services.py`, `attendance.py`, `attendance_evidence.py`) are the versions that
run on the author's private deployment, tested against a real Monash mailbox and the
Malaysia attendance portal on 2026-10-31. The public CLI and local MCP use the same
modules. Released in 1.5.0.

## Reading is compatible with downloading

`monash gmail login --read-only` asks for `gmail.readonly`. It is enough to download messages
and attachments, and it cannot change the mailbox, including unread status. Without the flag
login asks for `gmail.modify` (see Mailbox actions); downloading works the same way and still
never marks mail as read. Bodies, the
original Gmail `format=full` JSON payload, labels and attachment metadata go into a
private SQLite database with an FTS5 full-text index. This is a real local copy, not a
browser session that has to keep Gmail open. The payload JSON is not a byte-for-byte
RFC 822 `.eml` export.

Attachment bytes are downloaded on demand. The filename from the sender is only
metadata; the file on disk is named by its SHA-256, so a name like `../../x` cannot
escape the cache folder. `read_attachment` extracts text from PDF, Office, plain text
and images (OCR) and adds it to the index. The cache relies on owner-only file
permissions, not application-level encryption, and is never placed in the course-file
web directory. There is no automatic expiry for cached mail; deleting the data folder
removes it.

## Sync

`gmail.sync` is bounded and resumable:

- The first run (backfill) lists messages for the chosen query, `newer_than:90d` by
  default. `--query ''` asks for everything. `--pages` caps how many pages one run handles;
  running it again continues where it stopped.
- Each page is committed together with its page cursor. If page 3 fails, pages 1 and 2
  stay saved and the cursor still points at page 3, so the next run starts there. Nothing
  from the failed page is half saved.
- After the backfill, sync follows `historyId`. Label changes in history carry the full
  label list, so they are applied to the cached copy without downloading the message
  again. Deletions also need no download. Only new or uncached messages cost a
  `messages.get`. A label event without the full list is downloaded instead, so labels are
  never wiped by a partial event.
- An expired history cursor returns `resync_required`. The next run rebuilds the selected
  range and then checks older cached IDs in small reconciliation batches. `status` shows
  whether that is still pending.
- Read requests wait and retry on Gmail's per-minute rate limit (15 s, 30 s, ...) and retry
  a couple of times on 5xx. Writes are never retried automatically, because Gmail may
  already have applied them.

The query only picks the initial range. Later history sync also sees changes to messages
outside that range. Changing the query starts a new backfill but keeps what is already
cached; it is not a privacy filter. One cache belongs to one mailbox. Syncing a different
account into it is refused; use a separate `MONASH_KIT_HOME` for a second mailbox.

While the MCP server runs (the local one inside your AI app, or `monash remote serve`), a
background thread syncs mail every `gmail_sync_minutes` (30 by default, 0 turns it off). It
keeps the query from the last run, so a custom `--query` is not reset. Each round handles
at most three pages; while the first download is still going it comes back about a minute
later instead of waiting the full interval. After a complete round it also indexes the text
of up to five attachments. An expired login is written to `mcp.log` once, not every round.
`monash gmail sync` still works for an immediate update.

## Search results and mail to-do

`Store.search` (offline, plain keywords, never FTS syntax) and `live_search` return the same
summary per message: subject, sender, date, `unread`, `in_inbox`, `starred`, `important`,
Gmail category, attachment count, a short snippet and a link back to Gmail. That is enough
for an AI app to decide what needs attention without opening every thread.

`Store.inbox_todo(days, limit)` is the "anything I need to deal with" view: unread mail
still in the inbox from the last few days, with promotions and social updates left out. It
reads only the cache and reports `as_of`, the time of the last successful sync.

## Mailbox actions

With the `gmail.modify` authorization (the default of `monash gmail login`) the code can
create drafts, send an existing draft and change labels, which also covers archive, mark as
read and star. Labels can be changed on one message or a whole thread, and can be given by
name or by a common alias (星标, 未读, 收件箱, 重要). Nothing permanently deletes mail.

There are two ways to run an action, and both start with a preview.

**CLI.** `monash gmail draft`, `send-draft` and `labels` print what would happen and change
nothing. Adding `--confirm` runs the command with its current inputs. This is a manual
confirmation by the person at the terminal, not a stored review ticket.

**MCP.** `gmail_prepare_action` builds the full preview (account, recipients, Bcc, body,
attachments or label changes) and stores it as a ticket bound to a SHA-256 digest of that
exact payload. `execute_student_action` needs the ticket ID, the digest and
`user_confirmed: true`. A ticket:

- expires after 10 minutes and runs at most once; calling it again returns the stored
  result instead of acting twice;
- is claimed before contacting Gmail, so two concurrent calls cannot both send;
- checks that the mailbox is still the one that was previewed, and for `send_draft` that
  the draft has not changed since the preview;
- ends as `done`, `rejected` (a check failed before anything was sent, so it is safely not
  done) or `unknown` (an error during the write; check the mailbox before trying again).

Finished tickets and background jobs are deleted after 30 days. Tickets left as
`executing` or `unknown` are kept, because they record a write whose outcome still needs
checking. Attendance submission uses the same tickets.

## What the local and remote MCP expose

The local MCP registers these only when the matching setting is on. Nothing is on before
the student logs in. `monash gmail login` turns on both Gmail settings (only the first with
`--read-only`), and `monash attendance login` turns on both attendance settings, so everything
works right after login. Each one can be turned off with `monash config`. The write tools
still need a preview and the user's confirmation every time; see
[ADR 0004](adr/0004-confirmed-mail-and-attendance-actions.md).

| Setting | Tools added |
|---|---|
| `gmail_enabled` | `get_gmail_cache_status`, `get_gmail_inbox_todo`, `search_gmail_cache`, `read_gmail_cached_thread`, `gmail_read_attachment` |
| `gmail_manage_enabled` (with `gmail_enabled`) | `gmail_prepare_action`, `execute_student_action` |
| `attendance_enabled` | `attendance_status`, `attendance_sync`, `attendance_overview`, `attendance_find_codes`, `attendance_export`, `student_job_status` |
| `attendance_submit_enabled` (with `attendance_enabled`) | `attendance_prepare_submit`, `execute_student_action` |

The four Gmail cache tools only read the local cache and need neither the Google libraries
nor a valid token; `gmail_read_attachment` downloads the attachment first if it is not cached.
Attendance sync, code lookup and export open a headless browser or read many files, so they
run as background jobs and return a `job_id` for `student_job_status`.
`gmail_prepare_action` checks that the saved Google authorization actually has write access
before building a preview. Without that check a read-only login would only fail at execute
time, after the ticket was claimed, and leave it as `unknown`. When mail tools are on, the server instructions tell the AI app to use them
for questions about "my email" instead of a general Gmail connector the user may also have.
The scopes passed to `student_services` come from these settings, so turning a switch off
also blocks executing a ticket prepared before.

The remote transport (`monash remote serve`) never lists these local tool names. It uses the
catalog from `student_services` instead (`gmail_search`, `gmail_thread`, `gmail_sync`,
`gmail_read_attachment`, `attendance_overview`, `attendance_find_codes` and so on, plus
`gmail_inbox_todo` and `student_job_status`), and only for apps that were granted them:

- The pairing page shows one checkbox per feature the server owner has turned on with the
  settings above. Read-only features start ticked, the two write features start unticked.
  Nothing the server has turned off can be granted, even by editing the form.
- The choice is stored with the authorization code and the tokens and survives refresh.
  `monash remote clients` prints each app's scopes.
- At every call the app's scopes are intersected with the current server settings, so
  turning a setting off on the server takes it away from every app at once.
- Apps paired before this change, or paired with nothing ticked, keep `monash` only.
  To widen an app's access, pair it again; to narrow it, `monash remote revoke --client ID`
  and pair again.
- Review tickets and background jobs are stored under the app's client ID, so one app
  cannot execute or read another app's ticket or job.

## Public project direction

The same core modules are meant for local and self-hosted use. Each student runs their own
instance with their own Google Cloud project, credentials and data. That is different from
one shared hosted service for everyone, which would need multi-user isolation and Google's
verification for restricted scopes; this project does not provide it.

Monash's Google Workspace may still block third-party apps for some accounts. Test with
the real school account before relying on long-lived access. The setup guide covers moving
the OAuth app out of Testing, where Gmail refresh tokens stop working after seven days.

References:

- https://developers.google.com/workspace/gmail/api/auth/scopes
- https://developers.google.com/identity/protocols/oauth2
- https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification
- https://developers.google.com/workspace/gmail/api/guides/sync
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list
