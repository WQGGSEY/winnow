"""Domain-connector: the vagueness-driven diverse claim-generation front-end.

Turns one problem P (from grilling) into diverse intake candidates and a
P-aware baseline dossier. Production freezes the problem and accepted success
bar before any direction is generated. See ADR 0012 and CONTEXT.md
([[creativity]], [[abstraction (de-domained)]], [[reading (field-forced)]],
[[prune-1]], [[reduction]],
[[firewall (P-withholding)]]).

Reliability lives in the executed production gate, NOT in this front-end: the
generation steps (reading, prune-1, reduction) are best-effort and may
hallucinate; their errors are absorbed downstream. The ONE exception is the
firewall (P-withholding), enforced structurally because its failure is
unrecoverable.
"""
