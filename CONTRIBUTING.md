# Contributing

Research Harness is a maintained research prototype. Focused bug fixes,
regression tests, documentation corrections, and reproducible failure reports
are welcome. Large new features should start with an issue explaining the
problem and the smallest useful change; there is no promised roadmap or
response-time commitment.

## Development

Follow the Linux checkout instructions in [README.md](README.md), activate the
virtual environment, and install `python -m pip install -e '.[test]'`.

Run the smallest relevant tests first, then the full non-live validation:

```bash
python -B -m pytest -ra
python -B -m research_harness.local_preflight
python -m pip wheel --no-deps --wheel-dir dist .
git diff --check
```

The suite uses pytest configuration to prevent legacy Claude web-search calls.
Do not substitute `unittest discover` for the full run. Do not run live smoke
scripts, log in to provider accounts, supply keys, or enable live execution as
part of routine CI. A live integration check requires separate operator consent
and a clear quota boundary; report it separately from mocked/offline checks.

## Pull requests

Read `CLAUDE.md` and the relevant current code before editing. Preserve the
active pipeline, frozen goals, evidence gates, permission boundaries, and
operator-controlled model choices unless the PR explicitly addresses them.
Keep patches narrow, reuse existing tests and helpers, and avoid unrelated
refactoring. Do not loosen an assertion or skip a failing test solely to turn
CI green.

A useful PR describes the observed failure, the behavioral change, the commands
actually run, their outcomes, and what was not tested. Distinguish a mocked
provider result, a real subprocess execution, and a live model invocation.
AI-assisted contributions follow the same evidence standard. Do not claim
scientific success from an agent's prose, a demo, or a passing software suite.

Never commit credentials, local runtime settings, private datasets, run logs,
or holdouts. Scrub attachments before posting. Vulnerability reports should use
the private process in [SECURITY.md](SECURITY.md), not a public issue.

Contributions must be code/content you have the right to submit under this
project's MIT license. Retain applicable third-party license notices and
identify copied or adapted material. Adding an API/dataset adapter does not
itself grant rights to redistribute the source data.
