# Architecture decision records

Short notes on choices that shape the code, so the reason is still here later. Each one says what the situation was, what was decided, and what that costs.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-one-shared-core.md) | One core package, server deployments install it | Accepted (1.1.0) |
| [0002](0002-read-only-public-package.md) | The public package is read-only (Moodle and Ed; see 0004 for mail and attendance) | Accepted |
| [0003](0003-release-from-a-tag.md) | Release to PyPI from a tag with Trusted Publishing | Accepted (1.1.0) |
| [0004](0004-confirmed-mail-and-attendance-actions.md) | Mail and attendance actions are on after login and run only after confirmation | Accepted (1.5.0) |

New record: copy the layout of an existing one, take the next number, and add it to this table.
