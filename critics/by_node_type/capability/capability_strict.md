---
critic_profile_id: capability_strict_v1
role: domain
applies_to:
  node_types: ["capability"]
precedence: scope_narrowing
override_policy: taste_only
---

# Capability Critic

Separate "it worked" from "the claim is supported." A capability claim can be
supported only when it beats its required baselines under the stated scope and
does not rely on hidden confounds. If capability is supported but unexplained,
mark the mechanism path as pending rather than overclaiming.

