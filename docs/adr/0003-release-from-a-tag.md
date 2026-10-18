# 0003. Release to PyPI from a tag with Trusted Publishing

Status: accepted, 1.1.0

## Context

Up to 1.0.3, installing meant `uv tool install <zip of main>`. That had a few problems:

- whatever was on `main` at that moment got installed;
- there was no fixed version to pin, which the server tools need after [0001](0001-one-shared-core.md);
- GitHub releases stopped at v1.0.0 while the code was at 1.0.3, because making them was a manual step.

Nothing checked that the built package worked, only that the tests passed inside the repo.

## Decision

- Releases go to PyPI, and install is `uv tool install monash-study-kit`.
- A release is made by pushing a `v<version>` tag. `release.yml` checks the tag, `__version__` and the changelog agree and that the commit is on `main`, runs the full tests, builds, uploads with PyPI Trusted Publishing, installs the release back from PyPI on three OSes, and creates the GitHub release.
- Trusted Publishing means there is no PyPI token stored in the repo or in GitHub secrets.
- Every PR builds the wheel, checks its contents and runs it from a clean venv (`scripts/check_dist.py`, `scripts/smoke_test.py`).

## Consequences

- Each version on PyPI matches a tag and a changelog section, and has been installed and started on all three OSes.
- A module left out of the package fails CI instead of failing for users.
- PyPI never accepts the same version twice, so a broken release is fixed by releasing the next patch version.
- `monash update` and the update notice use PyPI first and fall back to GitHub, so people still on a zip install move over by themselves.
