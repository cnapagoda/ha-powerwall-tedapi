# Contributing

Thanks for helping. Bug reports, tests on other Powerwall hardware and pull requests are all welcome.

## Before you start

- **Open an issue first** for anything bigger than a small fix, so we can agree on the approach.
- **Safety comes first.** This integration controls real electrical equipment through interfaces Tesla does not document. A change that writes to the Powerwall needs a clear reason, a guard (confirm flag, battery level limit, time limit) and a test.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements_test.txt
```

Run the checks before every commit:

```bash
ruff check .
pytest -q --cov=custom_components.tesla_tedapi --cov-report=term-missing
```

The tests use a fake gateway and cloud (`tests/conftest.py`); they never talk to a real Powerwall.

## What a pull request needs

- **Tests for all new code.** Coverage is above 99 % and CI fails below 99 %; keep new modules at 100 % where you can, including the error paths.
- **`ruff check .` clean.**
- **Translations.** Every user-facing string goes in `strings.json` and in all eight languages under `translations/` (en, de, fr, nl, es, it, pl, pt-BR). If you can't translate, say so in the PR and use English as a placeholder.
- **A `CHANGELOG.md` line.**
- **Short docs.** One or two lines in the README per feature: what it does and where to find it.
- **No secrets** in code, tests, logs or screenshots.

## Branches and commits

- Open pull requests against a feature branch (for example `feature/2.4.1`), not `main`. Releases are merged to `main` after a soak test.
- Write commit messages as plain sentences: a short summary line, then what changed and why. No generated footers or attribution lines.

## Testing on real hardware

Reports from hardware other than a single Powerwall 3 are especially useful: Powerwall 2, Powerwall+, several Powerwalls, other regions and firmware versions.

- Try changes on your own system only, and only commands you understand.
- Start read-only. For dispatch or go off-grid, use short durations, a battery level limit, and stay near the Powerwall.
- Include the firmware version and the integration's diagnostics in your report, and check them for anything private first.
