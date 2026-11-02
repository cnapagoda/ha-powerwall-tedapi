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
- **Docs.** Document the feature in the matching page under [`docs/`](docs/README.md) (entities, actions, events, options, troubleshooting). Keep the README to the overview: add a line there only for a headline feature. New user-visible errors or repairs also go in [Troubleshooting](docs/troubleshooting.md).
- **No secrets** in code, tests, logs or screenshots.

## Branches and commits

- Open pull requests against a feature branch (for example `feature/2.4.1`), not `main`. Releases are merged to `main` after a soak test.
- Write commit messages as plain sentences: a short summary line, then what changed and why. No generated footers or attribution lines.

## Testing on real hardware

Reports from hardware other than a single Powerwall 3 are especially useful: Powerwall 2, Powerwall+, several Powerwalls, other regions and firmware versions.

- Try changes on your own system only, and only commands you understand.
- Start read-only. For dispatch or go off-grid, use short durations, a battery level limit, and stay near the Powerwall.
- Include the firmware version and the integration's diagnostics in your report, and check them for anything private first.
- Use the [hardware report](https://github.com/cnapagoda/ha-powerwall-tedapi/issues/new?template=hardware_report.yml) form: reports feed [Compatibility](docs/compatibility.md).

## Documentation

The docs are plain Markdown in [`docs/`](docs/README.md), one page per topic, rendered by GitHub. The README is shown in HACS, so it uses absolute links; pages under `docs/` use relative links and images from `docs/images/`.

- Write for someone who has never used the integration: say what something does, then how.
- Use the UI's exact words for menus, fields and messages (copy them from `strings.json`).
- Show entity IDs in the default form, `sensor.powerwall_tedapi_…`.
- Docs-only pull requests are welcome, including corrections from your own hardware.
