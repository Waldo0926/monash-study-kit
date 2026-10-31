# 0004. Mail and attendance actions run after confirmation

Status: accepted

## Context

[ADR 0002](0002-read-only-public-package.md) keeps everything that touches Moodle and Ed read-only. The optional Gmail and Attendance parts add a few actions that students actually want help with: drafting a reply, sending a draft they wrote, archiving or marking mail read, and checking in to a class they attended. Asking students to turn each one on by hand after logging in made the features look broken, since the AI would say it cannot do something the student just set up.

The risk from ADR 0002 still applies: the one choosing to call a tool is a language model, and sending mail or submitting a check-in cannot be taken back.

## Decision

After `monash gmail login` or `monash attendance login` the actions are on in the local MCP. What keeps them safe is not a switch but the flow every action goes through:

- A `prepare` tool builds the full preview (recipients, Bcc, body, attachments, label changes, or the class and code) and stores it as a ticket bound to a hash of that exact payload. Nothing is sent at this step.
- `execute_student_action` needs the ticket, the hash and `user_confirmed: true`. The server instructions tell the AI to show the preview and never confirm on the user's behalf.
- A ticket runs once and expires after 10 minutes. A draft that changed after the preview is refused. A failed check before sending is recorded as rejected; an error during sending is recorded as unknown and is not retried.
- Check-in is prepared only when the student says they actually attended, never for a future class or one already checked in.
- Nothing permanently deletes mail.

Students who want less can log in with `--read-only` or turn off `gmail_manage_enabled` and `attendance_submit_enabled`. Remote apps get these actions only if the student ticks them on the pairing page, and they start unticked.

## Consequences

- Moodle and Ed stay read-only as in ADR 0002. This decision covers only Gmail and the attendance portal.
- The Google consent page asks for `gmail.modify` by default, which reads as "view and modify". The privacy policy says so.
- New write tools must use the same prepare and execute flow. A tool that writes in one step is not accepted.
