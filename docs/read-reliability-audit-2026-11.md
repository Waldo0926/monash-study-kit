# Read reliability audit — November 2026

This audit covers the MCP and HTTP request boundary, grade/submission workflow, attachment parsing and download integrity, materials-only restrictions, local settings/status/update state, dependency advisories, and distribution checks. Existing regression suites also cover Moodle/Ed sync, failed-activity retries, stale indexes, exports, optional services and OS-specific behavior. Passing tests do not establish that external logins remain valid or that every upstream page layout is supported.

## Fixes

- Gradebook assignments expose a submission read path, connecting feedback to the submitted evidence without additional discovery requests.
- Downloads validate declared byte length before replacing a complete cache. Redirected authorities reject embedded credentials and alternate ports; login-page responses become authentication errors.
- Online text is extracted inside the submission region, preferring the expanded content over a collapsed summary.
- Malformed MCP parameters produce protocol errors. Refresh requests cannot silently change scopes or consume the still-valid refresh token when rejected.
- Non-object settings/status JSON and malformed update caches recover without breaking CLI startup. Update-cache writes use unique atomic files.
- Aggregate PowerPoint XML expansion is bounded, in addition to individual-member limits.
- Raise the pypdf safety floor to 6.21.4; refresh the lock to 7.1.0. See the [maintainer's page-tree memory advisory](https://github.com/py-pdf/pypdf/security/advisories/GHSA-cggf-277g-3q54).
- Optional image OCR uses Pillow 13. On Python 3.10, record access remains available but image OCR is blocked before decoding. See [Pillow's decoder advisory](https://github.com/python-pillow/Pillow/security/advisories/GHSA-4558-3h8r-v8xp) and [13.0 release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html).

## Boundaries retained

Submission caches remain outside the course library, index and exports. Materials-only profiles cannot read personal submissions or grades. Moodle and Ed tools remain read-only. Video transcription and scanned-document OCR are not part of the submission reader. Image OCR still requires its optional system tools; a passing unit test is not an authenticated attendance-session test.

## Verification

Run `uv run pytest -q`, `uvx ruff@0.6.9 check src tests`, `uv build`, and `scripts/check_dist.py`. PR checks cover Linux, macOS and Windows on Python 3.10/3.13, plus isolated wheel installations. Known-advisory scanning complements these tests; local source projects without published advisory metadata are outside that scanner's coverage.
