# Contributing

Thanks for helping. Bug reports from other Monash students are the most useful thing, since every unit sets up Moodle a bit differently.

## Reporting a bug

Use the bug report form. Run `monash doctor` (or `monash doctor --deep` if something looks wrong in the results) and paste the output. It leaves out cookies, tokens and calendar links. Please don't paste unit materials, other students' posts or your grades.

## Setting up

```bash
git clone https://github.com/Waldo0926/monash-study-kit
cd monash-study-kit
uv sync
uv run pytest -q
uvx ruff@0.6.9 check src tests scripts
uv run monash --help
```

`uv run monash ...` uses your real data folder. To try things without touching it, set `MONASH_KIT_HOME` to an empty folder first.

## Making a change

- Keep it read-only. Tools must not submit, post or change anything on Moodle or Ed.
- Add a test for the bug or feature. Tests never touch the network; save a trimmed HTML or JSON sample in the test instead.
- Test samples must not contain real names, student ids, emails, cookies or tokens. Replace them with made-up values.
- If you change anything under `src/`, bump `__version__` in `src/monash_study_kit/__init__.py` (last number for a fix, middle number for a feature) and add a line to `CHANGELOG.md`. CI checks this.
- Code comments and CLI messages are in Chinese, docs are in English with a Chinese README. Follow whatever the file you're editing already does.
- Bigger design choices go in [docs/adr](docs/adr) as a short record.

CI runs the tests on macOS, Windows and Linux with Python 3.10 and 3.13, runs ruff, and installs the built package into a clean venv to check that `monash` and the MCP server start.

Releases are made by the maintainer from a tag, see [docs/releasing.md](docs/releasing.md).

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
