# 0002. The public package is read-only

Status: accepted

## Context

The package acts with the student's own Moodle session and Ed token. Through MCP, the one deciding to call a tool is a language model. A tool that can submit an assignment, start a quiz attempt or post on Ed could do that because of a misunderstood request, and the result can't be taken back.

My private server version does have write actions (submitting files and quiz answers), used only by me, with confirmation steps I built for myself.

## Decision

Everything in this package only reads from Moodle and Ed. There is no tool or command that submits, posts, replies or changes settings on either site. Credentials never go through a tool argument: Moodle login happens in a browser window the student controls, and the Ed token is typed in the terminal.

Reading still leaves the same traces as clicking a link in the browser. Opening a Moodle resource during sync shows up in Moodle's logs and can tick "viewed" in activity completion.

## Consequences

- The worst a bad tool call can do is read something the student could read anyway.
- Features that need writing (submitting work, posting a question) are out of scope. Feature requests have to confirm this.
- New tools need a read-only check in review.
