# Study panels, local library and optional remote MCP (1.4.0)

All additions operate on the owner's own data. The default remains a local stdio
server. Installing or updating the package does not start a web server, register
a remote endpoint, change client settings or install a system service.

## Three study panels

In an MCP Apps-capable host, `get_study_todo` can show deadline cards and unfinished
Lessons. `get_ed_lessons` can show module progress or quiz practice. The panels use
the same tools and arguments as before; the original text result remains available
for hosts without MCP Apps. No extra panel tools are registered.

- Ask “What do I need to do this week?” for the todo panel.
- Ask “Show my FIT2109 Lesson progress” for progress grouped by module.
- Ask “Help me practice FIT2109's Week 3 quiz” for questions and choices. The AI can
  call `get_ed_lessons` with `course`, `quiz=true`, and optionally `module`. With
  `lesson` plus `quiz=true`, the original lesson text is retained and the attached
  panel selects the matching lesson's questions.

The quiz panel keeps choices in memory. Its explanation button sends your answers
back into the conversation; it does not mark a Lesson complete or submit an Ed
attempt. Ed does not provide student-visible answer keys, so the AI must read the
Lesson materials and explain uncertainty. Hosts control whether panels, messages
and external links are available. A successful protocol test is not a guarantee
that every client renders MCP Apps.

`search_moodle_activities` adds name-based section/activity lookup to MCP, using the
same backend as `monash moodle find`. It searches names, while `search_content`
searches downloaded file text. The local catalog is now 18 tools; all 17 existing
tool names, argument schemas, annotations and descriptions remain compatible.

## Browse synced files without an AI

```sh
monash library --open
# Or choose a free local port:
monash library --port 8766
```

Open the printed `http://127.0.0.1:PORT/` address. Filter by course and filename or
week. The page reads existing Moodle file records and Ed Lesson file records;
`monash sync` remains a separate command. Click a file to download its original
bytes. Empty data gets a first-sync message. Listings are paginated at 200 files.

The server only binds on IPv4 loopback. It has no login, upload, sync or submission
route. Host/Origin checks reject DNS rebinding and cross-origin requests; course
text is HTML-escaped and downloaded files are attachments with `nosniff`.
Unregistered, removed, missing or out-of-root files are refused, including symlinks
escaping the allowed folders. It does not serve your secrets or browser profile.
Local programs on the same machine can still access loopback; do not proxy this
unauthenticated local library onto a public interface. Stop it with Ctrl-C.

## Optional single-user remote MCP

This mode is for owners who want to host **their own** data. It is not a shared
student hosting service and does not provide a public server maintained by the
author. Use a separate OS account and data directory for each owner.

1. Install and set up Study Kit under the intended account. Complete Moodle login
   and enter the Ed token on that machine's own terminal. Use `MONASH_KIT_HOME` if
   you need a separate data home; use the same value for serving and pairing.
2. Provide a domain with HTTPS and a reverse proxy to the loopback port. Then run:

   ```sh
   monash remote serve --public-url https://study.example.org --port 8765
   ```

   `MONASH_KIT_REMOTE_URL` is an alternative to `--public-url`. The server only
   binds to `127.0.0.1`; it does not configure nginx, DNS, certificates or services.
   Normal background sync/keepalive runs while this command runs. Add
   `--no-background` if another process already schedules those jobs.
3. In another terminal under the **same account/data home**, run:

   ```sh
   monash remote pair
   ```

   Enter that one-time code on the authorization page opened by your AI client.
   The code expires in ten minutes, permits one authorization and locks after
   five wrong attempts. Never paste Moodle Cookies or Ed tokens into a chat.
4. Configure a remote-capable client's endpoint as
   `https://study.example.org/mcp`, with OAuth. Supported default callbacks are
   the official Claude and ChatGPT HTTPS callbacks. Each client pairs separately.
   A different client's exact callback may be opted in with repeated
   `--redirect-uri` flags (HTTPS, or HTTP on localhost with an explicit port);
   wildcards and arbitrary port patterns are not accepted. Use the callback
   actually reported by that client, not an invented example.
5. Inspect or revoke authorization locally:

   ```sh
   monash remote clients
   monash remote revoke --client CLIENT_ID
   monash remote revoke --all
   ```

Discovery metadata lives at `/.well-known/oauth-protected-resource` and
`/.well-known/oauth-authorization-server`. OAuth routes are `/oauth/register`,
`/oauth/authorize` and `/oauth/token`. The base scope is `monash` (Moodle and Ed).
Optional Gmail and Attendance scopes are chosen per app on the pairing page; see
[gmail-foundation.md](gmail-foundation.md#what-the-local-and-remote-mcp-expose). Tokens are
bound to the MCP resource URL; access tokens last one hour and refresh tokens last
30 days and rotate on use. Only hashes are kept in the private
`secrets/remote-mcp.db` (0700 directory, 0600 file on Unix). Changing the public
origin changes the audience and requires fresh authorization. Windows users must
also restrict the account's data-home ACLs.

The base remote catalog contains 16 read-only query tools. Mail and attendance tools are
added only for apps that were granted them. It omits local browser login
and manual sync triggers and rejects their legacy aliases too. It shares study
panel resources, tool text and prompts with stdio. There is no credential-setting,
assignment submission, quiz submission, posting or public file-download route.

### Reverse-proxy example (adapt before using)

This is a configuration example only; the package never installs or runs it. On
an existing HTTPS virtual host, forward only MCP/OAuth paths, preserve Host and
Authorization, cap bodies, and add your normal proxy rate/time limits:

```nginx
location ~ ^/(mcp|oauth/(register|authorize|token)|\.well-known/oauth-(protected-resource|authorization-server)(/mcp)?)$ {
    client_max_body_size 1m;
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-Proto https;
}
```

Use the actual public authority in Host, including a non-default port if used.
Do not expose `monash library` or the Study Kit data directory through that proxy.

## Verification and release boundary

Automated tests use temporary Moodle/Ed databases, synthetic course content and
loopback HTTP. They cover unchanged legacy tool contracts, three panel payloads,
file traversal/removed-file checks, origin checks, Claude/ChatGPT OAuth flows,
PKCE, token expiry/rotation, revocation and refusal of local-maintenance tools.
Wheel smoke reads the packaged panel resource and initializes the actual CLI.

After a separately authorized self-hosted deployment, verify HTTPS discovery,
unauthenticated MCP 401, initialize, query-tool catalog (16), a safe course query,
normal client refresh and a second query. Check panel rendering in each supported
host. No live private service or university data is changed by this feature PR.
Merging does not publish PyPI: follow the repository's normal reviewed tag/release
process before users install 1.4.0.
