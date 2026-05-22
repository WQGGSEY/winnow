# Research Harness

Greenfield-compatible research harness inspired by Sakana AI Scientist-v2, with
Claude Code subscription workers treated as bounded runtime tools rather than
plain completion API calls.

This repository starts from a v0 scaffold. It preserves Sakana-v2-style search
vocabulary while adding claim contracts, worker envelopes, deterministic critic
governance, failure/lesson memory, and a rebuttal/AC publication gate.

References:

- Sakana AI Scientist-v2 repository: https://github.com/SakanaAI/AI-Scientist-v2
- Sakana AI Scientist-v2 BFTS config: https://github.com/SakanaAI/AI-Scientist-v2/blob/main/bfts_config.yaml

## Current v0 Scope

Included:

- Claim-centered node schema.
- Runtime envelope and worker report schemas.
- Baseline dossier structure with current-best, naive, and random/null roles.
- Always-included one-line lesson memory.
- Deterministic critic folder routing.
- Rebuttal packet, rebuttal critic stage, AC decision schema.
- Dry-run Claude Code invocation envelope and bounded prompt generation.
- Deterministic local runner manifest validation.
- Baseline dossier validation and dry-run resolution report.
- Mock end-to-end pipeline that writes a research state bundle and HTML summary.

Excluded for v0:

- Live Claude Code execution.
- Full Sakana-v2 BFTS parity.
- Real long-running training runner.
- Automated web baseline resolution.
- Full camera-ready generation.

## Run The Demo

```bash
python -m research_harness
```

The demo writes:

```text
runs/demo_run/node.json
runs/demo_run/worker_report.json
runs/demo_run/critic_review_bundle.json
runs/demo_run/orchestrator_reduction.json
runs/demo_run/rebuttal_packet.md
runs/demo_run/orchestrator_rebuttal.md
runs/demo_run/rebuttal_critic_bundle.json
runs/demo_run/ac_decision.json
runs/demo_run/research_state_bundle.json
runs/demo_run/interactive_summary.html
```

## Run Tests

```bash
python -B -m unittest discover -s tests -v
```

## Run Pre-Live Local Preflight

```bash
python -B -m research_harness.local_preflight
```

This checks config/profile loading, node invariants, deterministic critic
routing, dry-run Claude invocation envelope generation, worker-report schema
validation, deterministic runner manifest validation, baseline dossier
validation, rebuttal/AC gating, and interactive HTML generation without calling
Claude Code.

## Build Manual Live Smoke Plan

```bash
python -B -m research_harness.workers.live_gate
```

This still does not call Claude Code. It writes a schema-valid manual smoke
plan under `runs/manual_live_smoke/`, checks subscription auth precedence, and
records the exact command a human operator can run later. By default, the plan
is blocked by the billing guard; live Claude Code requires an explicit
`RESEARCH_HARNESS_ALLOW_CLAUDE_LIVE=subscription_ack` acknowledgement.

## Core Files

```text
settings.json                 Runtime, memory, publishing, and publication-gate knobs.
configs/harness.yaml           Sakana-like search config plus harness_semantics.
research_profile.md            Positive research taste and branch-generation priors.
lessons.yaml                   Active one-line lessons always included in orchestration.
critics/                       Deterministic critic personas.
memory/                        Failure, lesson, and baseline dossier indexes.
research_harness/              Standard-library v0 package.
```

## Non-Overridable Invariants

- Workers do not own search policy.
- Workers write only node-local artifacts.
- Critics are read-only and selected by deterministic governance.
- Experiments require claim contracts.
- Missing mandatory baselines make a claim not evaluable or confounded.
- Claude Code subscription auth is runtime preflight responsibility.
- Long-running jobs belong to deterministic runners, not agent sessions.
- Publication requires rebuttal and AC gating when enabled.
