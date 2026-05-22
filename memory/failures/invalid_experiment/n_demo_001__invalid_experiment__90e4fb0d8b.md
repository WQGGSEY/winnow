---
id: n_demo_001__invalid_experiment__90e4fb0d8b
category: invalid_experiment
tags: ["claude_cli", "permission_denied"]
node_id: n_demo_001
worker_status: blocked_permission
claim_verdict_candidate: not_evaluable
source_artifact: runs/manual_live_smoke/nodes/n_demo_001/workspace/claude_stdout.json
lesson: "In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: Claude CLI reported permission denials before a trusted worker_report could be accepted."
---
# Failure Record: n_demo_001__invalid_experiment__90e4fb0d8b

## Claim

A Sakana-v2-compatible harness can use bounded Claude Code subscription workers without corrupting the outer tree search.

## Reason

Claude CLI reported permission denials before a trusted worker_report could be accepted.

## One-Line Lesson

In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: Claude CLI reported permission denials before a trusted worker_report could be accepted.
