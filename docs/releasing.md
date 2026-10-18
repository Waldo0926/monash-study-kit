# Releasing

Releases go to [PyPI](https://pypi.org/project/monash-study-kit/) and GitHub Releases from one tag push. Nothing is uploaded by hand and no PyPI password or token is stored anywhere: the upload uses [Trusted Publishing](https://docs.pypi.org/trusted-publishers/), where PyPI trusts this repository's `release.yml` running in the `pypi` environment.

## Every release

1. In the PR, bump `__version__` in `src/monash_study_kit/__init__.py` and add a `## <version>` section to `CHANGELOG.md`. CI checks the bump and, on every OS, builds the wheel and runs it from a clean venv.
2. Merge the PR into `main`.
3. Tag the merge commit and push the tag:

   ```bash
   git checkout main && git pull
   git tag v1.1.0
   git push origin v1.1.0
   ```

4. Watch the `release` workflow. It stops before uploading anything if:
   - the tag and `__version__` differ, or the changelog has no section for this version;
   - the tagged commit is not on `main`;
   - any test, the package content check or the clean-venv smoke test fails.

   After uploading it installs `monash-study-kit==<version>` from PyPI on macOS, Windows and Linux and runs the smoke test again, then creates the GitHub release with that version's changelog section and the built files attached.

Users on an older version see the update notice within a day and run `monash update`.

## If something goes wrong

- **Failed before `publish`**: nothing was uploaded. Fix it on `main`, then move the tag: `git tag -f v1.1.0 <commit> && git push -f origin v1.1.0`.
- **Failed after `publish`**: PyPI never accepts the same version twice. Fix it, bump to the next patch version and release that. If the uploaded version is broken, yank it on PyPI (Manage → Releases → Yank) so `uv tool install` skips it.

## One-time setup

1. On PyPI, open *Your account → Publishing* and add a **pending publisher**:
   - PyPI project name: `monash-study-kit`
   - Owner: `Waldo0926`
   - Repository: `monash-study-kit`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. The `pypi` environment is created on GitHub the first time the workflow runs. To require a click before each upload, add yourself as a required reviewer under *Settings → Environments → pypi*.

## What the scripts check

| Script | Runs in | Checks |
| --- | --- | --- |
| `scripts/check_dist.py dist <version>` | every PR, release | one wheel and one sdist with the right version; every module in `src/` is in the wheel; no `__pycache__`, tests, databases, cookies, tokens or `.env` files; the `monash` entry point and README are in the metadata |
| `scripts/smoke_test.py <monash> <version>` | every PR (built wheel), release (wheel from PyPI) | `monash --version` and `monash help` work; `monash mcp` answers `initialize`, `tools/list` and `prompts/list`, all 17 tools are listed with a description and input schema, and notifications get no reply. It uses an empty temporary data folder, so it needs no login and no network. |
