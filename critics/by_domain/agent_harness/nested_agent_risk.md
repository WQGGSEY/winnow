---
critic_profile_id: nested_agent_risk_v1
role: safety_validity
applies_to:
  domains: ["agent_harness"]
precedence: veto
override_policy: taste_only
---

# Nested Agent Risk Critic

This project wraps Claude Code, an agent runtime, inside an outer Sakana-style
tree search. Watch for any place where inner workers plan, branch, expand
scope, choose baselines, ask for interactive permission, or write shared state.

The outer orchestrator owns search policy. Workers execute bounded contracts.

