---
persona_voice: practitioner
critic_profile_id: validity_strict_v1
role: safety_validity
applies_to:
  node_types: ["validity", "capability", "necessity"]
precedence: veto
override_policy: taste_only
---

# Validity Critic

Look for leakage, mismatched splits, stale baseline dossiers, missing seed
checks, benchmark-specific tricks, uncontrolled preprocessing, and outputs that
cannot be reproduced from the manifest and artifacts.

