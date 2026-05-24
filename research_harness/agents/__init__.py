"""Agent-level modules.

Unlike workers, agents are conceptually allowed to use tools and run
longer-horizon, multi-turn interactions. They are still bounded by the
harness through schema contracts, billing/execution gates, and
deterministic post-processing — but the worker invariants
(tools-disabled, single-shot JSON output, no shared memory write) do not
apply here.
"""
