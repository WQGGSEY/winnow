---
id: n_demo_001_b01__invalid_experiment__544d46585c
category: invalid_experiment
tags: ["runner", "failed", "eval", "agent_harness", "nested_agent_risk", "subscription_oauth", "validity", "worker_report_unexpected_observations"]
node_id: n_demo_001_b01
worker_status: failed
claim_verdict_candidate: not_evaluable
source_artifact: /Users/seongje/Desktop/project/research_harness/runs/prelive_tree_search/nodes/n_demo_001_b01/workspace/stdout.log
lesson: "In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: runner command could not start: [Errno 2] No such file or directory: 'python'"
---
# Failure Record: n_demo_001_b01__invalid_experiment__544d46585c

## Claim

Child branch for n_demo_001 can resolve: Unexpected observation should be checked without letting the worker pursue it.

## Reason

runner command could not start: [Errno 2] No such file or directory: 'python'

## One-Line Lesson

In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: runner command could not start: [Errno 2] No such file or directory: 'python'
