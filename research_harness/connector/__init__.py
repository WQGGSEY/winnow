"""Domain-connector: the vagueness-driven diverse claim-generation front-end.

Turns one problem P (from grilling) into a forest of diverse, gate-judged
research claims, of which the existing production gate selects the single
strongest-earned survivor. See ADR 0012 and CONTEXT.md
([[creativity]], [[abstraction (de-domained)]], [[reading (field-forced)]],
[[prune-1]], [[reduction]], [[research forest + single output]],
[[firewall (P-withholding)]]).

Reliability lives in the executed production gate, NOT in this front-end: the
generation steps (reading, prune-1, reduction) are best-effort and may
hallucinate; their errors are absorbed downstream. The ONE exception is the
firewall (P-withholding), enforced structurally because its failure is
unrecoverable.
"""
