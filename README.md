# Research Harness

A Python research prototype for **evidence-governed experiments with bounded
Codex workers**. It separates hypothesis generation, experiment execution,
baseline qualification, independent review, and publication checks instead of
treating an agent's success report as sufficient evidence.

**Status: maintained research prototype.** Maintenance focuses on correctness,
regressions, documentation, and reproducibility. This is not a turnkey autonomous
scientist or a claim of publication-quality research. Internal states such as
`goal_achieved` describe harness checks, not independent scientific certification.
See the [paper-readiness audit](docs/research/paper-readiness-audit.md) for known
limitations.

## Current workflow

```text
research goal
  -> grilling (structured interview)
  -> connector (diverse research perspectives and baseline research)
  -> immutable GoalContract
  -> one direction attempt at a time
  -> execution evidence, independent reviews, and real-holdout checks
  -> manuscript preview and separate audit summary, only if gates pass
```

The active frontend phases are `grilling`, `connector`, and `production`.
The [pipeline specification](docs/PIPELINE.md) is authoritative. Older market,
refiner, and tree-search documentation describes historical workflows, not the
new-thread entry path.

The current implementation includes:

- Provider-neutral request/result types around Codex CLI sessions, explicit
  live-use acknowledgements, and per-invocation MCP configuration.
- Claim and experiment contracts, deterministic execution manifests, baseline
  qualification, and reports derived from recorded source/metric evidence.
- An immutable goal, isolated direction attempts, private failure context,
  construct-adversary and real-holdout gates, and rebuttal review.
- A localhost operator UI and persistent run artifacts. `paper.html` is a
  manuscript preview; `interactive_summary.html` exposes the supporting state.

These mechanisms reduce some failure modes; they do not prove that generated
code, measurements, reviews, or scientific conclusions are correct.

## Requirements and installation

Use a **Linux checkout** with Python 3.11+ and `bubblewrap` (`bwrap` on `PATH`).
The CI configuration targets Ubuntu 22.04 with Python 3.12 and 3.13; it does not
establish macOS or Windows support. Local experiments require usable Linux
namespaces, not just an installed `bwrap` binary.

The documented workflow runs from the repository root because configuration,
critic personas, and research profiles live outside the Python package. A wheel
is not a self-contained replacement for this checkout.

```bash
# Debian/Ubuntu: install the host tools, then create an isolated Python environment.
sudo apt-get update
sudo apt-get install --yes git python3-venv bubblewrap ripgrep

git clone https://github.com/WQGGSEY/research_harness.git
cd research_harness
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
python -m pip check

# Verify that this host permits the isolation primitives used by LocalRunner.
bwrap --ro-bind / / --unshare-net --unshare-pid --proc /proc --dev /dev -- true
```

`.[test]` includes the frontend and validation dependencies. For a smaller UI
installation without the test dependencies, use `.[frontend]` instead. Model
CLIs, CUDA/PyTorch, private datasets, and experiment-specific dependencies are
not installed by either extra. If the namespace probe fails, use a compatible
Linux host; do not remove sandboxing to make a check pass.

## Credential-free validation

From the repository root, with the virtual environment active:

```bash
python -B -m pytest -ra
python -B -m research_harness.local_preflight
```

Use **pytest**, not `unittest discover`, for the full suite: the shared pytest
configuration disables legacy Claude web-search subprocesses. CI does not
install or authenticate Codex/Claude, supply provider keys, or acknowledge live
model execution. Dependency installation still uses the network.

For a legacy deterministic artifact demonstration, `python -m research_harness`
uses the local runner. Its demo metrics are not research evidence. Read the
[CI workflow](.github/workflows/ci.yml) for the exact validation scope; no fixed
test count or scientific performance result is asserted here.

## Configure resources before live work

The distributed `settings.json` is deliberately portable:

- Executable overrides are empty, so the runner uses the active `PATH`.
- Advertised runner capabilities are `local_runner` and `cpu` only.
- Registered local data adapters and MCP server environment overrides are empty.

Register resources that actually exist on your machine before starting research.
Use the settings UI or edit `settings.json` locally. Add CUDA, framework, data,
and absolute executable settings only when they are available. Do not commit
private paths, credentials, restricted data, or claims of unavailable resources.

Existing operators should back up their old `settings.json` **outside the
repository before upgrading**, then reapply only the required local resource
settings. This change does not delete their datasets or environments.
`settings.local.json` is frontend acknowledgement/state storage, **not** a
complete runtime-configuration override file.

Per-role models remain configurable under `runtime.agent_models`; supervisor
model selection is under `runtime.llm_orchestrator.mcp`. Existing model defaults
are retained, but account availability must be checked locally. An optional
`experiment_plan_templates/` directory can hold operator-authored templates; no
ready-to-use retrieval template is bundled in this checkout. The fallback demo
is not a substitute for an experiment with real evidence.

## Live operator workflow

Live work requires a separately installed, authenticated Codex CLI and access
to the configured models. It consumes the operator's quota. Installing this
repository does not grant Codex access or any credits.

```bash
codex login status
python -m research_harness.frontend --port 8765
```

Open `http://127.0.0.1:8765`, create a thread, complete grilling and connector,
and start production from its panel. Review the resource settings and live-use
acknowledgements before doing so. Keep the UI on localhost; it is not a
multi-user or internet-facing service. See [MCP operations](docs/MCP_OPERATIONS.md)
for supervisor operation and per-invocation MCP configuration.

A CLI grilling session is also available:

```bash
RESEARCH_HARNESS_ALLOW_CODEX_LIVE=subscription_ack \
RESEARCH_HARNESS_EXECUTE_CODEX_LIVE=live_smoke_ack \
python -B -m research_harness.research_runner grill \
  --user-goal "your research goal" \
  --billing-ack --execute-ack \
  --run-dir runs/grilling/example
```

Do not set these acknowledgements in CI. Production can continue through retries
and multiple model sessions; soft milestone counts are not spending caps.
Monitor runs and stop the supervisor when necessary. No live model run is needed
to perform the credential-free checks above.

## Safety and limitations

LocalRunner restricts experiment writes and disables experiment networking, but
its read-only host filesystem mount is **not a confidentiality boundary**. Run
untrusted experiments in a disposable environment without sensitive readable
files or credentials. The supervisor, acquisition tools, model CLI, and browser
are separate trust boundaries. See [SECURITY.md](SECURITY.md).

A clean test run does not prove end-to-end research validity. Real-holdout data,
baseline implementation quality, source rights, model behavior, long-running
training, and manuscript readiness require separate verification. Historical
records under `.audit/` and `docs/research/` must not be read as current successful
runs or as a complete security audit.

## Documentation and contribution

- [Current pipeline](docs/PIPELINE.md), [architecture](docs/ARCHITECTURE.md),
  [MCP operations](docs/MCP_OPERATIONS.md), and [domain vocabulary](CONTEXT.md).
- [Contributing](CONTRIBUTING.md), [security reporting](SECURITY.md), and the
  [public-release checklist](docs/PUBLIC_RELEASE.md).
- [License](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).

The design was inspired by
[Sakana AI Scientist-v2](https://github.com/SakanaAI/AI-Scientist-v2).
This is an independent project, not an official Sakana or OpenAI product.
Original project code is offered under MIT; third-party assets retain their
own terms, including the bundled htmx distribution.
