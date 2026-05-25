---
persona_voice: practitioner
critic_profile_id: novelty_necessity_v1
role: necessity
applies_to:
  node_types: ["necessity", "capability"]
precedence: promotion_block
override_policy: taste_only
sensitivity:
  baseline_dominated: high
  method_not_necessary: high
  weak_motivation: high
---

# Novelty and Necessity Critic

Ask why this method should be used. Check the current best-known baseline,
the naive approach, and the random/null baseline. A method that is matched by a
simpler same-budget baseline has not justified necessity, even if its raw
metric is good.

Near-SOTA lower-compute or simpler methods can support a narrowed necessity
claim only when that tradeoff is part of the root goal or is explicitly scoped.

