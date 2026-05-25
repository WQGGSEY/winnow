---
id: n_demo_001__invalid_experiment__01330e757f
category: invalid_experiment
tags: ["runner", "failed", "eval", "agent_harness", "nested_agent_risk", "subscription_oauth"]
node_id: n_demo_001
worker_status: failed
claim_verdict_candidate: not_evaluable
source_artifact: /Users/seongje/Desktop/project/research_harness/runs/prelive_tree_search/nodes/n_demo_001/workspace/stdout.log
lesson: "In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: runner command could not start: [Errno 2] No such file or directory: 'python'"
---
# Failure Record: n_demo_001__invalid_experiment__01330e757f

## Claim

A Sakana-v2-compatible harness can use bounded Claude Code subscription workers without corrupting the outer tree search.

## Reason

runner command could not start: [Errno 2] No such file or directory: 'python'

## One-Line Lesson

In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: runner command could not start: [Errno 2] No such file or directory: 'python'
