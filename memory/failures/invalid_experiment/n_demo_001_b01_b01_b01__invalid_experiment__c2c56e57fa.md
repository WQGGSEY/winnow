---
id: n_demo_001_b01_b01_b01__invalid_experiment__c2c56e57fa
category: invalid_experiment
tags: ["runner", "metrics_evidence", "eval", "missing_metrics_file", "agent_harness", "nested_agent_risk", "subscription_oauth", "validity", "worker_report_unexpected_observations"]
node_id: n_demo_001_b01_b01_b01
worker_status: failed
claim_verdict_candidate: not_evaluable
source_artifact: nodes/n_demo_001_b01_b01_b01/workspace/src/experiment.py
lesson: "In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: declared metrics file is missing: artifacts/metrics.json"
---
# Failure Record: n_demo_001_b01_b01_b01__invalid_experiment__c2c56e57fa

## Claim

Child branch for n_demo_001_b01_b01 can resolve: Unexpected observation should be checked without letting the worker pursue it.

## Reason

declared metrics file is missing: artifacts/metrics.json

## One-Line Lesson

In agent_harness, treat invalid_experiment as non-promotable until the orchestrator addresses: declared metrics file is missing: artifacts/metrics.json
