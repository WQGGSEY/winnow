---
id: n_demo_001__invalid_experiment__01330e757f
category: invalid_experiment
tags: ["runner", "failed", "eval", "agent_harness", "nested_agent_risk", "subscription_oauth"]
node_id: n_demo_001
worker_status: failed
claim_verdict_candidate: not_evaluable
source_artifact: runs/prelive_tree_search/nodes/n_demo_001/workspace/stdout.log
lesson: "In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: runner command could not start because the configured Python executable was unavailable."
---
# Failure Record: n_demo_001__invalid_experiment__01330e757f

## Claim

A Sakana-v2-compatible harness can use bounded Claude Code subscription workers without corrupting the outer tree search.

## Reason

The runner command could not start because the configured Python executable was unavailable.

## One-Line Lesson

In agent_harness, treat invalid_experiment as non-promotable until the runner uses an available, explicitly configured Python executable.
