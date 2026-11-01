# Security verification scope for v1.5 features

**Status: checklist, not an audit sign-off.** Created on 2026-11-01 for the public v1.5 feature set. The original [v1.1 review](security.md) documents previous findings and tests only; its GO result is not a blanket verdict for v1.5. No new penetration test or production verification is claimed here.

## Boundary and threat model

- **Local by default:** Moodle, Ed, optional Gmail and attendance credentials live on the instance owner's machine. The chosen AI provider receives tool result content that a client requests, so privacy is not equivalent to offline processing.
- **Remote MCP:** An instance owner can expose their own endpoint over HTTPS and pair clients with OAuth. The maintainer does not operate a shared relay. Network exposure, trusted proxies, callbacks and per-client grants matter.
- **Read-only:** Moodle and Ed query tools must not submit assignments, attempt quizzes or post messages. The optional Gmail and attendance modules can perform state-changing operations only after a full preview and an explicit user confirmation.
- **Permission scope:** Older client pairings must not silently gain Gmail or attendance access when the server is upgraded.
- **Attendance ethics:** Code discovery and the check-in mechanism must not imply that mere possession of a code proves attendance; students should check in only for classes actually attended.

## Verification checklist

- [ ] Re-run v1.1 tests with current code and update stale file/line references; mark any changed behaviour explicitly
- [ ] Inspect OAuth client registration, PKCE, redirect URI allowlist, state, replay resistance and code expiry with negative tests
- [ ] Test remote reverse-proxy exposure: no credential, library, diagnostics, setup or raw files accessible from the public endpoint
- [ ] Check each remote client's scopes independently; legacy grants remain restricted and revocation applies immediately
- [ ] Test expiry, replay, concurrent execution, tampering and cross-client execution of prepare/execute tickets
- [ ] Verify the action preview includes the exact email recipients (including Bcc), attachments and body, or the attendance class and code
- [ ] Verify actions are never executed by tool orchestration solely from AI-generated `user_confirmed=true` without a distinct, informed user confirmation in the client workflow
- [ ] Verify unknown send outcomes are not retried blindly; inspect provider-side state before a new attempt
- [ ] Check Gmail OAuth minimum privilege, read-only option, stored tokens, revocation and consent-screen wording
- [ ] Check Attendance browser/HTTP reader parity against current portal markup without leaking student records into logs
- [ ] Test sensitive data scrubbing in doctor output and exceptions, especially with newly added Google/OAuth/attendance tokens
- [ ] Review Python dependencies, bundled assets, release workflow and Google OAuth client configuration for exposure
- [ ] Verify current docs explain that connecting to a cloud AI client sends queried tool results to that client
- [ ] Record tests, commit/version, environment and residual findings before publishing a new overall security verdict

## Existing design evidence, not a substitute for verification

- [ADR 0002](adr/0002-read-only-public-package.md): Moodle/Ed read-only boundary
- [ADR 0004](adr/0004-confirmed-mail-and-attendance-actions.md): two-step confirmed actions
- [Privacy policy](../PRIVACY.md): data sources, storage, AI-client transfer and deletion
- [Remote MCP notes](panels-library-remote.md): optional remote architecture
- [Google and attendance setup](google-and-attendance-setup.md): optional service configuration

Do not check off a test without recording actual test evidence. Keep passwords, session cookies, API tokens, real emails and personal attendance records out of public issues and screenshots.
