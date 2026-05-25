---
id: n_demo_001__invalid_experiment__ae100009f3
category: invalid_experiment
tags: ["runner", "metrics_evidence", "eval", "missing_metrics_file", "agent_harness", "nested_agent_risk", "subscription_oauth"]
node_id: n_demo_001
worker_status: failed
claim_verdict_candidate: not_evaluable
source_artifact: nodes/n_demo_001/workspace/src/experiment.py
lesson: "In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: declared metrics file is missing: artifacts/metrics.json"
---
# Failure Record: n_demo_001__invalid_experiment__ae100009f3

## Claim

A Sakana-v2-compatible harness can use bounded Claude Code subscription workers without corrupting the outer tree search.

## Reason

declared metrics file is missing: artifacts/metrics.json

## One-Line Lesson

In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: declared metrics file is missing: artifacts/metrics.json
