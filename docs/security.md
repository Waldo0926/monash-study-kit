# Security review

Scope: the `monash-study-kit` package in `src/monash_study_kit`, its tests, and the release workflow. The package runs on the student's own computer. It holds three secrets: the Moodle session cookie, the Moodle iCal calendar URL (it has an `authtoken` in it), and the Ed API token. It also stores course files and a local copy of Ed posts.

Every claim below points to the code line that does it and to a test that checks it. The tests in `tests/test_security.py` were written for this review. Line numbers are for version 1.1.0.

Overall verdict: **GO**. No high-risk problem was found. The review found seven smaller gaps (G1 to G7), and all of them were fixed in 1.1.0. See [Found and fixed in 1.1.0](#found-and-fixed-in-110). A few weaker points remain and are listed under [Remaining](#remaining).

## Summary

| Area | Verdict | Evidence |
| --- | --- | --- |
| 1. Moodle cookie only goes to Moodle | GO (G1 fixed) | `moodlelib.py:105-110`, `:145-148`; `test_security.py::test_cookie_goes_to_moodle_but_not_to_the_cloudfront_hop` |
| 2. Login window takes only Moodle cookies | GO (G6 fixed on macOS/Linux) | `browser_login.py:73-82`, `:101-104`, `:125-128`; `test_security.py::test_login_window_uses_its_own_private_profile` |
| 3. Secrets at rest | GO on macOS/Linux, weaker on Windows | `paths.py:96-113`; `test_security.py::test_write_secret_is_owner_only_and_atomic` |
| 4. Ed token handling | GO (G2 fixed) | `cli.py:77`, `edlib.py:19`, `:127-133`; `test_security.py::test_ed_token_is_not_carried_across_a_redirect` |
| 5. MCP server: stdio, read-only | GO, side effects noted | `mcp_server.py:693-713`, `:611-626`; `test_mcp.py::test_tools_cover_both_sides_and_only_login_sync_are_not_read_only` |
| 6. `read_file` stays in the file folders | GO (G7 fixed) | `content_index.py:304-313`, `:179-181`; `test_security.py::test_resolve_readable_rejects_paths_outside_the_file_dirs` |
| 7. Zip bombs and huge files | GO (G3, G4 fixed), PDFs not capped | `content_index.py:29-33`, `mcp_server.py:371-372`, `:393-396`, `textextract.py:68-76`; `test_security.py::test_read_file_refuses_a_zip_member_that_inflates_too_far` |
| 8. `doctor` output scrubbing | GO (G5 fixed) | `doctor.py:25-34`, `:207`, `doctor_deep.py:247`; `test_security.py::test_doctor_scrubs_sesskey_and_crashed_checks` |
| 9. Update check sends no identity | GO | `update_check.py:52-55`; `test_security.py::test_update_check_sends_only_a_versioned_user_agent` |
| 10. Supply chain and release | GO, Actions pinned by commit SHA | `pyproject.toml:22-24`, `release.yml:26-38`, `:64-71`; `test_security.py::test_release_uses_trusted_publishing_without_stored_secrets` |

## 1. The Moodle cookie is only sent to Moodle

**Risk.** Course files are served through a 302 redirect to CloudFront or to other sites (Google Slides, YouTube). If the client followed redirects with the cookie attached, the `MoodleSession` cookie would leak to those hosts. Anyone with that cookie is logged in as the student. A cookie sent over plain HTTP can be read by anyone on the same network.

**What the code does.**
- `moodlelib.py:105-110`: `_NoRedirect` returns `None` from `redirect_request`, so urllib never follows a redirect by itself. Every hop is a separate request made by our code.
- `moodlelib.py:145-148`: the `Cookie` header is added only when the hostname equals `HOST` (`learning.monash.edu`) and the scheme equals `BASE_SCHEME` (`moodlelib.py:28`, the scheme of `BASE_URL`, normally `https`). It is added with `add_unredirected_header`, so even if urllib did follow a redirect, the header would not be copied.
- `moodlelib.py:154-155`: `Set-Cookie` is only taken from responses from `HOST` over the same scheme as `BASE_URL`. An off-site or plain-HTTP response cannot plant a cookie in the jar (`test_security.py::test_set_cookie_over_plain_http_is_ignored`).
- `moodlelib.py:284-300`: `resolve()` stops at the first off-site hop and returns that URL without requesting it.
- `moodlelib.py:302-313`: `download()` follows the CloudFront hop. Because of line 147 that request has no cookie.
- `moodlelib.py:339-348`: `_check_login_url` runs before each hop is followed (`:197`, `:216`, `:247`, `:296`, `:311`). A redirect to `*.okta.com`, `/login/` or `/auth/saml` raises `MoodleAuthError`, so an Okta login page is never saved as a course file.
- Other fetchers do not touch the Moodle jar: iCal (`features.py:166-169`), Ed lesson PDFs (`lessons.py:35`), web notes (`webnotes.py:194`, `:223`) and Zoom recordings, which use their own empty jar (`recordings.py:106-107`).

**Tests.**
- `tests/test_security.py::test_cookie_goes_to_moodle_but_not_to_the_cloudfront_hop`
- `tests/test_security.py::test_cookie_is_not_sent_over_plain_http`
- `tests/test_security.py::test_urllib_never_follows_redirects_by_itself`
- `tests/test_security.py::test_resolve_stops_at_the_first_offsite_hop`
- `tests/test_security.py::test_okta_redirect_fails_before_it_is_followed`
- `tests/test_layout.py::test_login_redirect_is_detected_before_following`

The scheme check was gap G1 and is fixed, for both sending the cookie and accepting `Set-Cookie`.

## 2. The login window only takes Moodle cookies

**Risk.** Reading cookies from the student's normal browser would need the OS keychain key, which unlocks every site's cookies. Taking all cookies from the login window would also store Okta and tracking cookies. If the window's DevTools endpoint can be reached by other programs, they can read every cookie in it.

**What the code does.**
- `browser_login.py:68`, `:75`, `:86`: the browser starts with `--user-data-dir=BROWSER_PROFILE`, a separate profile under the kit's home (`paths.py:90`). The folder is made with mode 0700. The student's everyday profile is never opened.
- `browser_login.py:73-82`: on macOS and Linux the browser is started with `--remote-debugging-pipe`. `PipeCDP` (`cdp.py:127-149`) passes two pipes to the browser as fd 3 and fd 4, so DevTools has no TCP port. Other local programs and other users cannot connect to it.
- `browser_login.py:83-93`: on Windows the browser still uses `--remote-debugging-port=0` (a random port), and the client only connects to `ws://127.0.0.1` (`cdp.py:29`).
- `browser_login.py:101-104` and `:27`: only cookies whose names start with `MoodleSession`, `MOODLEID`, `AWSALB` or `AWSELB` are kept, and only if the domain matches.
- `browser_login.py:125-128`: `_domain_matches` accepts `HOST` itself or a parent domain on a dot boundary. `ash.edu` and `learning.monash.edu.evil.com` do not match.

**Tests.**
- `tests/test_sync_robustness.py::test_cookie_domain_must_match_on_a_dot_boundary`
- `tests/test_security.py::test_login_window_keeps_only_moodle_cookies`
- `tests/test_security.py::test_login_window_uses_its_own_private_profile` (profile flag, pipe flag, no port flag, 0700 folder)
- `tests/test_security.py::test_pipe_cdp_talks_to_a_real_browser_without_opening_a_port` (real headless Chromium; skipped when none is installed)
- `tests/test_cdp.py::test_only_local_connections`

The open DevTools port was gap G6. It is fixed on macOS and Linux. Windows keeps the port, see [Remaining](#remaining).

## 3. Secrets at rest

**Risk.** Another local user, or a backup tool that copies world-readable files, could read the cookie or token. A crash halfway through a write could leave a broken or half-written secret file.

**What the code does.**
- `paths.py:104-113` `write_secret`: makes the parent folder private, opens a temp file with `O_CREAT | O_TRUNC` and mode `0o600`, writes, then `os.replace` puts it in place in one step. The temp name has the process id (`:109`), so two MCP servers (Claude Desktop and Claude Code) do not overwrite each other's temp file.
- `paths.py:96-101` `ensure_private_dir`: `mkdir` plus `chmod 0o700`. Errors from `chmod` are ignored.
- Where the secrets live: `SECRETS_DIR` (`paths.py:83`) holds `moodle-cookie` (`moodlelib.py:29`), `calendar-url` (`features.py:131`) and `ed-token` (`paths.py:89`). All three are written with `write_secret` (`moodlelib.py:100`, `features.py:143`, `edlib.py:51`).
- The kit's home folder, which also holds the databases with classmates' names and private posts, is set to 0700 at every CLI and MCP start (`cli.py:766`, `mcp_server.py:697`).

**Windows caveat.** `chmod` has no real effect on Windows. The files are protected only by the default ACL of `%LOCALAPPDATA%` (`paths.py:29-31`), which is normally private to the user. The code says this in the `write_secret` docstring (`paths.py:105`).

**Tests.**
- `tests/test_security.py::test_write_secret_is_owner_only_and_atomic` (0600 file, 0700 folder, per-process temp name, a leftover 0644 file is replaced)
- `tests/test_security.py::test_all_three_secrets_live_in_the_secrets_dir`
- `tests/test_shared_core.py::test_cookie_save_leaves_no_tmp`

## 4. The Ed token

**Risk.** If the token were passed through a chat or a tool argument, it would end up in the conversation history and in client logs. If Ed requests could go to another host, the token header would go with them.

**What the code does.**
- `cli.py:77`: the token is read with `getpass.getpass`, so it is not echoed. `cli.py:81-89` checks it against Ed before saving.
- No MCP tool has a token, cookie or password parameter (tool table `mcp_server.py:500-538`). The server instructions tell Claude that the token must not be pasted into the chat (`mcp_server.py:43-44`). An `EdAuthError` from any tool returns a message that sends the user to `monash login ed` in the terminal (`mcp_server.py:640-643`).
- `edlib.py:19`: `API_BASE = "https://edstem.org/api"`. Both request functions build the URL as `API_BASE + path` (`edlib.py:136-139`, `:165`). Paths are fixed strings with integer ids.
- `edlib.py:127-133`: `EdClient._request` adds the auth header with `add_unredirected_header`. If Ed ever redirected a request to another host, urllib would not copy the token to it.
- `lessons.py:25-35`: Ed lesson PDFs come from a different host and are downloaded without the token.

**Tests.**
- `tests/test_security.py::test_ed_token_is_read_with_getpass_in_the_terminal`
- `tests/test_security.py::test_no_tool_takes_a_credential_argument`
- `tests/test_security.py::test_ed_requests_only_go_to_the_ed_api`
- `tests/test_security.py::test_ed_token_is_not_carried_across_a_redirect`
- `tests/test_mcp.py::test_auth_errors_tell_claude_what_to_do`
- `tests/test_ed_auth.py::TokenKindTests::test_api_token_uses_bearer`

The redirect case was gap G2 and is fixed.

## 5. MCP server: stdio only, read-only tools

**Risk.** A network listener would let other programs or other machines call the tools with the student's session. A write tool would let a model submit work or post on Ed because it misread a request. The design decision is in [ADR 0002](adr/0002-read-only-public-package.md).

**What the code does.**
- `mcp_server.py:693-713`: `main()` reads JSON-RPC lines from `sys.stdin` and writes replies to `sys.stdout`. No module in the package opens a listening socket. The only `socket` import is the DevTools client in `cdp.py`, which connects out to 127.0.0.1 on Windows.
- `mcp_server.py:611-626`: every tool is published with an `annotations.readOnlyHint`. It is `true` for 12 tools. It is `false` for `start_monash_login` and `sync_monash_data` (`mcp_server.py:505-506`) because they open a local login window or start a local sync. Neither writes to Moodle or Ed.
- `mcp_server.py:681-682`: a tool name that is not in the table is refused. There is no submit, quiz, post or reply tool.
- Moodle AJAX calls used anywhere in the package are all `*_get_*` methods plus `core_session_touch` and `core_session_time_remaining`. The only POSTs are `/calendar/export.php` (`features.py:154`) and Ed's `/renew_token` (`edlib.py:181`). `moodlelib.multipart` (`moodlelib.py:316`) exists but nothing calls it.

**Side effects that remain.** "Read-only" here means no tool submits, posts, replies or changes settings. Reading still leaves the same traces as clicking in a browser:
- Sync opens `mod/resource/view.php?...&redirect=1` and `mod/url/view.php?...&redirect=1` (`syncer.py:216`, `:231`), and folder, assignment, page and book pages (`syncer.py:225`, `:239`). `monash doctor --deep` does the same for up to 10 missing resources per course (`doctor_deep.py:124`). Moodle logs these as views, and activity completion set to "viewed" can be ticked by them.
- Logging in may fetch the calendar URL through `/calendar/export.php` (`browser_login.py:182-190`). That makes Moodle create a calendar token for the account if there was none.
- `core_session_touch` keeps the Moodle session alive every 20 minutes while Claude is open (`paths.py:42`).

**Tests.**
- `tests/test_mcp.py::test_tools_cover_both_sides_and_only_login_sync_are_not_read_only`
- `tests/test_mcp.py::test_unknown_tool_and_method`
- `tests/test_security.py::test_mcp_talks_over_stdin_and_stdout`
- `tests/test_security.py::test_package_opens_no_listening_socket`
- `tests/test_security.py::test_only_read_calls_reach_moodle_and_ed`

## 6. `read_file` only opens files in the two file folders

**Risk.** `read_file` takes a path from the model. Text in a course file or an Ed post could try to make the model read `../../.ssh/id_rsa` or the secrets folder.

**What the code does.**
- `mcp_server.py:365-366` passes the path to `content_index.resolve_readable`.
- `content_index.py:304-313`: the base is `FILES_DIR`, or `ED_FILES_DIR` when the path starts with `ed:`. A leading `/` is stripped (`:310`), so an absolute path is treated as relative to the base. The joined path goes through `Path.resolve()`, which removes `..` and follows symlinks. The result must have the resolved base among its parents and must be a regular file (`:311`). Otherwise it raises `ValueError`.
- So `../x`, `U/../../x`, `ed:../x`, a directory, an empty path and a symlink that points outside are all refused.
- `content_index.py:179-181`, `:189`, `:200`: the indexer applies the same rule. `_inside` skips a symlink whose target is outside the folder, so `search_content` cannot show snippets of it.
- Zip members are only read into memory (`mcp_server.py:393-394`, `content_index.py:100`). Nothing is extracted to disk, so a member called `../../x` cannot write anywhere.

**Tests.**
- `tests/test_mcp.py::test_read_file_stays_inside_files_dir`
- `tests/test_lesson_reader.py::test_read_file_opens_ed_files_but_not_outside`
- `tests/test_security.py::test_resolve_readable_rejects_paths_outside_the_file_dirs`
- `tests/test_security.py::test_resolve_readable_turns_absolute_paths_into_relative_ones`
- `tests/test_security.py::test_resolve_readable_rejects_a_symlink_that_points_outside`
- `tests/test_security.py::test_index_skips_symlinks_that_point_outside`

The indexer following outside symlinks was gap G7 and is fixed.

## 7. Zip bombs and huge files

**Risk.** A small zip, docx or pptx can expand to gigabytes. A huge file can use all memory.

**What the code does.**
- `content_index.py:33`, `:220`: files larger than `MAX_FILE` (30 MB on disk) are skipped by the indexer.
- `content_index.py:29-30`, `:96-99`: inside a zip, the indexer reads only text members with a declared size up to `ZIP_MEMBER_MAX` (1 MB), and at most `ZIP_MEMBERS_MAX` (300) of them. Python's `zipfile` stops reading at the declared size, so a member that lies about its size cannot return more than 1 MB.
- `mcp_server.py:371-372`: `read_file` refuses a text file larger than `MAX_FILE`.
- `mcp_server.py:393-396`: `read_file` reads a zip member with `z.open(inner).read(MAX_FILE + 1)` and refuses it if it is larger than `MAX_FILE` after decompression.
- `textextract.py:68-76`, `:85`, `:88`: docx and pptx XML is read through `_read_member`, which stops at `OFFICE_XML_MAX` (50 MB) and raises `ValueError` beyond that. This covers both `read_file` and the indexer.
- `content_index.py:110-118`, `:230`: broken, encrypted or odd files are skipped, so one bad file does not stop indexing.

**Tests.**
- `tests/test_shared_core.py::test_zip_contents_are_searchable` (a member over `ZIP_MEMBER_MAX` is not indexed)
- `tests/test_security.py::test_zip_indexing_reads_at_most_zip_members_max`
- `tests/test_security.py::test_files_over_max_file_are_not_indexed`
- `tests/test_security.py::test_read_file_refuses_a_zip_member_that_inflates_too_far`
- `tests/test_security.py::test_read_file_refuses_huge_text_and_office_xml`

The missing limits in `read_file` and the office reader were gaps G3 and G4 and are fixed. PDFs have no size check in `read_file`, see [Remaining](#remaining).

## 8. `monash doctor` and `doctor --deep` output

**Risk.** The doctor report is meant to be pasted to a helper. Errors in it could contain a token, a session value or a `sesskey`.

**What the code does.**
- `doctor.py:25`: `SECRET` matches `authtoken=`, `sesskey=`, `MoodleSession=`/`:`, `Bearer `, and `token=`/`:` (which also covers `x-token:`), case-insensitive. `scrub` (`doctor.py:32-34`) replaces the value with `…`.
- `scrub` is used on login and network errors (`doctor.py:81`, `:83`, `:95`), sync errors (`:128`), lines from `mcp.log` (`:181`), errors from a check that crashed (`:207`) and errors from deep checks (`doctor_deep.py:247`).
- `features.py:146-147` `mask_calendar_url` hides the `authtoken` value. `monash moodle calendar` prints the masked URL unless `--show` is given (`cli.py:497`). The MCP status tool only reports whether a calendar URL exists, as a boolean (`mcp_server.py:200`).
- The deep checks print counts, course codes, dates and activity ids only (`doctor_deep.py:11-12`).

**Tests.**
- `tests/test_doctor.py::test_scrub_hides_credentials`
- `tests/test_doctor.py::test_mcp_log_surfaces_recent_errors_scrubbed`
- `tests/test_features.py::test_mask_calendar_url`
- `tests/test_security.py::test_scrub_also_hides_ed_token_headers`
- `tests/test_security.py::test_doctor_deep_errors_are_scrubbed`
- `tests/test_security.py::test_doctor_scrubs_sesskey_and_crashed_checks`
- `tests/test_security.py::test_calendar_url_is_masked_in_cli_and_absent_from_mcp`

Unscrubbed errors from crashed and network checks were gap G5 and are fixed.

## 9. Update check

**Risk.** A version check could become a tracking ping if it sent an id, a cookie or a token.

**What the code does.**
- `update_check.py:52-55`: a GET with one header, `User-Agent: monash-study-kit/<version>`. No cookie, no token, no body. At most 2 MB is read.
- `update_check.py:58-66`: it asks PyPI first (`:23`). If that fails or gives a strange version, it reads `__init__.py` from `raw.githubusercontent.com` (`:24`).
- `update_check.py:39-42` and `:72`: at most once a day. It is off with `MONASH_KIT_NO_UPDATE_CHECK`, `CI`, or `monash config update_check false`.
- PyPI and GitHub still see the IP address and the version. That is all they get.

**Tests.**
- `tests/test_security.py::test_update_check_sends_only_a_versioned_user_agent`
- `tests/test_update_check.py::test_fetch_prefers_pypi_and_falls_back_to_github`
- `tests/test_update_check.py::test_disabled_by_env_or_setting`

## 10. Supply chain and release

**Risk.** Every dependency is code that runs with the student's session. A stolen PyPI token would let someone publish a bad version.

**What the code does.**
- `pyproject.toml:22-24`: one runtime dependency, `pypdf>=4.0`. The large media tools (`faster-whisper`, `yt-dlp`, `deno`) are in the optional `media` extra (`pyproject.toml:26-32`) and are only installed on request.
- `release.yml:64-65`, `:71`: publishing uses PyPI Trusted Publishing (`id-token: write` with `pypa/gh-action-pypi-publish`). No PyPI token is stored. The only other credential is the built-in `github.token` used to create the GitHub release (`release.yml:110`).
- `release.yml:26-38`: the tag must equal `__version__`, `CHANGELOG.md` must have that section, and the tagged commit must be on `main`. Tests run before the build (`release.yml:41-43`).
- `release.yml:52` runs `scripts/check_dist.py`, which fails the build if the wheel or sdist contains caches, tests, databases, cookie or token files (`scripts/check_dist.py:22`).

**Tests.**
- `tests/test_security.py::test_one_runtime_dependency_and_heavy_media_is_optional`
- `tests/test_security.py::test_release_uses_trusted_publishing_without_stored_secrets`
- `tests/test_update_check.py::test_version_regex_matches_the_real_file`

- GitHub Actions are pinned by commit SHA in both workflows (the tag is kept as a comment), so a moved tag can't change what the release job runs. Updating an action means looking up the new SHA by hand.

## Found and fixed in 1.1.0

| # | Gap | What changed | Test (`tests/test_security.py::`) |
| --- | --- | --- | --- |
| G1 | The cookie was sent to `http://learning.monash.edu/...` links in plain text. | The cookie is only added when the scheme equals `BASE_SCHEME` (`moodlelib.py:28`, `:145-148`). | `test_cookie_is_not_sent_over_plain_http` |
| G2 | The Ed token was a normal header, and urllib copies normal headers when it follows a redirect. | `EdClient._request` adds it with `add_unredirected_header`. `get` and `post` both use it (`edlib.py:127-133`, `:139`, `:165`). | `test_ed_token_is_not_carried_across_a_redirect` |
| G3 | `read_file` loaded a whole zip member into memory, so a zip bomb could use gigabytes. | The member is read up to `MAX_FILE + 1` bytes and refused if it is bigger (`mcp_server.py:393-396`). | `test_read_file_refuses_a_zip_member_that_inflates_too_far` |
| G4 | `read_file` read text files whole, and the docx/pptx reader had no limit on the XML. | Text files over `MAX_FILE` are refused (`mcp_server.py:371-372`). Office XML goes through `_read_member` with `OFFICE_XML_MAX` (`textextract.py:68-76`). | `test_read_file_refuses_huge_text_and_office_xml` |
| G5 | Errors from a crashed check and network errors were not scrubbed, and `sesskey=` was not covered. | `SECRET` covers `sesskey=`, and `scrub` is applied at `doctor.py:81`, `:83`, `:95`, `:207`. | `test_doctor_scrubs_sesskey_and_crashed_checks` |
| G6 | During login, Chrome's DevTools port listened on 127.0.0.1 with no authentication. | On macOS and Linux the window uses `--remote-debugging-pipe` through `PipeCDP` (`browser_login.py:73-82`, `cdp.py:127-149`). | `test_login_window_uses_its_own_private_profile`, `test_pipe_cdp_talks_to_a_real_browser_without_opening_a_port` |
| G7 | The indexer followed a symlink that pointed outside the folder, so `search_content` could show it. | `_inside` skips such symlinks in both folders (`content_index.py:179-181`, `:189`, `:200`). | `test_index_skips_symlinks_that_point_outside` |

## Remaining

These are still true after the fixes. None of them is high risk.

- **Windows login uses the DevTools port.** `browser_login.py:83-93` still starts the browser with a random 127.0.0.1 port, because Windows has no way to hand fd 3 and fd 4 to a child process. While the window is open, another local program can connect to it. The port number is written in the profile folder only.
- **Windows secrets rely on folder permissions.** `ensure_private_dir` and the 0600 mode do nothing on Windows (`paths.py:96-101`, `:110`). The files are as private as `%LOCALAPPDATA%`.
- **PDFs have no size check in `read_file`.** This is on purpose: lecture slide decks with images are often over 30 MB and still need to be readable. `mcp_server.py:379-380` passes the PDF to pypdf without checking its size. The indexer only checks the size on disk (`content_index.py:220`). I did not check pypdf's own limits on stream decompression. Course PDFs come from staff, so the risk is low.
- **`PipeCDP` uses `preexec_fn` with `close_fds=False`** (`cdp.py:134-144`). Python documents `preexec_fn` as unsafe when other threads are running, and the MCP server starts login from a background thread. If the child hangs before `exec`, `Browser.getVersion` times out after 30 seconds and the login fails (`browser_login.py:77-81`). Nothing leaks. With `close_fds=False` the browser also inherits any other inheritable fds. Python makes new fds non-inheritable by default, and stdin, stdout and stderr are set to `/dev/null`.
- **Reading leaves traces on Moodle.** Views are logged and "viewed" completion can be ticked (section 5). This is by design, see ADR 0002.
- **Not checked:** whether Ed marks a thread as seen when `read_ed_thread` is called with `live=true`.

## How to re-check

From the repo root:

```sh
# all tests, including the ones written for this review
/tmp/claude-0/msvenv/bin/python -m pytest -q
/tmp/claude-0/msvenv/bin/python -m pytest -q tests/test_security.py

# lint (same as CI)
uvx ruff@0.6.9 check src tests scripts

# no listening sockets, no write calls
grep -nE "http\.server|socketserver|\.listen\(|\.bind\(" src/monash_study_kit/*.py
grep -nE '\.call\(\s*"|\.post\(' src/monash_study_kit/*.py

# where the cookie header and the Ed token header are set
grep -n "add_unredirected_header\|BASE_SCHEME" src/monash_study_kit/*.py
```

The real-Chromium test is skipped when no Chromium is installed. When the code changes, re-run the tests above and re-check the line numbers in this file with `grep -n`.
