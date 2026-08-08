# Candidate c2: Direct API Port

Role: naive baseline.

Description: Replace each `llm.generate(prompt)` style call with Claude Code
invocation and return the final text.

Risk tags: nested agent variance, uncontrolled tool use, permission blocking,
scope drift, schema instability, global-state mutation risk.
