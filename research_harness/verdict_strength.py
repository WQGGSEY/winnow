"""ADR 0008 — the verdict-strength type system.

closeness is never proven by the proposer's assertion. It is proven only by
a funded adversary *failing* against a referent the proposer did not author.
This module encodes the consequence: a verdict's MAXIMUM strength is bounded
by the strongest referent its falsifier was actually run (and checked) against
by the harness. A verdict stronger than the available referent is
*unconstructable* — not rejected after the fact, but absent from the reachable
vocabulary, because the harness derives the strength from the referent ledger
and the proposer never gets to author it.

The ladder (two axes, ADR 0008):

    internally_valid  <  construct_valid  <  transfer_valid

- ``internally_valid``  — internal referent only (the run's own metrics; no
  funded adversary against an un-authored referent). The honest air-gapped
  floor; replaces ADR 0006's ``unverified_screen``.
- ``construct_valid``   — Axis 1 (close to the QUESTION): a funded
  construct-adversary searched the construction's pass-but-wrong region
  against the FROZEN question and failed to break it. Air-gapped-earnable.
  Necessary, NOT sufficient — says nothing about reality.
- ``transfer_valid``    — Axis 2 (close to REALITY): a falsifier ran against
  a real referent (registered real adapter). **Unreachable air-gapped** —
  only a real referent the operator holds unlocks it. This is the
  constitutional silence about the gap the harness cannot measure.

Only HARNESS-EXECUTED, HARNESS-CHECKED evidence raises strength. A proposer
assertion (a string the run wrote) never does — it is typed ``unverified``.
"""

from __future__ import annotations

from typing import Any

# Strength order, weakest first. Index = rank.
VERDICT_LADDER = ["internally_valid", "construct_valid", "transfer_valid"]

# The reality-closeness verdict that air-gapped runs can never reach.
REALITY_VERDICT = "transfer_valid"


def strength_rank(verdict: str) -> int:
    """Rank of a verdict in the ladder; -1 if it is not a reachable verdict
    (i.e. an unsayable / unknown token)."""
    try:
        return VERDICT_LADDER.index(verdict)
    except ValueError:
        return -1


def referent_ledger(
    *,
    real_referent_verified: bool,
    construct_referent_verified: bool,
) -> dict[str, Any]:
    """Build the referent ledger from the two harness-checked signals.

    ``real_referent_verified``     — a real-adapter falsifier was executed and
                                      passed (Axis 2 unlock).
    ``construct_referent_verified``— a funded construct-adversary survived
                                      against the frozen question (Axis 1).

    Both are booleans the *harness* computed from executed evidence; neither is
    a proposer assertion.
    """
    return {
        "has_real_referent": bool(real_referent_verified),
        "has_construct_referent": bool(construct_referent_verified),
        "max_reachable_verdict": max_reachable_verdict(
            real_referent_verified=real_referent_verified,
            construct_referent_verified=construct_referent_verified,
        ),
    }


def max_reachable_verdict(
    *,
    real_referent_verified: bool,
    construct_referent_verified: bool,
) -> str:
    """The strongest verdict the thread may even *form*. Anything above this
    is unconstructable."""
    if real_referent_verified:
        return "transfer_valid"
    if construct_referent_verified:
        return "construct_valid"
    return "internally_valid"


def is_reachable(verdict: str, ledger: dict[str, Any]) -> bool:
    """True iff ``verdict`` is at or below the ledger's max_reachable_verdict.
    A verdict above the ceiling is unsayable — the gate uses this to refuse to
    even persist the stronger token."""
    ceiling = ledger.get("max_reachable_verdict", "internally_valid")
    return 0 <= strength_rank(verdict) <= strength_rank(ceiling)
