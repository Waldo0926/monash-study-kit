---
name: monash-study-kit
description: Read a Monash student's own Moodle and Ed Discussion through Monash Study Kit (the `monash` CLI or its MCP tools) - this week's to-dos, deadlines, possibly missed assignments, grades and feedback, announcements, Ed posts and replies, Ed lessons, and full-text search over course files and lecture transcripts with page numbers or timestamps. Use when the student asks about their Monash units, assignments, due dates, course materials or Ed. Read-only - it never submits work, answers quizzes or posts.
---

# Monash Study Kit

Study Kit keeps a local copy of the student's Moodle and Ed and answers questions from it. Source and docs: https://github.com/Waldo0926/monash-study-kit.

## Which way to call it

- If tools from the `monash` MCP server are available (`get_study_todo`, `search_content`, `read_ed_thread` ...), use them. They read the same local data and their descriptions say which one fits.
- Otherwise run the `monash` CLI. Add `--json` when you parse the output. Results go to stdout, hints and errors to stderr.
- If `monash` is not installed, point the student to https://docs.secureview.tech/study-kit/ and stop. Do not install it without asking.

Talk to the student in the language they use with you. Command output is in Chinese.

## Commands

| The student asks | Run |
| --- | --- |
| What do I need to do this week | `monash todo` |
| When is something due | `monash due` or `monash due FIT2102` |
| Did I miss a submission | `monash moodle assignments --missing` |
| Grades and feedback | `monash moodle grades FIT2102` |
| Moodle announcements | `monash moodle news FIT2102` |
| Where a topic is covered | `monash grep "monad" FIT2102` |
| A unit's files for one week | `monash moodle ls FIT2109 5` |
| What's new on Ed | `monash ed new` |
| Did anyone reply to my posts | `monash ed following` |
| Search Ed | `monash ed search FIT2102 lodash` |
| Read one Ed post with all replies | `monash ed show FIT2102#42` |
| Read an Ed lesson | `monash ed read FIT2109 "W3 Pre-Class"` |
| Revise with Ed lesson quiz questions | `monash ed quiz FIT2109 --module "Week 3"` |
| Something doesn't work | `monash doctor` |
| Anything in my school email to deal with (optional, only if set up) | `monash gmail todo` |
| Find a school email (optional) | `monash gmail search exchange` |
| Which classes I haven't checked in to (optional) | `monash attendance overview` |
| Attendance code for a class (optional) | `monash attendance codes` |

The Gmail and attendance rows only work if the student set them up; `monash help` lists them once they are on. If not, the command says what to install or log in to: pass that on and stop. School mail lives in this tool, so for "my email" questions prefer it over a general Gmail connector unless the student means another mailbox. Attendance codes from `monash attendance codes` are candidates found in course posts and mail: show where each came from and let the student check.

`monash grep` searches slides, PDFs, course web pages, Ed lessons and lecture transcripts (`.transcript.md`, with timestamps). Each hit has a file path under the student's Study Kit folder. Read that file to quote the original. No hit can mean the data is not synced yet: suggest `monash sync`. `monash help` lists every feature with an example question.

## Questions about an assignment

When the student asks what an assignment wants, how it is marked, or its format or deadline:

1. **The brief and the marking rubric come first.** Find them with `monash grep` or `monash moodle ls` and read the original text.
2. **Ed adds to the brief.** Count only replies from teaching staff (marked 教师 or 助教 in the output) and answers they endorsed (✅). Other students' replies are leads, not rules.
3. **A later staff correction wins.** But when the brief and Ed disagree, list both with their dates, say which is newer, and suggest the student confirm with staff. Do not decide for them.
4. **Give a source for every rule:** file name and page, or post number, who said it and the date. If you cannot find a source, say so. Do not fill the gap yourself.

Help the student understand the requirements. The work they hand in has to be their own, and unit rules on AI use apply.

## Things only the student does

- **Logging in.** Exit code 2 means a login is needed. Moodle: ask the student to run `monash login` (it opens a browser window for Okta and MFA). Ed: `monash login ed` in their own terminal. Never ask for, accept or repeat an Ed token, password or MFA code. If the student pastes a token into the chat, tell them to delete it at https://edstem.org/au/settings/api-tokens and make a new one.
- **Submitting, quizzes and posting.** Study Kit cannot do these, and you should not try through other means. They do it on the website.
- **Changing the mailbox.** `monash gmail draft`, `send-draft` and `labels` only preview by default. Show the student the whole preview. Add `--confirm` only after they confirm that exact action, and never retry a write that failed without checking the mailbox first. Attendance check-in is only done through the MCP tools, after the student says they actually attended and confirms the preview.
- **Gmail and attendance logins.** `monash gmail login` and `monash attendance login` open a browser for the student. Never read or print the OAuth client JSON or token files.
- **Setup and settings.** Do not run `monash setup`, `monash courses`, `monash config` or `monash uninstall` yourself; they are interactive or change the student's setup. Never print anything from the `secrets` folder in the Study Kit data folder.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | OK |
| 1 | Bad arguments (see stderr) |
| 2 | Login needed: `monash login` (Moodle) or `monash login ed` |
| 3 | Could not reach Moodle or Ed, or it returned an error; try again later |
| 4 | Unit or post not found |
| 130 | Interrupted |
