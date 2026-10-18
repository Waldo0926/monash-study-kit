# Security policy

## Reporting a problem

Please report security problems privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**. Don't open a public issue, and don't paste cookies, tokens, calendar links or screenshots of your Moodle pages into the report.

I'm a student maintaining this in my own time. I'll reply within a week, and fix confirmed problems in a patch release as soon as I can.

## Supported versions

Only the latest release on [PyPI](https://pypi.org/project/monash-study-kit/) gets fixes. Update with `monash update`.

## What counts

Anything that could leak or misuse what the tool stores or reads, for example:

- the Moodle session cookie, the Ed token or the calendar link being sent anywhere other than Moodle or Ed, written somewhere other users can read, or printed in output (`monash doctor` output included);
- a tool reading files outside its two course-file folders;
- a way for a tool call to change anything on Moodle or Ed (every tool is meant to be read-only).

How the tool protects these, with the code and tests behind each point, is in [docs/security.md](docs/security.md).
