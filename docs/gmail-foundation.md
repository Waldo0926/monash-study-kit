# Gmail mirror and management foundation (development branch)

This is the shared development implementation for the unified student MCP.
It is not a public release and has not been tested against a real Monash mailbox.
The private moodle-sync deployment consumes a snapshot of the shared modules and
has its own tested remote grants, Attendance tools and scheduler. The public
transport has not yet been migrated to those capabilities.

## Reading is compatible with downloading

The default **sync** authorization uses `gmail.readonly`: it permits downloading
messages and attachments while leaving the mailbox unchanged, including unread
status. Bodies, original Gmail `format=full` JSON payloads, labels and attachment
metadata are stored in a private SQLite database with an FTS5 full-text index.
This is persistent storage, not a browser session that has to keep Gmail open.
Message payload JSON is not a byte-for-byte RFC 822 `.eml` export.

Attachment bytes can be downloaded on demand into the private cache. The remote
filename is metadata; disk filenames are SHA-256 hashes to avoid path traversal.
The index covers message bodies, subjects, senders and attachment text after
extraction. `read_attachment` supports PDF, Office, text and image OCR; the private
deployment downloads/indexes bounded batches automatically. Retention controls
remain to be completed. The cache is private filesystem storage, not application-level
encryption, and is never added to the course-material web directory.

The optional **manage** authorization uses `gmail.modify`. It enables creating
drafts, sending drafts and changing labels (including archive or mark-read
operations). These CLI commands preview by default and mutate only with
`--confirm`; they are not exposed as MCP write tools in this slice. No operation
permanently deletes messages. API writes are never automatically retried.

## Setup and commands

Install the development checkout with its Gmail extra:

```sh
python -m pip install -e '.[gmail]'
monash gmail login --client /private/path/desktop-client.json
# Optional later upgrade of authorization:
monash gmail login --client /private/path/desktop-client.json --manage

monash gmail sync
monash gmail status
monash gmail search 'exchange application'
monash gmail thread THREAD_ID
monash gmail attachment MESSAGE_ID PART_ID
```

Use a Google Cloud **Desktop app** OAuth client with Gmail API enabled. The user
signs in and grants access in their own system browser. The loopback callback
never requires pasting a password or token into an AI chat. Tokens live under
`MONASH_KIT_SECRETS`; message data lives under `MONASH_KIT_DATA/gmail`.
For VPS use, a separate SSH-based token transfer/setup workflow is still needed.
Do not place the OAuth client JSON or token JSON in the repository.

The initial download defaults to `newer_than:90d`; `--query ''` requests all
available history. `--pages` bounds each run, and repeated invocations resume.
The query selects the **initial/backfill** range; subsequent history sync observes
mailbox changes, including changes to messages outside that initial range.
Changing the initial query starts a new backfill but retains previously cached
messages; it is not a cache deletion or ongoing privacy filter.

Once backfill completes, sync uses `historyId`. Expired history returns
`resync_required`; the next run rebuilds the selected range, then verifies older
cached message IDs in bounded reconciliation batches. The status reports whether
that reconciliation remains pending. A previously
downloaded attachment is retained as a local copy even if the source is deleted.

Each transaction commits both message data and its cursor. Failed fetches roll
back that invocation. One cache belongs to one mailbox; account switching needs
a separate `MONASH_KIT_HOME`. Cached thread reads explicitly report that older
or newer messages may be outside the mirror's coverage.

Optional management examples (the first invocation only previews):

```sh
monash gmail draft --file /private/path/message.eml
monash gmail draft --file /private/path/message.eml --confirm
monash gmail send-draft DRAFT_ID
monash gmail send-draft DRAFT_ID --confirm
monash gmail labels MESSAGE_ID --remove INBOX
monash gmail labels MESSAGE_ID --remove INBOX --confirm
```

Review recipients, Bcc, body and attachments before confirming. CLI confirmation
applies to the command's current inputs; it is not a server-bound review token.
The future MCP write flow must bind confirmation to the exact reviewed payload,
handle concurrent edits and reconcile ambiguous results before retrying.

## MCP and compatibility

Gmail login opts the local MCP into three cache tools:

- `get_gmail_cache_status`
- `search_gmail_cache`
- `read_gmail_cached_thread`

`monash config gmail_enabled true` can enable these for an already populated
offline cache. They do not require Google libraries or a valid token to read.
Sync is explicit via CLI in this slice; no automatic Gmail scheduler has been
added yet. Existing Moodle/Ed background jobs remain independent.

Existing public remote `monash` grants must not silently gain mailbox access.
The remote transport excludes these tools **and rejects direct calls** until
per-client Gmail capabilities are implemented. The private moodle-sync server
has its own MCP implementation and is not changed by this development checkout.

## Public project direction

Share the same core modules between local and VPS deployments. A public source
release should let each student run their own instance, with their own data and
credentials. This is different from operating one shared hosted service for
everyone. The latter additionally needs multi-user isolation and a separate
OAuth verification/hosting design; this branch does not provide it.

Google's personal-use verification exception does not guarantee that Monash
Workspace will permit an app. External OAuth applications in Testing generally
get seven-day refresh tokens for Gmail scopes. Determine the appropriate Google
publishing/verification setup and test the actual school account before claiming
long-lived production access.

References:

- https://developers.google.com/workspace/gmail/api/auth/scopes
- https://developers.google.com/identity/protocols/oauth2
- https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification
- https://developers.google.com/workspace/gmail/api/guides/sync
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list
