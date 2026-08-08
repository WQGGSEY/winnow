---
id: n_demo_001_b01__invalid_experiment__544d46585c
category: invalid_experiment
tags: ["runner", "failed", "eval", "agent_harness", "nested_agent_risk", "subscription_oauth", "validity", "worker_report_unexpected_observations"]
node_id: n_demo_001_b01
worker_status: failed
claim_verdict_candidate: not_evaluable
source_artifact: runs/prelive_tree_search/nodes/n_demo_001_b01/workspace/stdout.log
lesson: "In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: runner command could not start because the configured Python executable was unavailable."
---
# Failure Record: n_demo_001_b01__invalid_experiment__544d46585c

## Claim

Child branch for n_demo_001 can resolve an unexpected observation without letting the worker expand scope.

## Reason

The runner command could not start because the configured Python executable was unavailable.

## One-Line Lesson

In agent_harness, treat invalid_experiment as non-promotable until the runner uses an available, explicitly configured Python executable.
